"""
Training Classifier, Export & Benchmark using Vortex
====================================================
This tutorial shows you how to use vortex to train
classification model, then export and benchmark the
exported model.

We will use CIFAR10 for our example dataset
and mnasnet from torchvision for example model.

Vortex model base is derived from pytorch lightning,
so you can treat it just like pytorch lightning module
Like we'll show you in this example.

Then you can export the trained model to onnx and
benchmark it using vortex.
"""

# %%
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torchvision as vision

import pytorch_lightning as pl

from collections import OrderedDict

from vortex.development.utils.registry import Registry
from vortex.development.utils.metrics import METRICS, ClassificationMetrics, MetricBase
from vortex.development.networks.models import ModelBase
from vortex.development.utils.profiler.lightning import Profiler
from abc import abstractmethod

# %%
# 1. Preparing Dataset
# --------------------
# We will use pytorch lightning's ``LightningDataModule``,
# to load the dataset, we use ``torchvision``'s ``CIFAR10``.
# 
# Note that we will use this dataset module also for benchmark
# using vortex, and we need to define ``test_dataloader`` in
# addition to ``train_dataloader`` and ``test_dataloader``.
class CIFAR(pl.LightningDataModule):
    def __init__(self, batch_size, img_size, **kwargs):
        super().__init__()
        self.batch_size = batch_size
        self._init_train_set()
        self._init_val_set()
    
    def prepare_data(self):
        pass
    
    def _init_train_set(self):
        self.transform = vision.transforms.Compose([
            vision.transforms.ToTensor(),
            vision.transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
        ])
        self.train_set = vision.datasets.CIFAR10(root='./data', train=True,
                            download=True, transform=self.transform)
        self.class_names = ['airplanes', 'cars', 'birds', 'cats', 'deer', 'dogs', 'frogs', 'horses', 'ships', 'trucks']
    
    def _init_val_set(self):
        self.val_set = vision.datasets.CIFAR10(root='./data', train=False,
                            download=True, transform=self.transform)
    
    def _init_test_set(self):
        self.test_set = vision.datasets.CIFAR10(root='./data', train=False,
                            download=True, transform=self.transform)

    def train_dataloader(self):
        kwargs = dict(
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=2
        )
        self.train_loader = torch.utils.data.DataLoader(self.train_set, **kwargs)
        return self.train_loader
    
    def val_dataloader(self):
        kwargs = dict(
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=2
        )
        self.val_loader = torch.utils.data.DataLoader(self.val_set, **kwargs)
        return self.val_loader
    
    def test_dataloader(self, batch_size):
        kwargs = dict(
            batch_size=batch_size,
            shuffle=True,
            num_workers=2
        )
        self.test_loader = torch.utils.data.DataLoader(self.val_set, **kwargs)
        return self.test_loader

@METRICS.register()
class MyClassificationMetrics(pl.metrics.Metric):
    def __init__(self, num_classes, *, acc={}, f1={}, prec={}, rec={}):
        super().__init__()
        prec.update(dict(num_classes=num_classes))
        rec.update(dict(num_classes=num_classes))
        f1.update(dict(num_classes=num_classes))
        self.metrics = nn.ModuleList([
            pl.metrics.Accuracy(**acc),
            pl.metrics.F1(**f1),
            pl.metrics.Precision(**prec),
            pl.metrics.Recall(**rec),
        ])
        self.metrics_args = dict(
            num_classes=num_classes,
            acc=acc, f1=f1, prec=prec, rec=rec
        )

    def update(self, inputs, targets):
        if isinstance(inputs, list):
            inputs = np.concatenate([inp['class_label'] for inp in inputs])
            inputs = torch.as_tensor(inputs).to(targets.device)
        for metric in self.metrics:
            metric.update(inputs, targets)
    
    def compute(self):
        results = {}
        typename = lambda x: type(x).__name__.split('.')[-1]
        for metric in self.metrics:
            name = typename(metric)
            results[name] = metric.compute()
        return results

# %%
# 2. Preparing the Model
# ----------------------
# 
# ``vortex``'s ``ModelBase`` is just an extension to ``pl.LightningModule``,
# so we can use it just like any ``LightningModule``, like define
# ``training_step``, ``validation_step``, ``configure_optimizers``, etc.
# 
# Additionally, we need to define the following methods:
#
# +-----------------------+-------------------------------------------------------------------------------------------------------------------+
# | Method                | Description                                                                                                       |
# +=======================+===================================================================================================================+
# | ``input_names``       | should return a list of string representing the input names                                                       |
# +-----------------------+-------------------------------------------------------------------------------------------------------------------+
# | ``output_names``      | should return a list of string representing the output names                                                      |
# +-----------------------+-------------------------------------------------------------------------------------------------------------------+
# | ``on_export_start``   | (optional) will be called by vortex onnx exporter at the start of export                                          |
# +-----------------------+-------------------------------------------------------------------------------------------------------------------+
# | ``available_metrics`` | return metric(s) used by this module, if any.                                                                     |
# +-----------------------+-------------------------------------------------------------------------------------------------------------------+
# | ``output_format``     | should return a nested dictionary representing the structure of the batched output, defined for single batch      |
# +-----------------------+-------------------------------------------------------------------------------------------------------------------+
# 
# The additional methods above will be used for exporting.
# We can use on_export_start to sample input from dataset for exporting.
# The structure of ``output_format`` can be described using the following example:
# Assume the model return NxE 2D array/tensor where the first axis represent batch index
# and the second axis represent class label and confidence where class label is located
# at index 0 and class confidence at index 1, visually:
# 
# +-------+-------------+------------------+
# | batch | class_label | class_confidence |
# +=======+=============+==================+
# | 0     | 1           | 0.8              |
# +-------+-------------+------------------+
# | 1     | 4           | 0.9              |
# +-------+-------------+------------------+
# 
# then the output_format should be:
# ``{'class_label': {'indices':[0], 'axis': 0}, 'class_confidence': {'indices':[1], 'axis': 0}}``
# Note that field 'indices' and 'axis' from inner dict are reserved, this arguments is acually
# the argument for ``np.take`` which is used to slice output for single output.
# 
# Roughly, the following pseudocode illustrates how we slice batched output:
# 
# .. code-block:: python
# 
#     n = ouputs.shape[0]
#     results = []
#     for i in range(n):
#         result = {}
#         for field_name, args in output_format.items():
#             result[field_name] = np.take(outputs[i],**args)
#         results.append(result)
# 
class Model(ModelBase):
    def __init__(self, num_classes):
        super().__init__()
        self.num_classes = num_classes
        self.model = vision.models.mnasnet0_5(pretrained=False, progress=True, num_classes=num_classes)
        self.criterion = nn.CrossEntropyLoss()
        # self.metrics = MyClassificationMetrics(self.num_classes)
        self.metrics = ClassificationMetrics()
    
    def postprocess(self, x):
        x = torch.nn.functional.softmax(x, dim=1)
        conf_label, cls_label = x.max(dim=1, keepdim=True)
        return torch.stack((cls_label.float(), conf_label), dim=1)

    def predict(self, x):
        x = self.model(x)
        return self.postprocess(x)

    def forward(self, x):
        return self.model(x)

    def configure_optimizers(self):
        optimizer = optim.SGD(self.model.parameters(), lr=0.001, momentum=0.9)
        return optimizer

    def training_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)
        loss = self.criterion(y_hat, y)
        self.log('train_loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
        prediction = self.postprocess(y_hat)
        self.metrics.update(prediction.cpu().detach(), y.cpu().detach())
        return loss
    
    def validation_step(self, batch, batch_idx):
        x, y = batch
        with torch.no_grad():
            y_hat = self.predict(x)
        y_hat = y_hat[:,:]
        result = self.metrics(y_hat.cpu(), y.cpu())
        self.log_dict(result)
    
    def validation_epoch_end(self, *args, **kwargs):
        self.log_dict(self.metrics.compute(), on_epoch=True, prog_bar=True, logger=True)
        # we know that ClassificationMetrics' state need to be reset
        self.metrics.eval_init()

    def training_epoch_end(self, outs):
        # log epoch metric
        self.log_dict(self.metrics.compute())
        # we know that ClassificationMetrics' state need to be reset
        self.metrics.eval_init()

    def get_example_inputs(self):
        return self.sample,

    @property
    def input_names(self):
        return self._input_names

    @property
    def output_names(self):
        return self._output_names

    def on_export_start(self, exporter, dataset=None):
        batch_size = exporter.batch_size
        # not mandatory
        self.sample = next(iter(dataset.train_dataloader()))[0]
        self.sample = self.sample[:batch_size]
        self.class_names   = dataset.class_names
        self._input_names  = ['input']
        self._output_names = ['output']

    @property
    def available_metrics(self):
        return self.metrics

    @property
    def output_format(self):
        return {
            "class_label": {"indices": [0], "axis": 0},
            "class_confidence": {"indices": [1], "axis": 0}
        }

from vortex.development.exporter.onnx import ONNXExporter
from vortex.development.utils.runtime_wrapper import RuntimeWrapper

export_path = 'export_test.onnx'

# %%
# 3. Train and Evaluate
# ---------------------
# To train the model, we will use pytorch lightning's Trainer,
# to export to onnx, we will use vortex' ONNXExporter.
# 
# We will also use pytorch lightning's trainer
# to benchmark the exported model, wrapped in vortex' ``RuntimeWrapper``.

def train():
    dataset = CIFAR(128, img_size=32)
    loggers = [
        pl.loggers.TensorBoardLogger('logs/'),
    ]
    trainer = pl.Trainer(
        max_epochs=200, gpus=1, logger=loggers
    )
    model = Model(10)
    trainer.fit(model, dataset)

    exporter = ONNXExporter(dataset=dataset)
    exporter(model, export_path)

def evaluate():
    img_size = 32
    dataset  = CIFAR(1, img_size=img_size)
    profiler = Profiler(plot_dir='plot')
    trainer  = pl.Trainer(profiler=profiler)
    metric_args = dict(num_classes=10)
    runtime_device = 'cpu'
    model = RuntimeWrapper(export_path,
        profiler=profiler,
        metric_args=metric_args,
        runtime=runtime_device
    )
    batch_size = model.batch_size
    test_loader = dataset.test_dataloader(batch_size)
    trainer.test(model, test_loader)
    print(profiler.summary())
    if isinstance(profiler, Profiler):
        md = profiler.report(model=model,experiment_name='mnasnet')

        # must be str type
        dataset_info = [
            ['image_size',   str(img_size)],
            ['batch_size', str(batch_size)],
        ]
        dataset_info = md.make_table(header=['dataset args', 'value'], data=dataset_info)
        md.add_section('Dataset')
        md.write('Dataset name: CIFAR10')
        md.write(dataset_info)

        output_filename = 'report.md'
        md.save(output_filename)

if __name__=='__main__':
    train()
    evaluate()
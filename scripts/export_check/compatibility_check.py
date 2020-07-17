import onnx
import torch
import numpy as np
import os, sys, inspect
from pathlib import Path

# repo_root = Path(__file__).parent.parent.parent.

if __name__=='__main__' :
    import os
    import sys
    from pathlib import Path
    proj_path = os.path.abspath(Path(__file__).parents[2])
    sys.path.append(proj_path)

from math import isclose
from datetime import datetime
from easydict import EasyDict
# from networks.models import create_model_components
from vortex.networks.modules.backbones import all_models as all_backbones
from vortex.utils.profiler.resource import get_uname, get_cpu_info, get_gpu_info
# from runtime_predict import model_runtime_map
from vortex_runtime import model_runtime_map
# from runtime_predict import create_model as create_onnx_model
# from runtime_predict import predict as onnx_predict
# from predict import predict as torch_predict
# from export import create_exporter, create_predictor
from vortex.predictor import create_predictor, get_prediction_results
from vortex.core.factory import create_model,create_runtime_model,create_exporter
from typing import Type,List,Dict
from vortex_runtime.basic_runtime import BaseRuntime
from vortex.predictor.base_module import BasePredictor

np.random.seed(0)
torch.manual_seed(0)

isclose_config = dict(
    rel_tol=1e-3,
    abs_tol=1e-6,
)

mean_std=dict(
    mean=[0.5, 0.5, 0.5],
    std=[0.5, 0.5, 0.5],
)

model_argmap = EasyDict(
    FPNSSD=dict(
        preprocess_args=dict(
            input_size=512,
            input_normalization=mean_std
        ),
        network_args=dict(
            backbone='shufflenetv2_x1.0',
            n_classes=20,
            pyramid_channels=256,
            aspect_ratios=[1, 2., 3.],
        ),
        loss_args=dict(
            neg_pos=3,
            overlap_thresh=0.5,
        ),
        postprocess_args=dict(
            nms=True,
        )
    ),
    RetinaFace=dict(
        preprocess_args=dict(
            input_size=640,
            input_normalization=mean_std
        ),
        network_args=dict(
            n_classes=1,
            backbone='shufflenetv2_x1.0',
            pyramid_channels=64,
            aspect_ratios=[1, 2., 3.],
        ),
        loss_args=dict(
            neg_pos=7,
            overlap_thresh=0.35,
            cls=2.0,
            box=1.0,
            ldm=1.0,
        ),
        postprocess_args=dict(
            nms=True,
        ),
    ),
    softmax=dict(
        network_args=dict(
            backbone='shufflenetv2_x1.0',
            n_classes=10,
            freeze_backbone=False,
        ),
        preprocess_args=dict(
            input_size=32,
            input_normalization=dict(
                mean=[0.4914, 0.4822, 0.4465],
                std=[0.2023, 0.1994, 0.2010]
            )
        ),
        loss_args=dict(
            reduction='mean'
        ),
        postprocess_args={}
    )
)

test_backbones = all_backbones.copy()
## useful for single backbone check
# exclude_backbones = all_backbones.copy()
# exclude_backbones.remove('darknet53')
exclude_backbones = []

# exclude_models = ['softmax', 'RetinaFace']
exclude_models = []

## temporary metainfo
## TODO : remove
tmp_output_format = dict(
    softmax=dict(
        class_label=dict(
            indices=[0],
            axis=1,
        ),
        class_confidence=dict(
            indices=[1],
            axis=1,
        ),
    )
)

## template for single model
model_report_template = """
### {model_name}
| Backbone                 | Exportable  |
| ------------------------ | :---------: |
{backbones}
"""
## template for single model export
backbone_report_template = """| {backbone} | {result} |"""

## template for single runtime class
runtime_report_template = """
### {runtime_name}
| Model | Backbone            | Status  | diff | msg |
| ----- | ------------------- | :-----: | :--: | :-: |
{result}
"""
## template for single model evaluation
eval_report_template = """| {model} | {backbone} | {status} | {diff} | {msg} |"""

## final docs template
docs_report_template = """
# Model Compatibility   
torch version : {torch_version}.
onnx version : {onnx_version}.
Test date : {time}.
**Available backbone(s)** : {all_backbones}.
**Tested backbone(s)** : {tested_backbones}.
**Excluded backbone(s)** : {excluded_backbone}.
Note : this test are performed using **random weight** and **{example_input} input**.
This report is autogenerated from {uname}

## Test Environment   
### CPU   
{cpu_info}
### GPU   
{gpu_info}   

## IR Export
{export_results}
## Runtime Env.
- eval check with [`isclose`](https://docs.python.org/dev/library/math.html#math.isclose) config : {isclose_config}
- diff : normalized elementwise absolute difference, empty tensor ignored.
- `ERROR` : failed to run
- `FAILED` : elementwise `isclose` is `False`, torch and inference engine yield different results
- `SUCCESS` : runnable and elementwise `isclose` is `True`, difference between torch and inference engine outputs within tolerance
{eval_results}
"""

export_args=EasyDict(dict(
    module='onnx',
    args=dict(
        opset_version=10,
    ),
    ## optional temporary field (?)
    postprocess_args=dict(
        nms=False,
    )
))

test_output_directory = 'tmp/vortex'

from enum import Enum
from collections import namedtuple

class Status(Enum) :
    UNKNOWN = 0
    SUCCESS = 1
    FAILED = 2
    ERROR = 3
    def __bool__(self) :
        return self.value == Status.SUCCESS

## python 3.7+
# EvalResult_ = namedtuple('EvalResult',['status','diff','msg'],defaults=(Status.UNKNOWN,0.0,''))
## python <3.7
EvalResult_ = namedtuple('EvalResult',['status','diff','msg'])
EvalResult_.__new__.__defaults__ = (Status.UNKNOWN,None,'')

class EvalResult(EvalResult_) :
    def __repr__(self) :
        return "{status} | diff ({diff}) | msg : {msg}".format_map(self.__dict__)

def get_test_experiment_name(model_name, backbone, suffix, output_directory='') :
    name = 'test_{}_{}_{}'.format(model_name, backbone, suffix)
    if output_directory :
        name = '{}/{}'.format(output_directory, name)
    return name

def create_model_cfg(model_name, model_arg) :
    model_arg = EasyDict(model_arg)
    config = {
        'name' : model_name,
        **model_arg
    }
    return EasyDict(config)

def export_check(model_name : str, model_arg : dict, export_arg : dict, suffix : str, predictor_args : dict={}, example_image=None) :
    output_directory = Path(test_output_directory)
    output_directory.mkdir(exist_ok=True,parents=True)
    model_config = create_model_cfg(model_name, model_arg)
    model_components = create_model(model_config)
    ## TODO : remove
    if model_name in tmp_output_format :
        predictor_args['metainfo'] = tmp_output_format[model_name]
    predictor = create_predictor(
        model_components=model_components,
        **predictor_args
    ).eval()
    experiment_name = get_test_experiment_name(model_name, model_arg.network_args.backbone, suffix)
    image_size = model_config.preprocess_args.input_size
    exporter = create_exporter(
        config=export_arg,
        experiment_name=experiment_name,
        image_size=image_size,
        output_directory=output_directory
    )
    ## TODO : read export error msg
    result = exporter(predictor, example_image_path=example_image)
    torch.save(predictor.model.state_dict(), output_directory / '{}.pth'.format(experiment_name))
    del predictor, model_components
    return result

def eval_check(runtime : str, model_name : str, model_arg : dict, export_arg : dict, suffix : str, predictor_args : dict={}) :
    ## torch predictor
    output_directory = Path(test_output_directory)
    output_directory.mkdir(exist_ok=True,parents=True)
    model_config = create_model_cfg(model_name, model_arg)
    model_components = create_model(model_config)
    experiment_name = get_test_experiment_name(model_name, model_arg.network_args.backbone, suffix)
    onnx_model_path = output_directory / '{}.onnx'.format(experiment_name)
    pth_path = output_directory / '{}.pth'.format(experiment_name)
    if not (onnx_model_path.exists() and pth_path.exists()) :
        return EvalResult(Status.ERROR, msg='file not found, export might have failed')

    ckpt = torch.load(pth_path)
    if 'state_dict' in ckpt:
        ckpt = ckpt['state_dict']
    model_components.network.load_state_dict(ckpt)
    if model_name in tmp_output_format :
        predictor_args['metainfo'] = tmp_output_format[model_name]
    predictor = create_predictor(
        model_components=model_components,
        **predictor_args
    ).eval()
    image_size = model_config.preprocess_args.input_size
    ## onnx model
    try :
        ## TODO : check for fallback
        onnx_model = create_runtime_model(str(onnx_model_path), runtime)
    except Exception as e:
        print(e)
        return EvalResult(Status.ERROR, msg='RuntimeErorr : {}'.format(str(e)))
    ## predict check
    input_test = (np.random.rand(1,image_size,image_size,3) * 255).astype(np.uint8)
    ## TODO : read additional input from predictor or onnx input spec
    additional_args = dict(
        score_threshold=0.0,
        iou_threshold=1.0,
    )
    torch_results = torch_predict(predictor, input_test, **additional_args)
    onnx_results = onnx_predict(onnx_model, input_test, **additional_args)
    ok = len(torch_results) == len(onnx_results)
    # print("len(torch_results) == len(onnx_results)", len(torch_results) == len(onnx_results))
    status = EvalResult(status=ok, msg="len(torch_results) != len(onnx_results)" if not ok else "")
    for torch_result, onnx_result in zip(torch_results, onnx_results) :
        if not status :
            break
        ok = len(torch_result.keys()) == len(onnx_result.keys())
        # print("len(torch_result.keys()) == len(onnx_result.keys())", len(torch_result.keys()) == len(onnx_result.keys()))
        status = EvalResult(status=ok, msg="len(torch_result.keys()) != len(onnx_result.keys())" if not ok else "")
        if not status :
            break
        ok = all(key in onnx_result.keys() for key in torch_result.keys())
        # print("all(key in onnx_result.keys() for key in torch_result.keys())", all(key in onnx_result.keys() for key in torch_result.keys()))
        status = EvalResult(status=ok, msg="not all(key in onnx_result.keys() for key in torch_result.keys())" if not ok else "")
        if not status :
            break
        ok = all(isclose(onnx_value, torch_value, **isclose_config) for key in onnx_result.keys() for onnx_value, torch_value in zip(onnx_result[key].flatten(), torch_result[key].flatten()))
        ## TODO : collect diff across batch
        # diff = sum(np.sum(np.abs(onnx_result[key] - torch_result[key]).flatten()).item() / len(onnx_result[key].flatten()) for key in onnx_result.keys())
        diff = sum(((abs(onnx_value-torch_value) / len(onnx_result[key].flatten())) if len(onnx_result[key].flatten()) else 0) for key in onnx_result.keys() for onnx_value, torch_value in zip(onnx_result[key].flatten(), torch_result[key].flatten()))
        # print("all(np.isclose(onnx_result[key], torch_result[key], **isclose_config) for key in onnx_result.keys())", all(np.isclose(onnx_result[key], torch_result[key], **isclose_config) for key in onnx_result.keys()))
        s = Status.FAILED if not ok else Status.SUCCESS
        status = EvalResult(status=s, diff=diff, msg="isclose failed" if not ok else "")
        if not status :
            break
    del onnx_model, predictor, onnx_results, torch_results, input_test, model_components
    return status

# def get_uname() :
#     import subprocess
#     result = subprocess.run(['uname', '-a'], stdout=subprocess.PIPE)
#     return result.stdout.decode("utf-8")

# def get_cpu_info() :
#     import subprocess
#     result = subprocess.run(['lscpu'], stdout=subprocess.PIPE)
#     return result.stdout.decode("utf-8")

# def get_gpu_info() :
#     import subprocess
#     # result = subprocess.run("lspci | grep ' NVIDIA ' | grep ' VGA ' | cut -d" " -f 1 | xargs -i lspci -v -s {}".split(' '), shell=True, stdout=subprocess.PIPE)
#     p0 = subprocess.Popen(('lspci'), stdout=subprocess.PIPE)
#     p1 = subprocess.Popen(('grep', " NVIDIA "), stdin=p0.stdout, stdout=subprocess.PIPE)
#     p0.stdout.close()
#     p2 = subprocess.Popen(('grep', " VGA "), stdin=p1.stdout, stdout=subprocess.PIPE)
#     p3 = subprocess.Popen(('cut', '--delimiter= ', '-f', '1'), stdin=p2.stdout, stdout=subprocess.PIPE)
#     p2.stdout.close()
#     out = subprocess.check_output(('xargs', '-i', 'lspci', '-v', '-s', '{}'), stdin=p3.stdout)
#     p3.wait()
#     return out.decode("utf-8")

def onnx_predict(model: Type[BaseRuntime], img: np.ndarray, **kwargs):
    predict_args = {}
    for name, value in kwargs.items() :
        if not name in model.input_specs :
            print('additional input arguments {} ignored'.format(name))
            continue
        ## note : onnx input dtype includes 'tensor()', e.g. 'tensor(uint8)'
        dtype = model.input_specs[name]['type'].replace('tensor(','').replace(')','')
        predict_args[name] = np.array([value], dtype=dtype) if isinstance(value, (float,int)) \
            else np.asarray(value, dtype=dtype)
    results = model(img, **predict_args)
    ## convert to dict for visualization
    results = [result._asdict() for result in results]
    return results

def torch_predict(predictor: BasePredictor, img: np.ndarray, **kwargs) -> List[Dict[str,np.ndarray]]:
    output_format = predictor.output_format
    device = list(predictor.parameters())[0].device
    inputs = {'input' : torch.from_numpy(img).to(device)}
    if hasattr(predictor.postprocess, 'additional_inputs') :
        additional_inputs = predictor.postprocess.additional_inputs
        assert isinstance(additional_inputs, tuple)
        for additional_input in additional_inputs :
            key, _ = additional_input
            if key in kwargs :
                value = kwargs[key]
                inputs[key] = torch.from_numpy(np.asarray([value])).to(device)
    with torch.no_grad() :
        results = predictor(**inputs)
    if isinstance(results, torch.Tensor) :
        results = results.cpu().numpy()
    if isinstance(results, (np.ndarray, (list, tuple))) \
        and not isinstance(results[0], (dict)):
        ## first map to cpu/numpy
        results = list(map(lambda x: x.cpu().numpy() if isinstance(x,torch.Tensor) else x, results))
    results = get_prediction_results(
        results=results, 
        output_format=output_format
    )
    return results


def main(test_opset_version=[9, 10, 11], example_image=None) :
    time = str(datetime.now())
    uname = get_uname()
    cpu_info = get_cpu_info()
    gpu_info = get_gpu_info()
    supported_backbones = [backbone for backbone in test_backbones if not backbone in exclude_backbones]
    runtime_map = model_runtime_map['onnx']
    global model_argmap
    _ = [model_argmap.pop(k) for k in exclude_models]
    ni = len(test_opset_version)
    nj = len(model_argmap)
    nk = len(supported_backbones)
    nl = sum(rt.is_available() for rt in runtime_map.values())
    def print_export_progress(model_idx, backbone_idx, opset_version, msg) :
        print('[export opset {}] {} [{}/{}]'.format(
            opset_version, msg, (model_idx * nk + backbone_idx), (nj * nk)
        ))
    def print_eval_progress(runtime_idx, model_idx, backbone_idx, opset_version, msg) :
        print('[eval opset {}] {} [{}/{}]'.format(
            opset_version, msg, (runtime_idx * nl + model_idx * nk + backbone_idx), (nj * nk * nl)
        ))
    for i, opset_version in enumerate(test_opset_version) :
        export_args['args']['opset_version'] = opset_version
        suffix = 'opset{}'.format(opset_version)
        
        ## export tests
        tested_model = {}
        model_reports = []
        for model_idx, (model_name, model_args) in enumerate(model_argmap.items()) :
            backbone_reports = []
            for backbone_idx, backbone in enumerate(supported_backbones) :
                model_args.network_args.backbone = backbone
                try :
                    print_export_progress(
                        msg='trying to export {}-{}: '.format(backbone, model_name),
                        model_idx=model_idx, backbone_idx=backbone_idx, opset_version=opset_version
                    )
                    if 'filename' in export_args.args:
                        export_args.args.pop('filename')
                    ok = export_check(model_name, model_args, export_args, suffix=suffix, example_image=example_image)
                except RuntimeError as e:
                    print(e)
                    ok = False
                status = backbone, ok
                tested_model[model_name] = [status] if model_name not in tested_model else [*tested_model[model_name], status]
                backbone_reports.append(
                    backbone_report_template.format_map(dict(
                        backbone=backbone, result=ok
                    ))
                )
            model_reports.append(
                model_report_template.format_map(dict(
                    model_name=model_name,
                    backbones='\n'.join(backbone_reports)
                ))
            )
        print(tested_model)
        
        ## runtime tests
        tested_runtime = {}
        runtime_reports = []
        for runtime_idx, (runtime_name, runtime) in enumerate(runtime_map.items()) :
            if not runtime.is_available():
                continue
            runtime_model_reports = []
            tested_model = {}
            for model_idx, (model_name, model_args) in enumerate(model_argmap.items()) :
                for backbone_idx, backbone in enumerate(supported_backbones) :
                    model_args.network_args.backbone = backbone
                    print_eval_progress(
                        msg='trying to eval {}-{} with {}: '.format(backbone, model_name, runtime_name),
                        model_idx=model_idx, backbone_idx=backbone_idx, opset_version=opset_version, runtime_idx=runtime_idx
                    )
                    eval_status = eval_check(runtime_name, model_name, model_args, export_args, suffix)
                    status = backbone, bool(eval_status)
                    tested_model[model_name] = [status] if model_name not in tested_model else [*tested_model[model_name], status]
                    runtime_model_reports.append(
                        eval_report_template.format_map(dict(
                            model=model_name,
                            backbone=backbone,
                            status=str(eval_status.status),
                            diff=eval_status.diff,
                            msg=eval_status.msg,
                        ))
                    )
            tested_runtime[runtime_name] = [tested_model] if runtime_name not in tested_runtime else [*tested_runtime[runtime_name], tested_model]
            runtime_reports.append(
                runtime_report_template.format_map(dict(
                    runtime_name='{} ({})'.format(runtime_name, runtime.__name__),
                    result='\n'.join(runtime_model_reports)
                ))
            )
        print(tested_runtime)

        with Path('COMPATIBILITY_REPORT_opset{}.md'.format(opset_version)).open('w+') as f :
            docs = docs_report_template.format_map(dict(
                time=time,
                uname=uname,
                cpu_info=cpu_info,
                gpu_info=gpu_info,
                export_results='\n'.join(model_reports),
                eval_results='\n'.join(runtime_reports),
                isclose_config=', '.join('{} : {}'.format(key,value) 
                    for key, value in isclose_config.items()
                ),
                torch_version=torch.__version__,
                onnx_version=onnx.__version__,
                excluded_backbone=', '.join(exclude_backbones),
                example_input='random' if example_image is None else example_image,
                all_backbones=', '.join(all_backbones),
                tested_backbones=', '.join(supported_backbones),
            )).replace('\n', '   \n')
            f.write(docs)

if __name__=='__main__' :
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--opset-version', nargs='*', type=int)
    parser.add_argument('--example-image', help='optional example image for tracing')
    parser.add_argument('--backbones', default=all_backbones, choices=all_backbones, nargs='+', help='backbone(s) to test')
    parser.add_argument('--models', default=list(model_argmap.keys()), choices=list(model_argmap.keys()), nargs='+', help='model(s) to test')
    parser.add_argument('--exclude-backbones', default=[], choices=all_backbones, nargs='+', help='exclude this backbone(s) when testing')
    parser.add_argument('--exclude-models', default=[], choices=list(model_argmap.keys()), nargs='+', help='model(s) to exclude')
    args = parser.parse_args()
    test_opset_version = args.opset_version
    if test_opset_version is None:
        test_opset_version = [9,10,11]
    assert all(op in [9,10,11] for op in test_opset_version)
    print("warning : this check might be strorage and memory intensive")
    # global test_backbones, model_argmap, exclude_backbones, exclude_models
    test_backbones = args.backbones
    model_argmap = {model : model_argmap[model] for model in args.models}
    exclude_backbones = args.exclude_backbones
    exclude_models = args.exclude_models
    main(test_opset_version, args.example_image)

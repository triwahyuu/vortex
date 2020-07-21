from .modules import nvidia_dali
from typing import List
from easydict import EasyDict

supported_transforms = [
    'nvidia_dali'
]

class NvidiaDALIWrapper():
    
    def __init__(self,transforms,*args,**kwargs):
        self.compose = _parse_compose(transforms)

    def __call__(self,**data):
        for transform in self.compose:
            data = transform(**data)

        return data

def _parse_compose(transforms: List[EasyDict]):
    transforms_compose = []
    for component in transforms:
        transform = component['transform']
        args = {}
        if 'args' in component.keys():
            args = component['args']
        if not transform in nvidia_dali.modules.__all__:
            raise RuntimeError(
                "unsupported module '%s' in our Vortex wrapper of Nvidia DALI ops please check" % transform)
        try:
            transforms_compose.append(
                nvidia_dali.__getattribute__(transform)(**args))
        except Exception as e:
            raise RuntimeError(
                'when trying to create instance of %s with args : %s; got the following error : %s; please check Nvidia DALI wrapper in Vortex documentations' % (transform, args, str(e)))
    return transforms_compose



def create_transform(transforms: List = None, *args, **kwargs):
    if transforms is None:
        raise KeyError(
            "NVIDIA DALI module args expecting 'transforms' as one of the config args!")
    else:
        if not isinstance(transforms, list):
            raise TypeError(
                "NVIDIA DALI 'transforms' args expecting list as the input type, got %s" % type(transforms))
    nvidia_dali_wrapper = NvidiaDALIWrapper(
        transforms=transforms, *args, **kwargs)
    return nvidia_dali_wrapper





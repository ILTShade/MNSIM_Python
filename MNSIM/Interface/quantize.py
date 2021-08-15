#-*-coding:utf-8-*-
import collections
import copy
import math

from numpy import random
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Function

# last_activation_scale, last_weight_scale for activation and weight scale in last calculation
# last_activation_bit, last_weight_bit for activation and weight bit last time
last_activation_scale = None
last_weight_scale = None
last_activation_bit = None
last_weight_bit = None
# quantize Function
class QuantizeFunction(Function):
    @staticmethod
    def forward(ctx, input, qbit, mode, last_value = None, training = None):
        global last_weight_scale
        global last_activation_scale
        global last_weight_bit
        global last_activation_bit
        # last_value change only when training
        if mode == 'weight':
            last_weight_bit = qbit
        elif mode == 'activation':
            last_activation_bit = qbit
        else:
            assert 0, f'not support {mode}'
        # both weight and activation use min(max, 3*sigma+mu)
        if training or last_value is None:
            max_scale = torch.max(torch.abs(input))
            var_scale = 3 * torch.std(input) + torch.abs(torch.mean(input))
            scale = min(max_scale, var_scale)
        # update when training and last_value is not none
        if training and last_value is not None:
            if last_value.item() <= 0:
                last_value.data[0] = scale
            else:
                ratio = 0.1
                last_value.data[0] = ratio * last_value.data[0] + \
                    (1 - ratio) * scale
        # set scale in this quantize step
        if not (training or last_value is None):
            scale = last_value.data[0]
        # transfer
        thres = 2 ** (qbit - 1) - 1
        output:torch.Tensor = input * (thres / scale)
        output.round_().clamp_(min=-thres, max=thres)
        output.div_((thres / scale))
        scale = scale.item()
        if mode == 'weight':
            last_weight_scale = scale / thres
        elif mode == 'activation':
            last_activation_scale = scale / thres
        else:
            assert 0, f'not support {mode}'
        return output
    @staticmethod
    def backward(ctx, grad_output):
        return grad_output, None, None, None, None
Quantize = QuantizeFunction.apply

# AB = 1
# N = 512
# Q = 10
# # METHOD = 'TRADITION'
# # METHOD = 'FIX_TRAIN'
# # METHOD = 'SINGLE_FIX_TEST'
# METHOD = ''
# quantize layer, include conv and fc without bias
class QuantizeLayer(nn.Module):
    def __init__(self, hardware_config, layer_config, quantize_config):
        super(QuantizeLayer, self).__init__()
        # load hardware layer and quantize config in setting
        self.hardware_config = copy.deepcopy(hardware_config)
        self.layer_config = copy.deepcopy(layer_config)
        self.quantize_config = copy.deepcopy(quantize_config)
        self.bypass_quantize_weight = layer_config.get("bypass_quantize_weight", False)
        # split weights
        if self.layer_config['type'] == 'conv':
            assert 'in_channels' in self.layer_config.keys()
            channel_N = (self.hardware_config['xbar_size'] // (self.layer_config['kernel_size'] ** 2))
            complete_bar_num = self.layer_config['in_channels'] // channel_N
            residual_col_num = self.layer_config['in_channels'] % channel_N
            in_channels_list = []
            if residual_col_num > 0:
                in_channels_list = [channel_N] * complete_bar_num + [residual_col_num]
            else:
                in_channels_list = [channel_N] * complete_bar_num
            # generate Module List
            assert 'out_channels' in self.layer_config.keys()
            assert 'kernel_size' in self.layer_config.keys()
            if 'stride' not in self.layer_config.keys():
                self.layer_config['stride'] = 1
            if 'padding' not in self.layer_config.keys():
                self.layer_config['padding'] = 0
            if self.layer_config['kernel_size'] % 2 == 1:
                self.layer_list = nn.ModuleList([nn.Conv2d(
                    i, self.layer_config['out_channels'], self.layer_config['kernel_size'],
                    stride = self.layer_config['stride'], padding = self.layer_config['padding'], dilation = 1, groups = 1, bias = False
                    )
                    for i in in_channels_list])
            else:
                self.layer_list = nn.ModuleList([EvenKernelConv(
                    i, self.layer_config['out_channels'], self.layer_config['kernel_size'],
                    stride = self.layer_config['stride'], padding = self.layer_config['padding'], dilation = 1, groups = 1, bias = False
                    )
                    for i in in_channels_list])
            self.split_input = channel_N
        elif self.layer_config['type'] == 'fc':
            assert 'in_features' in self.layer_config.keys()
            complete_bar_num = self.layer_config['in_features'] // self.hardware_config['xbar_size']
            residual_col_num = self.layer_config['in_features'] % self.hardware_config['xbar_size']
            if residual_col_num > 0:
                in_features_list = [self.hardware_config['xbar_size']] * complete_bar_num + [residual_col_num]
            else:
                in_features_list = [self.hardware_config['xbar_size']] * complete_bar_num
            # generate Module List
            assert 'out_features' in self.layer_config.keys()
            self.layer_list = nn.ModuleList([nn.Linear(i, self.layer_config['out_features'], False) for i in in_features_list])
            self.split_input = self.hardware_config['xbar_size']
        else:
            assert 0, f'not support {self.layer_config["type"]}'
        # self.last_value = nn.Parameter(torch.ones(1))
        self.register_buffer('last_value', (-1) * torch.ones(1))
        # self.last_value[0] = 1
        # self.bit_scale_list = nn.Parameter(torch.FloatTensor([[9,1],[9,1],[9,1]]))
        self.register_buffer('bit_scale_list', torch.FloatTensor([
            [quantize_config['activation_bit'], -1],
            [quantize_config['weight_bit'], -1],
            [quantize_config['activation_bit'], -1]
        ]))
        # self.bit_scale_list[0, 0] = 9
        # self.bit_scale_list[0, 1] = 1
        # self.bit_scale_list[1, 0] = 9
        # self.bit_scale_list[1, 1] = 1
        # self.bit_scale_list[2, 0] = 9
        # self.bit_scale_list[2, 1] = 1
        # layer information
        self.layer_info = None
    def structure_forward(self, input):
        # TRADITION
        input_shape = input.shape
        input_list = torch.split(input, self.split_input, dim = 1)
        output = None
        for i in range(len(self.layer_list)):
            if i == 0:
                output = self.layer_list[i](input_list[i])
            else:
                output.add_(self.layer_list[i](input_list[i]))
        output_shape = output.shape
        # layer_info
        self.layer_info = collections.OrderedDict()
        if self.layer_config['type'] == 'conv':
            self.layer_info['type'] = 'conv'
            self.layer_info['Inputchannel'] = int(input_shape[1])
            self.layer_info['Inputsize'] = list(input_shape[2:])
            self.layer_info['Kernelsize'] = self.layer_config['kernel_size']
            self.layer_info['Stride'] = self.layer_config['stride']
            self.layer_info['Padding'] = self.layer_config['padding']
            self.layer_info['Outputchannel'] = int(output_shape[1])
            self.layer_info['Outputsize'] = list(output_shape[2:])
        elif self.layer_config['type'] == 'fc':
            self.layer_info['type'] = 'fc'
            self.layer_info['Infeature'] = int(input_shape[1])
            self.layer_info['Outfeature'] = int(output_shape[1])
        else:
            assert 0, f'not support {self.layer_config["type"]}'
        self.layer_info['Inputbit'] = int(self.bit_scale_list[0,0].item())
        self.layer_info['Weightbit'] = int(self.quantize_config['weight_bit'])
        self.layer_info['outputbit'] = int(self.quantize_config['activation_bit'])
        self.layer_info['row_split_num'] = len(self.layer_list)
        self.layer_info['weight_cycle'] = math.ceil((self.quantize_config['weight_bit'] - 1) / (self.hardware_config['weight_bit']))
        if 'input_index' in self.layer_config:
            self.layer_info['Inputindex'] = self.layer_config['input_index']
        else:
            self.layer_info['Inputindex'] = [-1]
        self.layer_info['Outputindex'] = [1]
        return output
    def forward(self, input, method = 'SINGLE_FIX_TEST', adc_action = 'SCALE'):
        METHOD = method
        # float method
        if METHOD == 'TRADITION':
            input_list = torch.split(input, self.split_input, dim = 1)
            output = None
            for i in range(len(self.layer_list)):
                if i == 0:
                    output = self.layer_list[i](input_list[i])
                else:
                    output.add_(self.layer_list[i](input_list[i]))
            return output
        # fix training
        if METHOD == 'FIX_TRAIN':
            weight = torch.cat([l.weight for l in self.layer_list], dim = 1)
            # quantize weight
            global last_weight_scale
            global last_activation_scale
            global last_weight_bit
            global last_activation_bit
            # last activation bit and scale
            self.bit_scale_list.data[0, 0] = last_activation_bit
            self.bit_scale_list.data[0, 1] = last_activation_scale
            if self.bypass_quantize_weight:
                self.bit_scale_list.data[1, 0] = self.quantize_config['weight_bit']
                assert self.bit_scale_list.data[1, 1] > 0
            else:
                weight = Quantize(weight, self.quantize_config['weight_bit'], 'weight', None, False)
                # weight bit and scale
                self.bit_scale_list.data[1, 0] = last_weight_bit
                self.bit_scale_list.data[1, 1] = last_weight_scale
            if self.layer_config['type'] == 'conv':
                output = F.conv2d(
                    input, weight, None, \
                    self.layer_config['stride'], self.layer_config['padding'], 1, 1
                )
            elif self.layer_config['type'] == 'fc':
                output = F.linear(input, weight, None)
            else:
                assert 0, f'not support {self.layer_config["type"]}'
            output = Quantize(output, self.quantize_config['activation_bit'], 'activation', self.last_value, self.training)
            # output activation bit and scale
            self.bit_scale_list.data[2, 0] = last_activation_bit
            self.bit_scale_list.data[2, 1] = last_activation_scale
            return output
        if METHOD == 'SINGLE_FIX_TEST':
            assert self.training == False
            bit_weights = self.get_bit_weights()
            output = self.set_weights_forward(input, bit_weights, adc_action)
            return output
        assert 0, f'not support {METHOD}'
    def get_bit_weights(self):
        # weight_bit = int(self.bit_scale_list[1, 0].item())
        weight_bit = self.quantize_config['weight_bit']
        weight_scale = self.bit_scale_list[1, 1].item()
        assert weight_bit != 0 and weight_scale != 0, f'weight bit and scale should be given by the params'
        bit_weights = collections.OrderedDict()
        for layer_num, l in enumerate(self.layer_list):
            # assert (weight_bit - 1) % self.hardware_config['weight_bit'] == 0, generate weight cycle
            weight_cycle = math.ceil((weight_bit - 1) / self.hardware_config['weight_bit'])
            # transfer part weight
            thres = 2 ** (weight_bit - 1) - 1
            weight_digit = torch.clamp(torch.round(l.weight / weight_scale), 0 - thres, thres - 0)
            # split weight into bit
            sign_weight = torch.sign(weight_digit)
            weight_digit = torch.abs(weight_digit)
            base = 1
            step = 2 ** self.hardware_config['weight_bit']
            for j in range(weight_cycle):
                tmp = torch.fmod(weight_digit, base * step) - torch.fmod(weight_digit, base)
                tmp = torch.mul(sign_weight, tmp)
                tmp = copy.deepcopy((tmp / base).detach().cpu().numpy())
                bit_weights[f'split{layer_num}_weight{j}_positive'] = np.where(tmp > 0, tmp, 0)
                bit_weights[f'split{layer_num}_weight{j}_negative'] = np.where(tmp < 0, -tmp, 0)
                base = base * step
        return bit_weights
    def set_weights_forward(self, input, bit_weights, adc_action):
        assert self.training == False
        output = None
        input_list = torch.split(input, self.split_input, dim = 1)
        scale = self.last_value.item()
        # weight_bit = int(self.bit_scale_list[1, 0].item())
        weight_bit = self.quantize_config['weight_bit']
        weight_scale = self.bit_scale_list[1, 1].item()
        for layer_num, l in enumerate(self.layer_list):
            # assert (weight_bit - 1) % self.hardware_config['weight_bit'] == 0, generate weight cycle
            weight_cycle = math.ceil((weight_bit - 1) / self.hardware_config['weight_bit'])
            weight_container = []
            base = 1
            step = 2 ** self.hardware_config['weight_bit']
            for j in range(weight_cycle):
                tmp = bit_weights[f'split{layer_num}_weight{j}_positive'] - bit_weights[f'split{layer_num}_weight{j}_negative']
                tmp = torch.from_numpy(tmp)
                weight_container.append(tmp.to(device = input.device, dtype = input.dtype))
                base = base * step
            activation_in_bit = int(self.bit_scale_list[0, 0].item())
            activation_in_scale = self.bit_scale_list[0, 1].item()
            thres = 2 ** (activation_in_bit - 1) - 1
            activation_in_digit = torch.clamp(torch.round(input_list[layer_num] / activation_in_scale), 0 - thres, thres - 0)
            # assert (activation_in_bit - 1) % self.hardware_config['input_bit'] == 0, generate activation_in cycle
            activation_in_cycle = math.ceil((activation_in_bit - 1) / self.hardware_config['input_bit'])
            # split activation into bit
            sign_activation_in = torch.sign(activation_in_digit)
            activation_in_digit = torch.abs(activation_in_digit)
            base = 1
            step = 2 ** self.hardware_config['input_bit']
            activation_in_container = []
            for i in range(activation_in_cycle):
                tmp = torch.fmod(activation_in_digit, base * step) -  torch.fmod(activation_in_digit, base)
                activation_in_container.append(torch.mul(sign_activation_in, tmp) / base)
                base = base * step
            # calculation and add
            point_shift = math.floor(self.quantize_config['point_shift'] + 0.5 * math.log2(len(self.layer_list)))
            Q = self.hardware_config['quantize_bit']
            for i in range(activation_in_cycle):
                for j in range(weight_cycle):
                    tmp = None
                    if self.layer_config['type'] == 'conv':
                        tmp = F.conv2d(
                            activation_in_container[i], weight_container[j], None, \
                            self.layer_config['stride'], self.layer_config['padding'], 1, 1
                        )
                    elif self.layer_config['type'] == 'fc':
                        tmp = F.linear(activation_in_container[i], weight_container[j], None)
                    else:
                        assert 0, f'not support {self.layer_config["type"]}'
                    if adc_action == 'SCALE':
                        tmp = tmp * weight_scale * activation_in_scale
                        tmp = tmp / scale * (2 ** ((activation_in_cycle - 1) * self.hardware_config['input_bit'] + \
                                                   (weight_cycle - 1) * self.hardware_config['weight_bit']))
                        transfer_point = point_shift + (Q - 1)
                        tmp = tmp * (2 ** transfer_point)
                        tmp = torch.clamp(torch.round(tmp), 1 - 2 ** (Q - 1), 2 ** (Q - 1) - 1)
                        tmp = tmp / (2 ** transfer_point)
                    elif adc_action == 'FIX':
                        # fix scale range
                        fix_scale_range = (2 ** self.hardware_config['input_bit'] - 1) * \
                                          (2 ** self.hardware_config['weight_bit'] - 1) * \
                                            self.hardware_config['xbar_size']
                        tmp = tmp / fix_scale_range * (2 ** (Q - 1))
                        tmp = torch.clamp(torch.round(tmp), 1 - 2 ** (Q - 1), 2 ** (Q - 1) - 1)
                        tmp = tmp * fix_scale_range / (2 ** (Q - 1))
                        tmp = tmp * weight_scale * activation_in_scale
                        tmp = tmp / scale * (2 ** ((activation_in_cycle - 1) * self.hardware_config['input_bit'] + \
                                                   (weight_cycle - 1) * self.hardware_config['weight_bit']))
                    else:
                        assert 0, f'can not support {adc_action}'
                    # scale
                    scale_point = (activation_in_cycle - 1 - i) * self.hardware_config['input_bit'] + \
                                  (weight_cycle - 1 - j) * self.hardware_config['weight_bit']
                    tmp = tmp / (2 ** scale_point)
                    # add
                    if torch.is_tensor(output):
                        output = output + tmp
                    else:
                        output = tmp
        # quantize output
        activation_out_bit = int(self.bit_scale_list[0, 0].item())
        activation_out_scale = self.bit_scale_list[0, 1].item()
        thres = 2 ** (activation_out_bit - 1) - 1
        output = torch.clamp(torch.round(output * thres), 0 - thres, thres - 0)
        output = output * scale / thres
        return output
    def extra_repr(self):
        return str(self.hardware_config) + ' ' + str(self.layer_config) + ' ' + str(self.quantize_config)
QuantizeLayerStr = ['conv', 'fc']

class ViewLayer(nn.Module):
    def __init__(self):
        super(ViewLayer, self).__init__()
    def forward(self, x):
        return x.view(x.size(0), -1)

class EleSumLayer(nn.Module):
    def __init__(self):
        super(EleSumLayer, self).__init__()
    def forward(self, x):
        return x[0] + x[1]

class DownSampleLayer(nn.Module):
    def __init__(self):
        super(DownSampleLayer,self).__init__()
    def forward(self,x):
        return nn.functional.interpolate(
            x, scale_factor=0.5, mode="area", recompute_scale_factor=True
        )

class HardTanhLayer(nn.Module):
    def __init__(self):
        super(HardTanhLayer,self).__init__()
    def forward(self,x):
        return nn.functional.hardtanh(x)

class ExpandLayer(nn.Module):
    def __init__(self,_max_channels):
        super(ExpandLayer,self).__init__()
        self._max_channels=_max_channels
    def forward(self,inputs):
        input_channels = inputs.size(1)
        pcd = (0, 0, 0, 0, 0, self._max_channels - input_channels)
        return nn.functional.pad(inputs, pcd, "constant", 0)
class ConcatLayer(nn.Module):
    def __init__(self):
        super().__init__()
    def forward(self, x):
        output_list = copy.deepcopy(x[0])
        for output in x[1:]:
            output_list = torch.cat((output_list,output), dim=1)
        return output_list
class StraightLayer(nn.Module):
    def __init__(self, hardware_config, layer_config, quantize_config):
        super(StraightLayer, self).__init__()
        # load hardware layer and quantize config in setting
        self.hardware_config = copy.deepcopy(hardware_config)
        self.layer_config = copy.deepcopy(layer_config)
        self.quantize_config = copy.deepcopy(quantize_config)
        # generate layer
        if self.layer_config['type'] == 'pooling':
            assert 'kernel_size' in self.layer_config.keys()
            assert 'stride' in self.layer_config.keys()
            if 'padding' not in self.layer_config.keys():
                self.layer_config['padding'] = 0
            if self.layer_config['mode'] == 'AVE':
                self.layer = nn.AvgPool2d(
                    kernel_size = self.layer_config['kernel_size'], \
                    stride = self.layer_config['stride'], \
                    padding = self.layer_config['padding']
                )
            elif self.layer_config['mode'] == 'MAX':
                self.layer = nn.MaxPool2d(
                    kernel_size = self.layer_config['kernel_size'], \
                    stride = self.layer_config['stride'], \
                    padding = self.layer_config['padding']
                )
            else:
                assert 0, f'not support {self.layer_config["mode"]}'
        elif self.layer_config['type'] == 'relu':
            self.layer = nn.ReLU()
        elif self.layer_config['type'] == 'view':
            self.layer = ViewLayer()
        elif self.layer_config['type'] == 'bn':
            self.layer = nn.BatchNorm2d(self.layer_config['features'])
        elif self.layer_config['type'] == 'dropout':
            self.layer = nn.Dropout()
        elif self.layer_config['type'] == 'element_sum':
            self.layer = EleSumLayer()
        elif self.layer_config['type'] == 'AdaptiveAvgPool2d':
            self.layer = nn.AdaptiveAvgPool2d(layer_config["output_size"])
        elif self.layer_config['type'] == 'flatten':
            self.layer = nn.Flatten(start_dim=layer_config["start_dim"], end_dim=layer_config["end_dim"])
        elif self.layer_config['type'] == 'hard_tanh':
            self.layer = nn.Hardtanh()
        elif self.layer_config['type'] == "expand":
            self.layer = ExpandLayer(layer_config["_max_channels"])
        elif self.layer_config['type'] == 'downsample':
            self.layer = DownSampleLayer()
        elif self.layer_config['type'] == 'concat':
            self.layer = ConcatLayer()
        else:
            assert 0, f'not support {self.layer_config["type"]}'
        # self.last_value = nn.Parameter(torch.ones(1))
        self.register_buffer('last_value', (-1) * torch.ones(1))
        # self.last_value[0] = 1
        self.layer_info = None
        if "quantize_flag" in layer_config.keys():
            self.quantize_flag = layer_config["quantize_flag"]
        else:
            if layer_config["type"] == "bn":
                self.quantize_flag = True
            else:
                self.quantize_flag = False
    def structure_forward(self, input):
        if self.layer_config['type'] != 'element_sum':
            # generate input shape and output shape
            self.input_shape = input.shape
            output = self.layer.forward(input)
            self.output_shape = output.shape
            # generate layer_info
            self.layer_info = collections.OrderedDict()
            if self.layer_config['type'] == 'pooling':
                self.layer_info['type'] = 'pooling'
                self.layer_info['Inputchannel'] = int(self.input_shape[1])
                self.layer_info['Inputsize'] = list(self.input_shape)[2:]
                self.layer_info['Kernelsize'] = self.layer_config['kernel_size']
                self.layer_info['Stride'] = self.layer_config['stride']
                self.layer_info['Padding'] = self.layer_config['padding']
                self.layer_info['Outputchannel'] = int(self.output_shape[1])
                self.layer_info['Outputsize'] = list(self.output_shape)[2:]
            elif self.layer_config['type'] == 'relu':
                self.layer_info['type'] = 'relu'
            elif self.layer_config['type'] == 'view':
                self.layer_info['type'] = 'view'
            elif self.layer_config['type'] == 'bn':
                self.layer_info['type'] = 'bn'
                self.layer_info['features'] = self.layer_config['features']
            elif self.layer_config['type'] == 'dropout':
                self.layer_info['type'] = 'dropout'
            elif self.layer_config['type'] == 'AdaptiveAvgPool2d':
                self.layer_info['type'] = 'AdaptiveAvgPool2d'
            elif self.layer_config['type'] == 'flatten':
                self.layer_info['type'] = 'flatten'
            elif self.layer_config['type'] == 'hard_tanh':
                self.layer_info['type'] = 'hard_tanh'
            elif self.layer_config['type']=='expand':
                self.layer_info['type'] = 'expand'
            elif self.layer_config["type"] == "downsample":
                self.layer_info["type"] = 'downsample'
            elif self.layer_config['type'] == 'concat':
                self.layer_info['type'] == 'concat'
            else:
                assert 0, f'not support {self.layer_config["type"]}'
        else:
            self.input_shape = (input[0].shape, input[1].shape)
            output = self.layer.forward(input)
            self.output_shape = output.shape
            self.layer_info = collections.OrderedDict()
            self.layer_info['type'] = 'element_sum'
        self.layer_info['Inputbit'] = self.quantize_config['activation_bit']
        self.layer_info['Weightbit'] = self.quantize_config['weight_bit']
        self.layer_info['outputbit'] = self.quantize_config['activation_bit']
        if 'input_index' in self.layer_config:
            self.layer_info['Inputindex'] = self.layer_config['input_index']
        else:
            self.layer_info['Inputindex'] = [-1]
        self.layer_info['Outputindex'] = [1]
        return output

    def forward(self, input, method = 'SINGLE_FIX_TEST', adc_action = 'SCALE'):
        # DOES NOT use method and adc_action, for unifying with QuantizeLayer
        METHOD = method
        # float method
        if METHOD == 'TRADITION':
            output = self.layer(input)
            return output
        # fix training and single fix test
        if METHOD == 'FIX_TRAIN' or METHOD == 'SINGLE_FIX_TEST':
            output = self.layer(input)
            if self.quantize_flag:
                output = Quantize(output, self.quantize_config['activation_bit'], 'activation', self.last_value, self.training)
            return output
        assert 0, f'not support {METHOD}'
    def get_bit_weights(self):
        return None
    def extra_repr(self):
        return str(self.hardware_config) + ' ' + str(self.layer_config) + ' ' + str(self.quantize_config)
StraightLayerStr = [
    'pooling', 'relu', 'view', 'bn', 'dropout',
    'element_sum', 'expand', 'AdaptiveAvgPool2d', 'downsample', 'flatten',
    'hard_tanh', 'concat'
]
class SplitInputLayer(nn.Module):
    def __init__(self, groups, in_channels):
        super().__init__()
        assert in_channels % groups == 0 
        self.split_input = int(in_channels / groups)
    def forward(self,x):
        input_list = torch.split(x, self.split_input, dim = 1)
        return input_list
class GroupLayer(nn.Module):
    def __init__(self, hardware_config, layer_config, quantize_config):
        super().__init__()
        self.hardware_config = copy.deepcopy(hardware_config)
        self.layer_config = copy.deepcopy(layer_config)
        self.quantize_config = copy.deepcopy(quantize_config)
        assert 'groups' in layer_config.keys()
        self.groups = self.layer_config['groups']
        assert 'in_channels' in self.layer_config.keys() and self.layer_config['in_channels'] % self.layer_config['groups'] == 0
        assert 'out_channels' in self.layer_config.keys() and self.layer_config['out_channels'] % self.layer_config['groups'] == 0
        self.layer_list = nn.ModuleList([SplitInputLayer(self.groups, self.layer_config['in_channels'])])
        conv_list = nn.ModuleList([])
        for group_layer_config in self.split_layer_config():
            conv_list.append(QuantizeLayer(self.hardware_config, group_layer_config, self.quantize_config))
        self.layer_list.append(conv_list)
        self.layer_list.append(ConcatLayer())
    def split_layer_config(self):
        self.group_in_channels = int(self.layer_config['in_channels'] / self.groups)
        self.group_out_channels = int(self.layer_config['out_channels'] / self.groups)
        group_layer_config_list = []
        for i in range(self.groups):
            group_layer_config_list.append(copy.deepcopy(self.layer_config))
            group_layer_config_list[-1]['in_channels'] = self.group_in_channels
            group_layer_config_list[-1]['out_channels'] = self.group_out_channels
            group_layer_config_list[-1]['type'] = 'conv'
        return group_layer_config_list
    def structure_forward(self,input):
        # generate input shape and output shape
        self.input_shape = input.shape
        output = self.forward(input, method = 'TRADITION')
        self.output_shape = output.shape
        # generate layer_info
        self.layer_info = collections.OrderedDict()
        if self.layer_config['type'] == 'group_conv':
            self.layer_info['type'] = 'conv'
            self.layer_info['Inputchannel'] = int(self.input_shape[1] / self.groups)
            self.layer_info['Inputsize'] = list(self.input_shape[2:])
            self.layer_info['Kernelsize'] = self.layer_config['kernel_size']
            self.layer_info['Stride'] = self.layer_config['stride']
            self.layer_info['Padding'] = self.layer_config['padding']
            self.layer_info['Outputchannel'] = int(self.output_shape[1])
            self.layer_info['Outputsize'] = list(self.output_shape[2:])
            self.layer_info['groups'] = self.groups
        else:
            assert 0,'unsupported type:{} in GroupLayer'.format(self.layer_config['type'])
        self.layer_info['Inputbit'] = int(self.layer_list[1][0].bit_scale_list[0,0].item())
        self.layer_info['Weightbit'] = int(self.layer_list[1][0].quantize_config['weight_bit'])
        self.layer_info['outputbit'] = int(self.layer_list[1][0].quantize_config['activation_bit'])
        self.layer_info['row_split_num'] = len(self.layer_list[1][0].layer_list)
        self.layer_info['weight_cycle'] = math.ceil((self.layer_list[1][0].quantize_config['weight_bit'] - 1) / (self.layer_list[1][0].hardware_config['weight_bit']))
        if 'input_index' in self.layer_config:
            self.layer_info['Inputindex'] = self.layer_config['input_index']
        else:
            self.layer_info['Inputindex'] = [-1]
        self.layer_info['Outputindex'] = [1]
        return output
    def get_bit_weights(self):
        bit_weights = []
        for model in self.layer_list[1]:
            bit_weights.append(model.get_bit_weights())
        return bit_weights
    def set_weights_forward(self, input, bit_weights, adc_action):
        input_list = self.layer_list[0](input)
        assert len(input_list) == len(bit_weights)
        output_list = []
        for i,model in enumerate(self.layer_list[1]):
            output_list.append(model.set_weights_forward(input = input_list[i], bit_weights = bit_weights[i], adc_action = adc_action))
        output = self.layer_list[2](output_list)
        return output
    def extra_repr(self):
        return str(self.hardware_config) + ' ' + str(self.layer_config) + ' ' + str(self.quantize_config)
    def forward(self, input, method = 'SINGLE_FIX_TEST', adc_action = 'SCALE'):
        METHOD = method
        # float method
        assert METHOD != 'FIX_TRAIN'
        input_list = self.layer_list[0](input)
        output_list = []
        for i,model in enumerate(self.layer_list[1]):
            output_list.append(model(input_list[i], method = method, adc_action = adc_action))
        output = self.layer_list[2](output_list)
        return output
    
GroupLayerStr = ['group_conv']

class EvenKernelConv(nn.Conv2d):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.padding_flag = bool(random.randint(0, 1))
    def forward(self, inputs):
        outputs = super().forward(inputs)
        # check for even kernel
        if self.kernel_size[0] % 2 == 0 and \
            self.padding[0] >= self.kernel_size[0] // 2:
            # fake output
            assert len(outputs.shape) == 4, \
                "The outputs should have 4 dim"
            if self.padding_flag:
                outputs = outputs[:, :, :-1, :-1]
            else:
                outputs = outputs[:, :, 1:, 1:]
        return outputs
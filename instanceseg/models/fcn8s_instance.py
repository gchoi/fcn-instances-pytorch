import os.path as osp
from collections import OrderedDict

from instanceseg.models.model_utils import copy_tensor, copy_conv, module_has_params, get_activations

try:
    import fcn
except ImportError:
    fcn = None
import torch
import torch.nn as nn
from instanceseg.utils import instance_utils

from instanceseg.models import model_utils
from graveyard.models import attention_old

DEFAULT_SAVED_MODEL_PATH = osp.expanduser('~/data/models/pytorch/fcn8s-instance.pth')

# TODO(allie): print out flops so you know how long things should take

# TODO(allie): Handle case when extra instances (or semantic segmentation) lands in channel 0

DEBUG = True

'''
        100 padding for 2 reasons:
            1) support very small input size
            2) allow cropping in order to match size of different layers' feature maps
        Note that the cropped part corresponds to a part of the 100 padding
        Spatial information of different layers' feature maps cannot be align exactly because of cropping, which is bad
'''


def isiterable(x):
    try:
        iter(x)
        is_iterable = True
    except TypeError:
        is_iterable = False
    return is_iterable


def get_default_sublayer_names(block_num, n_convs):
    default_names = []
    for c in range(n_convs):
        # default_names.append('convblock{}_conv{}'.format(block_num, c))
        # default_names.append('convblock{}_relu{}'.format(block_num, c))
        default_names.append('conv{}'.format(c))
        default_names.append('relu{}'.format(c))
    # default_names.append('pool{}'.format(block_num))
    default_names.append('pool')
    return default_names


def make_conv_block(in_channels, out_channels, n_convs=3, kernel_sizes: tuple = 3, stride=2,
                    paddings: tuple = 1, nonlinear_type=nn.ReLU, pool_type=nn.MaxPool2d, pool_size=2,
                    layer_names: list or bool=None, block_num=None):
    if layer_names is None:
        layer_names = True
    if layer_names is True:
        assert block_num is not None, 'I need the block number to create a default sublayer name'
        layer_names = get_default_sublayer_names(block_num, n_convs)

    paddings_list = paddings if isiterable(paddings) else [paddings for _ in range(n_convs)]
    kernel_sizes_list = kernel_sizes if isiterable(kernel_sizes) else [kernel_sizes for _ in range(n_convs)]
    in_c = in_channels
    layers = []
    for c in range(n_convs):
        layers.append(nn.Conv2d(in_c, out_channels, kernel_size=kernel_sizes_list[c], padding=paddings_list[c]))
        layers.append(nonlinear_type(inplace=True))
        in_c = out_channels

    layers.append(pool_type(kernel_size=pool_size, stride=stride, ceil_mode=True))
    if layer_names is False:
        return nn.Sequential(*layers)
    else:
        assert len(layer_names) == len(layers)
        layers_with_names = [(name, layer) for name, layer in zip(layer_names, layers)]
        ordered_layers_with_names = OrderedDict(layers_with_names)
        return nn.Sequential(ordered_layers_with_names)


def initialize_basic_conv_from_sublayers(basic_conv, conv_sublayers):
    pass


class FCN8sInstance(nn.Module):
    INTERMEDIATE_CONV_CHANNEL_SIZE = 20

    def __init__(self, n_instance_classes=None, semantic_instance_class_list=None, map_to_semantic=False,
                 include_instance_channel0=False, bottleneck_channel_capacity=None, score_multiplier_init=None,
                 at_once=True, n_input_channels=3, clip=None, use_conv8=False, use_attention_layer=False):
        """
        n_classes: Number of output channels
        map_to_semantic: If True, n_semantic_classes must not be None.
        include_instance_channel0: If True, extras are placed in instance channel 0 for each semantic class (otherwise
        we don't allocate space for a channel like this)
        bottleneck_channel_capacity: n_classes (default); 'semantic': n_semantic_classes', some number
        """
        super(FCN8sInstance, self).__init__()

        if include_instance_channel0:
            raise NotImplementedError
        if semantic_instance_class_list is None:
            assert n_instance_classes is not None, \
                ValueError('either n_classes or semantic_instance_class_list must be specified.')
            assert not map_to_semantic, ValueError('need semantic_instance_class_list to map to semantic')
        else:
            assert n_instance_classes is None or n_instance_classes == len(semantic_instance_class_list)
            n_instance_classes = len(semantic_instance_class_list)

        if semantic_instance_class_list is None:
            self.semantic_instance_class_list = list(range(n_instance_classes))
        else:
            self.semantic_instance_class_list = semantic_instance_class_list
        self.n_instance_classes = n_instance_classes
        self.at_once = at_once
        self.map_to_semantic = map_to_semantic
        self.score_multiplier_init = score_multiplier_init
        self.instance_to_semantic_mapping_matrix = \
            instance_utils.get_instance_to_semantic_mapping_from_sem_inst_class_list(
                self.semantic_instance_class_list, as_numpy=False, compose_transposed=True)
        self.n_semantic_classes = self.instance_to_semantic_mapping_matrix.size(0)
        self.n_output_channels = n_instance_classes if not map_to_semantic else self.n_semantic_classes
        self.n_input_channels = n_input_channels
        self.activations = None
        self.activation_layers = []
        self.my_forward_hooks = {}
        self.use_attention_layer = use_attention_layer
        self.clip = clip
        self.use_conv8 = use_conv8

        if bottleneck_channel_capacity is None:
            self.bottleneck_channel_capacity = self.n_instance_classes
        elif isinstance(bottleneck_channel_capacity, str):
            assert bottleneck_channel_capacity == 'semantic', ValueError('Did not recognize '
                                                                         'bottleneck_channel_capacity {}')
            self.bottleneck_channel_capacity = self.n_semantic_classes
        else:
            assert bottleneck_channel_capacity == int(bottleneck_channel_capacity), ValueError(
                'bottleneck_channel_capacity must be an int')
            self.bottleneck_channel_capacity = int(bottleneck_channel_capacity)

        self.conv1 = make_conv_block(self.n_input_channels, 64, n_convs=2, paddings=(100, 1), block_num=1)  # 1/2
        self.conv2 = make_conv_block(64, 128, n_convs=2, block_num=2)  # 1/4
        self.conv3 = make_conv_block(128, 256, n_convs=3, block_num=3)  # 1/8
        self.conv4 = make_conv_block(256, 512, n_convs=3, block_num=4)  # 1/16
        self.conv5 = make_conv_block(512, 512, n_convs=3, block_num=5)  # 1/32

        # fc6
        self.fc6 = nn.Conv2d(512, 4096, 7)
        self.relu6 = nn.ReLU(inplace=True)
        self.drop6 = nn.Dropout2d()

        # fc7
        self.fc7 = nn.Conv2d(4096, 4096, kernel_size=1)  # H/32 x W/32 x 4096
        self.relu7 = nn.ReLU(inplace=True)
        self.drop7 = nn.Dropout2d()

        # H/32 x W/32 x n_semantic_cls
        intermediate_channel_size = self.bottleneck_channel_capacity if not self.use_conv8 else \
            self.INTERMEDIATE_CONV_CHANNEL_SIZE
        if self.use_conv8:
            self.conv8 = nn.Conv2d(4096, intermediate_channel_size, kernel_size=3, padding=1)
            fr_in_dim = intermediate_channel_size
        else:
            fr_in_dim = 4096
        if self.use_attention_layer:
            self.attn1 = attention_old.Self_Attn(in_dim=fr_in_dim, activation='relu')
            # fr_in_dim = self.attn1.out_dim

        self.score_fr = nn.Conv2d(fr_in_dim, intermediate_channel_size, kernel_size=1)

        # H/32 x W/32 x n_semantic_cls
        self.score_pool3 = nn.Conv2d(256, self.bottleneck_channel_capacity, 1)
        # H/32 x W/32 x n_semantic_cls
        self.score_pool4 = nn.Conv2d(512, self.bottleneck_channel_capacity, 1)
        # Note: weight tensor is [N, 512, 1, 1]

        bc, n_inst = self.bottleneck_channel_capacity, self.n_instance_classes
        self.upscore2 = nn.ConvTranspose2d(bc, bc, kernel_size=4, stride=2, bias=False)  # H/16 x W/16 x channels
        self.upscore_pool4 = nn.ConvTranspose2d(bc, bc, kernel_size=4, stride=2, bias=False)  # H x W x channels
        self.upscore8 = nn.ConvTranspose2d(bc, n_inst, kernel_size=16, stride=8, bias=False)  # H/2 x W/2 x out_chn
        self.score_multiplier1x1 = None if self.score_multiplier_init is None else \
            nn.Conv2d(n_inst, n_inst, kernel_size=1, stride=1, bias=True)
        self.clipping_function = None if self.clip is None else \
            model_utils.get_clipping_function(min=-self.clip, max=self.clip)

        self.conv1x1_instance_to_semantic = None if not self.map_to_semantic else \
            nn.Conv2d(in_channels=self.n_instance_classes, out_channels=self.n_output_channels, kernel_size=1,
                      bias=False)

        self._initialize_weights()

    def forward(self, x):
        h = x
        h = self.conv1(h)  # 1/2
        h = self.conv2(h)  # 1/4
        h = self.conv3(h)  # 1/8
        pool3 = h
        h = self.conv4(h)  # 1/16
        pool4 = h
        h = self.conv5(h)  # 1/32

        h = self.relu6(self.fc6(h))
        h = self.drop6(h)

        h = self.relu7(self.fc7(h))
        h = self.drop7(h)

        if self.use_conv8:
            h = self.conv8(h)

        if self.use_attention_layer:
            h, p1 = self.attn1(h)

        h = self.score_fr(h)

        h = self.upscore2(h)  # ConvTranspose2d, stride=2
        upscore2 = h  # 1/16

        if self.at_once:
            h = self.score_pool4(pool4 * 0.01)
        else:
            h = self.score_pool4(pool4)
        h = h[:, :, 5:5 + upscore2.size()[2], 5:5 + upscore2.size()[3]]
        score_pool4c = h  # 1/16

        h = upscore2 + score_pool4c  # 1/16
        h = self.upscore_pool4(h)  # ConvTranspose2d, stride=2
        upscore_pool4 = h  # 1/8

        if self.at_once:
            h = self.score_pool3(pool3 * 0.0001)
        else:
            h = self.score_pool3(pool3)
        h = h[:, :,
            9:9 + upscore_pool4.size()[2],
            9:9 + upscore_pool4.size()[3]]
        score_pool3c = h  # 1/8

        h = upscore_pool4 + score_pool3c  # 1/8

        h = self.upscore8(h)

        if self.score_multiplier_init:
            h = self.score_multiplier1x1(h)

        if self.clipping_function is not None:
            h = self.clipping_function(h)

        if self.map_to_semantic:
            h = self.conv1x1_instance_to_semantic(h)

        h = h[:, :, 31:31 + x.size()[2], 31:31 + x.size()[3]].contiguous()

        return h

    def copy_params_from_fcn8s(self, fcn16s):
        raise NotImplementedError('function not yet adapted for instance rather than semantic networks (gotta copy '
                                  'weights to each instance from the same semantic class)')

    def _initialize_weights(self):
        num_modules = len(list(self.modules()))
        for idx, m in enumerate(self.modules()):
            if self.map_to_semantic and idx == num_modules - 1:
                assert m == self.conv1x1_instance_to_semantic
                copy_tensor(src=self.instance_to_semantic_mapping_matrix.view(self.n_instance_classes,
                                                                              self.n_semantic_classes, 1, 1),
                            dest=self.conv1x1_instance_to_semantic.weight.data)
                self.conv1x1_instance_to_semantic.weight.requires_grad = False  # Fix weights
            elif isinstance(m, nn.Conv2d):
                m.weight.data.zero_()
                # m.weight.data.normal_(0.0, 0.02)
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.ConvTranspose2d):
                assert m.kernel_size[0] == m.kernel_size[1]
                if m.in_channels == m.out_channels:
                    initial_weight = model_utils.get_upsampling_weight(
                        m.in_channels, m.out_channels, m.kernel_size[0])
                else:
                    initial_weight = model_utils.get_non_symmetric_upsampling_weight(
                        m.in_channels, m.out_channels, m.kernel_size[0],
                        semantic_instance_class_list=self.semantic_instance_class_list)
                copy_tensor(src=initial_weight, dest=m.weight.data)
        if self.score_multiplier_init:
            self.score_multiplier1x1.weight.data.zero_()
            for ch in range(self.score_multiplier1x1.weight.size(1)):
                self.score_multiplier1x1.weight.data[ch, ch] = self.score_multiplier_init
            self.score_multiplier1x1.bias.data.zero_()

    def copy_params_from_vgg16(self, vgg16):
        features = []
        for conv_block in [self.conv1, self.conv2, self.conv3, self.conv4, self.conv5]:
            features += list(conv_block.children())

        self.copy_from_vgg16_to_modules(features, vgg16)

    def copy_from_vgg16_to_modules(self, features, vgg16):
        for l1, l2 in zip(vgg16.features, features):
            if isinstance(l1, nn.Conv2d) and isinstance(l2, nn.Conv2d):
                if l2 == self.conv1[0] and self.n_input_channels != 3:  # accomodate different input size
                    assert self.n_input_channels > 3, NotImplementedError('Only know how to initialize with # '
                                                                          'input channels >= 3')
                    copy_tensor(src=l1.weight.data, dest=l2.weight.data[:, :3, ...])
                    copy_tensor(src=l1.bias.data, dest=l2.bias.data)
                else:
                    copy_conv(src_conv_module=l1, dest_conv_module=l2)
        for i, name in zip([0, 3], ['fc6', 'fc7']):
            l1 = vgg16.classifier[i]
            l2 = getattr(self, name)
            l2.weight.data.copy_(l1.weight.data.view(l2.weight.size()))
            l2.bias.data.copy_(l1.bias.data.view(l2.bias.size()))

    def copy_params_from_semantic_equivalent_of_me(self, semantic_model):
        if self.bottleneck_channel_capacity != self.n_semantic_classes:
            conv2d_with_repeated_channels = ['score_fr', 'score_pool3', 'score_pool4']
            conv2dT_with_repeated_channels = ['upscore2', 'upscore8', 'upscore_pool4']
        else:
            conv2d_with_repeated_channels = []
            conv2dT_with_repeated_channels = ['upscore8']
        module_types_to_ignore = [nn.ReLU, nn.MaxPool2d, nn.Dropout2d]
        module_names_to_ignore = ['score_multiplier1x1']
        # check whether this has the right number of channels to be the semantic version of me
        assert self.semantic_instance_class_list is not None, ValueError('I must know which semantic classes each of '
                                                                         'my instance channels map to in order to '
                                                                         'copy weights.')
        n_semantic_classes = self.n_semantic_classes
        last_layer_name = 'upscore8'
        last_features = getattr(semantic_model, last_layer_name)
        if last_features.weight.size(1) != n_semantic_classes:
            raise ValueError('The semantic model I tried to copy from has {} output channels, but I need {} channels '
                             'for each of my semantic classes'.format(last_features.weight.size(1), n_semantic_classes))
        copy_modules_from_semantic_to_instance(self, semantic_model, conv2dT_with_repeated_channels,
                                               conv2d_with_repeated_channels, module_names_to_ignore,
                                               module_types_to_ignore, n_semantic_classes,
                                               self.semantic_instance_class_list)

        # Assert that all the weights equal each other
        if DEBUG:
            assert_successful_copy_from_semantic_model(self, semantic_model, self.semantic_instance_class_list,
                                                       conv2dT_with_repeated_channels,
                                                       conv2d_with_repeated_channels, module_names_to_ignore)

        if self.map_to_semantic:
            self.conv1x1_instance_to_semantic = nn.Conv2d(in_channels=self.n_instance_classes,
                                                          out_channels=self.n_semantic_classes,
                                                          kernel_size=1, bias=False)

    def store_activation(self, layer, input, output, layer_name):
        if layer_name not in self.activation_layers:
            self.activation_layers.append(layer_name)
        if self.activations is None:
            self.activations = {}
        self.activations[layer_name] = output.data

    def get_activations(self, input, layer_names):
        return get_activations(self, input, layer_names)


def FCN8sInstancePretrained(model_file=DEFAULT_SAVED_MODEL_PATH, n_instance_classes=21,
                            semantic_instance_class_list=None, map_to_semantic=False):
    model = FCN8sInstance(n_instance_classes=n_instance_classes,
                          semantic_instance_class_list=semantic_instance_class_list,
                          map_to_semantic=map_to_semantic, at_once=True)
    # state_dict = torch.load(model_file, map_location=lambda storage, location: 'cpu')
    state_dict = torch.load(model_file, map_location=lambda storage, loc: storage)[
        'model_state_dict']
    model.load_state_dict(state_dict)
    return model


def assert_successful_copy_from_semantic_model(instance_model, semantic_model,
                                               semantic_instance_class_list, conv2dT_with_repeated_channels,
                                               conv2d_with_repeated_channels, module_names_to_ignore):
    successfully_copied_modules = []
    unsuccessfully_copied_modules = []
    for module_name, my_module in instance_model.named_children():
        if module_name in module_names_to_ignore:
            import ipdb;
            ipdb.set_trace()
            continue
        module_to_copy = getattr(semantic_model, module_name)
        for i, (my_p, p_to_copy) in enumerate(
                zip(my_module.named_parameters(), module_to_copy.named_parameters())):
            assert my_p[0] == p_to_copy[0]
            if torch.equal(my_p[1].data, p_to_copy[1].data):
                successfully_copied_modules.append(module_name + ' ' + str(i))
                continue
            else:
                if module_name in (conv2d_with_repeated_channels + conv2dT_with_repeated_channels):
                    are_equal = True
                    for inst_cls, sem_cls in enumerate(semantic_instance_class_list):
                        are_equal = torch.equal(my_p[1].data[:, inst_cls, :, :],
                                                p_to_copy[1].data[:, sem_cls, :, :])
                        if not are_equal:
                            break
                    if are_equal:
                        successfully_copied_modules.append(module_name + ' ' + str(i))
                    else:
                        unsuccessfully_copied_modules.append(module_name + ' ' + str(i))
    if len(unsuccessfully_copied_modules) > 0:
        raise Exception('modules were not copied correctly: {}'.format(unsuccessfully_copied_modules))
    else:
        print('All modules copied correctly: {}'.format(successfully_copied_modules))


def copy_modules_from_semantic_to_instance(instance_model_dest, semantic_model, conv2dT_with_repeated_channels,
                                           conv2d_with_repeated_channels, module_names_to_ignore,
                                           module_types_to_ignore, n_semantic_classes, semantic_instance_class_list):
    for module_name, my_module in instance_model_dest.named_children():
        if module_name in module_names_to_ignore:
            continue
        module_to_copy = getattr(semantic_model, module_name)
        if module_name in conv2d_with_repeated_channels:
            for p_name, my_p in my_module.named_parameters():
                p_to_copy = getattr(module_to_copy, p_name)
                if not all(my_p.size()[c] == p_to_copy.size()[c] for c in range(1, len(my_p.size()))):
                    import ipdb;
                    ipdb.set_trace()
                    raise ValueError('semantic model is formatted incorrectly at layer {}'.format(module_name))
                if DEBUG:
                    assert my_p.data.size(0) == len(semantic_instance_class_list) \
                           and p_to_copy.data.size(0) == n_semantic_classes
                for inst_cls, sem_cls in enumerate(semantic_instance_class_list):
                    # weird formatting because scalar -> scalar not implemented (must be FloatTensor,
                    # so we use slicing)
                    n_instances_this_class = float(sum(
                        [1 if sic == sem_cls else 0 for sic in semantic_instance_class_list]))
                    copy_tensor(src=p_to_copy.data[sem_cls:(sem_cls + 1), ...] / n_instances_this_class,
                                dest=my_p.data[inst_cls:(inst_cls + 1), ...])
        elif module_name in conv2dT_with_repeated_channels:
            assert isinstance(module_to_copy, nn.ConvTranspose2d)
            # assert l1.weight.size() == l2.weight.size()
            # assert l1.bias.size() == l2.bias.size()
            for p_name, my_p in my_module.named_parameters():
                p_to_copy = getattr(module_to_copy, p_name)
                if not all(my_p.size()[c] == p_to_copy.size()[c]
                           for c in [0] + list(range(2, len(p_to_copy.size())))):
                    import ipdb;
                    ipdb.set_trace()
                    raise ValueError('semantic model formatted incorrectly for repeating params.')

                for inst_cls, sem_cls in enumerate(semantic_instance_class_list):
                    # weird formatting because scalar -> scalar not implemented (must be FloatTensor,
                    # so we use slicing)
                    copy_tensor(src=p_to_copy.data[:, sem_cls:(sem_cls + 1), ...],
                                dest=my_p.data[:, inst_cls:(inst_cls + 1), ...])
        elif isinstance(my_module, nn.Conv2d) or isinstance(my_module, nn.ConvTranspose2d):
            assert type(module_to_copy) == type(my_module)
            for p_name, my_p in my_module.named_parameters():
                p_to_copy = getattr(module_to_copy, p_name)
                if not my_p.size() == p_to_copy.size():
                    import ipdb;
                    ipdb.set_trace()
                    raise ValueError('semantic model is formatted incorrectly at layer {}'.format(module_name))
                copy_tensor(src=p_to_copy.data, dest=my_p.data)
                assert torch.equal(my_p.data, p_to_copy.data)
        elif any([isinstance(my_module, type) for type in module_types_to_ignore]):
            continue
        else:
            if not module_has_params(my_module):
                print('Skipping module of type {} (name: {}) because it has no params.  But please place it in '
                      'list of module types to not copy.'.format(type(my_module), my_module))
                continue
            else:
                raise Exception('Haven''t handled copying of {}, of type {}'.format(module_name, type(my_module)))

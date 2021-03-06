import numpy as np
import torch
from torch import nn


class InstanceProblemConfig(object):
    """
    Used for both models and datasets to lay out the assumptions of each during instance
    segmentation.

    For models: Specifies the max # of instances of each semantic class it will attempt to produce.

    For datasets: Specifies the max # of instances of each class that appear in the
    (training) dataset.
    """

    def __init__(self, n_instances_by_semantic_id, semantic_class_names=None, semantic_vals=None, void_value=-1,
                 include_instance_channel0=False, map_to_semantic=False):
        """
        For semantic, include_instance_channel0=True
        n_instances_by_semantic_id = [0, 0, ..]
        """
        if semantic_vals is not None:
            assert len(semantic_vals) == len(n_instances_by_semantic_id)
        assert n_instances_by_semantic_id is not None, ValueError

        self.map_to_semantic = map_to_semantic
        self.semantic_class_names = semantic_class_names
        self.semantic_vals = semantic_vals or range(len(n_instances_by_semantic_id))
        self.void_value = void_value
        self.include_instance_channel0 = include_instance_channel0
        self.n_instances_by_semantic_id = n_instances_by_semantic_id \
            if not map_to_semantic else [1 for _ in n_instances_by_semantic_id]
        self.model_n_instances_by_semantic_id = n_instances_by_semantic_id \
            if not map_to_semantic else [1 for _ in n_instances_by_semantic_id]

        # Some derivative stuff
        self.model_semantic_instance_class_list = get_semantic_instance_class_list(n_instances_by_semantic_id)
        self.semantic_instance_class_list = get_semantic_instance_class_list(self.n_instances_by_semantic_id)

        # Some more derivative stuff
        self.n_semantic_classes = len(self.semantic_vals)
        self.n_classes = len(self.model_semantic_instance_class_list) \
            if not map_to_semantic else self.n_semantic_classes

        # Compute stuff dependent on whether or not we're converting the problem to semantic rather than instance
        self.sem_ids_by_instance_id = [id_into_sem_vals for
                                       id_into_sem_vals, n_inst in
                                       enumerate(self.n_instances_by_semantic_id) for _ in range(n_inst)]

        self.instance_count_id_list = get_instance_count_id_list(self.semantic_instance_class_list,
                                                                 include_channel0=self.include_instance_channel0)
        self.model_instance_count_id_list = get_instance_count_id_list(self.model_semantic_instance_class_list,
                                                                       include_channel0=self.include_instance_channel0)
        self.instance_to_semantic_mapping_matrix = get_instance_to_semantic_mapping(
            self.model_n_instances_by_semantic_id)
        self.instance_to_semantic_conv1x1 = nn.Conv2d(in_channels=len(self.model_semantic_instance_class_list),
                                                      out_channels=self.n_semantic_classes,
                                                      kernel_size=1, bias=False)

    @staticmethod
    def _get_channel_labels(semantic_instance_class_list, instance_count_id_list, class_names, map_to_semantic,
                            sem_inst_format):
        if class_names is None:
            semantic_instance_labels = semantic_instance_class_list
        else:
            semantic_instance_labels = [class_names[c] for c in semantic_instance_class_list]
        if map_to_semantic:
            channel_labels = [sem_inst_format.format(sem_cls, '') for sem_cls, inst_id in zip(
                semantic_instance_labels, instance_count_id_list)]
        else:
            channel_labels = [sem_inst_format.format(sem_cls, int(inst_id)) for sem_cls, inst_id in zip(
                semantic_instance_labels, instance_count_id_list)]
        return channel_labels

    def get_channel_labels(self, sem_inst_format='{}_{}'):
        return self._get_channel_labels(self.semantic_instance_class_list, self.instance_count_id_list,
                                        self.semantic_class_names, map_to_semantic=self.map_to_semantic,
                                        sem_inst_format=sem_inst_format)

    def get_model_channel_labels(self, sem_inst_format='{}_{}'):
        return self._get_channel_labels(self.model_semantic_instance_class_list, self.model_instance_count_id_list,
                                        self.semantic_class_names, map_to_semantic=False, sem_inst_format=sem_inst_format)

    def set_class_names(self, class_names):
        assert class_names is None or (len(class_names) == self.n_semantic_classes)
        self.semantic_class_names = class_names

    def decouple_instance_result(self, instance_scores):
        # TODO(allie): implement.
        raise NotImplementedError


def combine_semantic_and_instance_labels(sem_lbl, inst_lbl, semantic_instance_class_list, instance_count_id_list,
                                         set_extras_to_void=True, void_value=-1):
    """
    sem_lbl is size(img); inst_lbl is size(img).  inst_lbl is just the original instance
    image (inst_lbls at coordinates of person 0 are 0)
    """
    # TODO(allie): handle class overflow (from ground truth)
    assert set_extras_to_void == True, NotImplementedError
    assert sem_lbl.shape == inst_lbl.shape
    if torch.is_tensor(inst_lbl):
        y = inst_lbl.clone()
    else:
        y = inst_lbl.copy()
    y[...] = void_value
    unique_semantic_vals, inst_counts = np.unique(semantic_instance_class_list, return_counts=True)
    for sem_val, n_instances_for_this_sem_cls in zip(unique_semantic_vals, inst_counts):
        sem_inst_idxs = [i for i, s in enumerate(semantic_instance_class_list) if s == sem_val]
        for sem_inst_idx in sem_inst_idxs:
            inst_val = instance_count_id_list[sem_inst_idx]
            try:
                y[(sem_lbl == int(sem_val)) * (inst_lbl == int(inst_val))] = sem_inst_idx
            except:
                import ipdb; ipdb.set_trace()
                raise
    return y


def get_semantic_instance_class_list(n_channels_by_semantic_id):
    """
    Example:
        input: [1, 3, 3, 3]
        returns: [0, 1, 1, 1, 2, 2, 2, 3, 3, 3]
    """

    return [sem_cls for sem_cls, n_channels in enumerate(n_channels_by_semantic_id)
            for _ in range(n_channels)]


def get_instance_count_id_list(semantic_instance_class_list, non_instance_sem_classes=(0,), include_channel0=False):
    """
    Example:
        input: [0, 1, 1, 1, 2, 2, 2, 3, 3, 3]
        non_instance_sem_classes=(0,)  # (background class gets inst channel label 0)
            Returns:
                if include_channel0=False:
                    [0, 1, 2, 3, 1, 2, 3, 1, 2, 3]
                if include_channel0=True:
                    [0, 0, 1, 2, 0, 1, 2, 0, 1, 2]

    """
    semantic_instance_class_array = np.array(semantic_instance_class_list)
    unique_semantic_classes = np.unique(semantic_instance_class_array)
    instance_count_id_arr = np.empty((len(semantic_instance_class_list),))
    for sem_cls in unique_semantic_classes:
        sem_cls_locs = semantic_instance_class_array == sem_cls
        if sem_cls in list(non_instance_sem_classes):
            assert sum(sem_cls_locs) == 1
            instance_count_id_arr[sem_cls_locs] = 0
        else:
            instance_count_id_arr[sem_cls_locs] = np.arange(sem_cls_locs.sum()) + (0 if include_channel0 else 1)
    return instance_count_id_arr.astype(int).tolist()


def get_instance_to_semantic_mapping_from_sem_inst_class_list(semantic_instance_class_list,
                                                              as_numpy=False, compose_transposed=True):
    """
    returns a binary matrix, where semantic_instance_mapping is N x S
    (N = # instances, S = # semantic classes)
    semantic_instance_mapping[inst_idx, :] is a one-hot vector,
    and semantic_instance_mapping[inst_idx, sem_idx] = 1 iff that instance idx is an instance
    of that semantic class.
    compose_transposed: S x N
    """
    n_instance_classes = len(semantic_instance_class_list)
    n_semantic_classes = int(max(semantic_instance_class_list) + 1)
    if not compose_transposed:
        instance_to_semantic_mapping_matrix = torch.zeros((n_instance_classes, n_semantic_classes)).float()
        for instance_idx, semantic_idx in enumerate(semantic_instance_class_list):
            instance_to_semantic_mapping_matrix[instance_idx, semantic_idx] = 1
    else:
        instance_to_semantic_mapping_matrix = torch.zeros((n_semantic_classes, n_instance_classes)).float()
        for instance_idx, semantic_idx in enumerate(semantic_instance_class_list):
            instance_to_semantic_mapping_matrix[semantic_idx, instance_idx] = 1
    return instance_to_semantic_mapping_matrix if not as_numpy else \
        instance_to_semantic_mapping_matrix.numpy()


def get_instance_to_semantic_mapping(n_instances_by_semantic_id, as_numpy=False):
    """
    returns a binary matrix, where semantic_instance_mapping is N x S
    (N = # instances, S = # semantic classes)
    semantic_instance_mapping[inst_idx, :] is a one-hot vector,
    and semantic_instance_mapping[inst_idx, sem_idx] = 1 iff that instance idx is an instance
    of that semantic class.
    """
    semantic_instance_class_list = get_semantic_instance_class_list(n_instances_by_semantic_id)
    return get_instance_to_semantic_mapping_from_sem_inst_class_list(semantic_instance_class_list, as_numpy)


def permute_scores(score, pred_permutations):
    score_permuted_to_match = score.clone()
    for ch in range(score.size(1)):  # NOTE(allie): iterating over channels, but maybe should iterate over
        # batch size?
        score_permuted_to_match[:, ch, :, :] = score[:, pred_permutations[:, ch], :, :]
    return score_permuted_to_match


def permute_labels(label_preds, permutations):
    if torch.is_tensor(label_preds):
        label_preds_permuted = label_preds.clone()
    else:
        label_preds_permuted = label_preds.copy()
    for idx in range(permutations.shape[0]):
        permutation = permutations[idx, :]
        for new_channel, old_channel in enumerate(permutation):
            label_preds_permuted[label_preds == old_channel] = new_channel
    return label_preds_permuted

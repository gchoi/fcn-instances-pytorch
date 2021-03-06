from instanceseg.datasets import dataset_generator_registry
import instanceseg.utils.scripts
from scripts.configurations import synthetic_cfg
from scripts.configurations import voc_cfg
import numpy as np
from instanceseg.utils import datasets


def is_lr_ordered(sem_lbl, inst_lbl):
    unique_sem_lbls = np.unique(sem_lbl)
    sem_cls_preordered = []
    ordering_correct = True
    for sem_val in unique_sem_lbls[unique_sem_lbls > 0]:
        unique_instance_idxs = sorted(np.unique(inst_lbl[sem_lbl == sem_val]))
        assert 0 not in unique_instance_idxs

        ordered_coms = []
        for inst_val in unique_instance_idxs:
            com = datasets.compute_centroid_binary_mask(np.logical_and(sem_lbl == sem_val,
                                                                       inst_lbl == inst_val))
            ordered_coms.append(com)
        ordered_left_right_ordering = [x for x in np.argsort([com[1] for com in ordered_coms])]

        # Assert that they're actually in order
        sem_cls_preordered.append(all([x == y for x, y in zip(ordered_left_right_ordering, list(range(len(
            ordered_left_right_ordering))))]))
        if not all([x == y for x, y in zip(ordered_left_right_ordering, list(range(len(
                ordered_left_right_ordering))))]):
            ordering_correct = False
            break
    return ordering_correct


def test_lr_of_dataset(dataset_name):
    print('Getting datasets')
    if dataset_name == 'voc':
        cfg = voc_cfg.get_default_config()
        # unordered
        cfg['ordering'] = None
        instanceseg.utils.scripts.set_random_seeds()
        train_dataset_unordered, _ = dataset_generator_registry.get_dataset('voc', cfg)

        # ordered
        cfg['ordering'] = 'LR'
        instanceseg.utils.scripts.set_random_seeds()
        train_dataset_ordered, _ = dataset_generator_registry.get_dataset('voc', cfg)
    elif dataset_name == 'synthetic':
        cfg = synthetic_cfg.get_default_config()
        # unordered
        cfg['ordering'] = None
        instanceseg.utils.scripts.set_random_seeds()
        train_dataset_unordered, _ = dataset_generator_registry.get_dataset('synthetic', cfg)

        # ordered
        cfg['ordering'] = 'LR'
        instanceseg.utils.scripts.set_random_seeds()
        train_dataset_ordered, _ = dataset_generator_registry.get_dataset('synthetic', cfg)
    else:
        raise ValueError

    print('Testing right-left ordering...')
    test_lr_from_datasets(train_dataset_unordered, train_dataset_ordered)


def test_lr_from_datasets(unordered_dataset, ordered_dataset):
    # Make sure unordered dataset is actually unordered
    num_unordered = 0
    for i, (sem_lbl, inst_lbl) in unordered_dataset:
        if not is_lr_ordered(sem_lbl, inst_lbl):
            num_unordered += 1

    if num_unordered == 0:
        raise Exception('All images were ordered before asserting LR ordering.  Can\'t verify ordering worked.')
    else:
        print('{}/{} images were already left-right ordered'.format(len(unordered_dataset) - num_unordered,
                                                                    len(unordered_dataset)))

    # Make sure ordered dataset is actually ordered
    num_unordered = 0
    for i, (sem_lbl, inst_lbl) in ordered_dataset:
        if not is_lr_ordered(sem_lbl, inst_lbl):
            num_unordered += 1
    if num_unordered > 0:
        raise Exception('{}/{} were in the wrong order'.format(num_unordered, len(ordered_dataset)))
    else:
        print('PASSED: {}/{} now ordered left-right'.format(len(ordered_dataset) - num_unordered, len(ordered_dataset)))


if __name__ == '__main__':
    print('Testing synthetic...')
    test_lr_of_dataset('synthetic')
    print('Testing VOC...')
    test_lr_of_dataset('voc')

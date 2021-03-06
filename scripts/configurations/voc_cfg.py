from . import generic_cfg
from instanceseg.datasets import voc


def get_default_config():
    default_cfg = generic_cfg.get_default_config()
    default_cfg.update(
        dict(n_instances_per_class=3,
             lr=1.0e-4,
             dataset='voc',
             dataset_path=voc.get_default_voc_root()
             )
    )
    return default_cfg


configurations = {
    # same configuration as original work
    # https://github.com/shelhamer/fcn.berkeleyvision.org
    0: dict(),
    1: dict(
        n_instances_per_class=1,
    ),
    2: dict(
        semantic_only_labels=True,
        n_instances_per_class=1,
    ),
    3: dict(
        semantic_only_labels=False,
        n_instances_per_class=3,
    ),
    4: dict(
        semantic_subset=['person', 'background'],
    ),
    5: dict(
        semantic_only_labels=False,
        n_instances_per_class=3,
        max_iteration=1000000
    ),
    6: dict(  # semantic, single-instance problem
        n_instances_per_class=None,
        max_iteration=1000000,
        single_instance=True
    ),
    7: dict(
        semantic_only_labels=False,
        n_instances_per_class=3,
        weight_by_instance=True
    ),
    8: dict(
        n_instances_per_class=3,
        weight_by_instance=True,
        semantic_subset=['person', 'background'],
    ),
    9: dict(  # created to reduce memory
        n_instances_per_class=3,
        semantic_subset=['person', 'car', 'background'],
    ),
    10: dict(
        n_instances_per_class=3,
        semantic_subset=['person', 'car', 'background'],
        lr=1e-6
    ),
    11: dict(
        semantic_subset=['person', 'background'],
        interval_validate=4000,
        max_iteration=10000000,
    ),
    'person_only__freeze_vgg__many_itr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=1000,
        max_iteration=100000,
        freeze_vgg=True,
    ),
    'person_only__nofreeze__many_itr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=1000,
        max_iteration=100000,
        freeze_vgg=False,
    ),
    'person_only__3_channels_map_to_semantic__freeze_vgg__many_itr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=10,
        max_iteration=100000,
        map_to_semantic=True,
        n_instances_per_class=3,
        freeze_vgg=True,
    ),
    'person_only__3_channels_map_to_semantic__freeze_vgg__few_itr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=10,
        max_iteration=500,
        map_to_semantic=True,
        n_instances_per_class=3,
        freeze_vgg=True,
    ),
    'person_only__3_channels_map_to_semantic__nofreeze__few_itr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=10,
        max_iteration=500,
        map_to_semantic=True,
        n_instances_per_class=3,
        freeze_vgg=False,
    ),
    'person_only__3_channels_map_to_semantic__nofreeze__many_itr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=10,
        max_iteration=100000,
        map_to_semantic=True,
        n_instances_per_class=3,
        freeze_vgg=False,
    ),
    'person_only__3_channels__single_inst__nofreeze__many_itr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=10,
        max_iteration=100000,
        n_instances_per_class=3,
        freeze_vgg=False,
        map_to_semantic=False,
        single_instance=True,
    ),
    'person_only__3_channels__single_inst__freeze_vgg__many_itr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=10,
        max_iteration=100000,
        n_instances_per_class=3,
        freeze_vgg=True,
        map_to_semantic=False,
        single_instance=True,
    ),
    'person__semantic__freeze__smaller_lr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=50,
        max_iteration=100000,
        map_to_semantic=True,
        n_instances_per_class=3,
        freeze_vgg=True,
        lr=1e-6
    ),
    'person__semantic__nofreeze__smaller_lr': dict(
        semantic_subset=['person', 'background'],
        interval_validate=50,
        max_iteration=100000,
        map_to_semantic=True,
        n_instances_per_class=3,
        freeze_vgg=False,
        lr=1e-6
    ),
    'person_aug_inst': dict(
        semantic_subset=['person', 'background'],
        interval_validate=1000,
        max_iteration=100000,
        n_instances_per_class=3,
        freeze_vgg=False,
        augment_semantic=True,
    ),
    'person_noaug_inst': dict(
        semantic_subset=['person', 'background'],
        interval_validate=1000,
        max_iteration=100000,
        n_instances_per_class=3,
        freeze_vgg=False,
        augment_semantic=False,
    ),
    'person_aug_sem': dict(
        semantic_subset=['person', 'background'],
        interval_validate=1000,
        max_iteration=100000,
        n_instances_per_class=3,
        freeze_vgg=False,
        augment_semantic=True,
        map_to_semantic=True
    ),
    'person_noaug_sem': dict(
        semantic_subset=['person', 'background'],
        interval_validate=1000,
        max_iteration=100000,
        n_instances_per_class=3,
        freeze_vgg=False,
        augment_semantic=False,
        map_to_semantic=True
    ),
    'person_lr': dict(
        ordering='lr',
        matching=False,
        semantic_subset=['person', 'background'],
        n_instances_per_class=3,
    ),
    'person_not_lr': dict(
        ordering=None,
        matching=True,
        semantic_subset=['person', 'background'],
        n_instances_per_class=3,
    )
}

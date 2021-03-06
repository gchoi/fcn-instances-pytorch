
#!/usr/bin/env python

import argparse
import os
import os.path as osp

import numpy as np
import torch
from tensorboardX import SummaryWriter

import instanceseg
import instanceseg.datasets.voc
import instanceseg.utils.configs
import instanceseg.utils.logs
import instanceseg.utils.misc
import instanceseg.utils.scripts
from instanceseg.utils import instance_utils

default_config = dict(
    max_iteration=100000,
    lr=1.0e-6,
    momentum=0.99,
    weight_decay=0.0005,
    interval_validate=4000,
    matching=True,
    semantic_only_labels=False,
    n_instances_per_class=1,
    set_extras_to_void=True,
    semantic_subset=None,
    filter_images_by_semantic_subset=False,
    optim='adam'
)

configurations = {
    # same configuration as original work
    # https://github.com/shelhamer/fcn.berkeleyvision.org
    0: dict(  # single train image; enough iterations it should largely cover the train
        interval_validate=10,
        max_iteration=101,
        image_index=3
    ),
    1: dict(
        interval_validate=100,
        max_iteration=500,
        image_index=7,
        n_instances_per_class=2,
        lr=1e-6
    ),
    2: dict(
        interval_validate=10,
        max_iteration=200,
        image_index=7,
        n_instances_per_class=2,
        lr=1e-6
    ),
    3: dict(
        interval_validate=10,
        max_iteration=500,
        image_index=7,
        n_instances_per_class=2,
        lr=1e-6,
        semantic_only_labels=True,
    )
}

here = osp.dirname(osp.abspath(__file__))


def main():
    instanceseg.utils.scripts.check_clean_work_tree()
    parser = argparse.ArgumentParser()
    parser.add_argument('-g', '--gpu', type=int, required=True)
    parser.add_argument('-c', '--config', type=int, default=0,
                        choices=configurations.keys())
    parser.add_argument('--resume', help='Checkpoint path')
    parser.add_argument('--image_index', type=int, help='Image index to use for train/validation set', default=None)
    args = parser.parse_args()
    gpu = args.gpu
    config_idx = args.config

    cfg = instanceseg.utils.configs.create_config_from_default(configurations[config_idx], default_config)
    if args.image_index is not None:
        cfg['image_index'] = args.image_index

    out = instanceseg.utils.logs.get_log_dir(osp.basename(__file__).replace(
        '.py', ''), config_idx, instanceseg.utils.configs.create_config_copy(cfg),
        parent_directory=osp.dirname(osp.abspath(__file__)))

    print('logdir: {}'.format(out))
    resume = args.resume

    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)
    cuda = torch.cuda.is_available()

    torch.manual_seed(1337)
    if cuda:
        torch.cuda.manual_seed(1337)

    # 0. Problem setup (instance segmentation definition)
    n_semantic_classes = 21
    n_instances_by_semantic_id = [1] + [cfg['n_instances_per_class'] for sem_cls in range(1, n_semantic_classes)]
    problem_config = instance_utils.InstanceProblemConfig(n_instances_by_semantic_id=n_instances_by_semantic_id)

    # 1. dataset
    root = osp.expanduser('~/data/datasets')
    dataset_kwargs = dict(transform=True, semantic_only_labels=cfg['semantic_only_labels'],
                          set_extras_to_void=cfg['set_extras_to_void'], semantic_subset=cfg['semantic_subset'],
                          modified_indices=[cfg['image_index']])
    kwargs = {'num_workers': 4, 'pin_memory': True} if cuda else {}
    val_split = 'seg11valid'
    val_dataset = instanceseg.datasets.voc.VOC2011ClassSeg(root, split=val_split, **dataset_kwargs)
    val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=1, shuffle=False, **kwargs)
    train_loader = torch.utils.data.DataLoader(val_dataset, batch_size=1, shuffle=True, **kwargs)

    problem_config.set_class_names(val_dataset.class_names)

    # 2. model

    model = instanceseg.models.FCN8sInstance(
        semantic_instance_class_list=problem_config.semantic_instance_class_list,
        map_to_semantic=False, include_instance_channel0=False)
    print('Number of classes in model: {}'.format(model.n_classes))
    start_epoch = 0
    start_iteration = 0
    if resume:
        checkpoint = torch.load(resume)
        model.load_state_dict(checkpoint['model_state_dict'])
        start_epoch = checkpoint['epoch']
        start_iteration = checkpoint['iteration']
    else:
        print('Copying params from vgg16')
        vgg16 = instanceseg.models.VGG16(pretrained=True)
        model.copy_params_from_vgg16(vgg16)
    if cuda:
        model = model.cuda()

    # 3. optimizer
    if cfg['optim'] == 'adam':
        optim = torch.optim.Adam(model.parameters(), lr=cfg['lr'], weight_decay=cfg['weight_decay'])
    elif cfg['optim'] == 'sgd':
        optim = torch.optim.SGD(
            [
                {'params': instanceseg.utils.configs.get_parameters(model, bias=False)},
                {'params': instanceseg.utils.configs.get_parameters(model, bias=True),
                 #            {'params': filter(lambda p: False if p is None else p.requires_grad, get_parameters(
                 #                model, bias=False))},
                 #            {'params': filter(lambda p: False if p is None else p.requires_grad, get_parameters(
                 #                model, bias=True)),

                 'lr': cfg['lr'] * 2, 'weight_decay': 0},
            ],
            lr=cfg['lr'],
            momentum=cfg['momentum'],
            weight_decay=cfg['weight_decay'])
    else:
        raise Exception('optimizer {} not recognized.'.format(cfg['optim']))
    if resume:
        optim.load_state_dict(checkpoint['optim_state_dict'])

    writer = SummaryWriter(log_dir=out)
    trainer = instanceseg.Trainer(cuda=cuda, model=model, optimizer=optim, train_loader=train_loader,
                                  val_loader=val_loader, out_dir=out, max_iter=cfg['max_iteration'],
                                  instance_problem=problem_config,
                                  interval_validate=cfg.get('interval_validate', len(train_loader)),
                                  matching_loss=cfg['matching'], tensorboard_writer=writer,
                                  loader_semantic_lbl_only=cfg['semantic_only_labels'])
    trainer.epoch = start_epoch
    trainer.iteration = start_iteration

    print('Starting test by training {} images'.format(len(train_loader.dataset)))
    trainer.train()

    print('Evaluating final model')

    val_loss, metrics, (segmentation_visualizations, score_visualizations) = trainer.validate_split(
        should_export_visualizations=False)
    
    trainer.export_visualizations(segmentation_visualizations, 'seg_' + val_split, tile=True, outdir='./tmp/')
    trainer.export_visualizations(score_visualizations, 'score_' + val_split, tile=False, outdir='./tmp/')
    # viz = visualization_utils.get_tile_image(visualizations)
    # skimage.io.imsave(os.path.join(here, 'viz_evaluate.png'), viz)
    metrics = np.array(metrics)
    metrics *= 100
    print('''\
        Accuracy: {0}
        Accuracy Class: {1}
        Mean IU: {2}
        FWAV Accuracy: {3}'''.format(*metrics))
    if metrics[2] < 80:
        print(instanceseg.utils.misc.TermColors.FAIL + 'Test FAILED.  mIOU: {}'.format(metrics[2]) + instanceseg.utils.misc.TermColors.ENDC)
    else:
        print(instanceseg.utils.misc.TermColors.OKGREEN + 'TEST PASSED! mIOU: {}'.format(metrics[2]) + instanceseg.utils.misc.TermColors.ENDC)


if __name__ == '__main__':
    main()

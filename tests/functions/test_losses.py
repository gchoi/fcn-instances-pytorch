import os.path as osp

import instanceseg.utils.configs
import instanceseg.utils.logs
import instanceseg.utils.misc
import instanceseg.utils.scripts
from instanceseg.utils.scripts import setup, configure

here = osp.dirname(osp.abspath(__file__))


def get_single_img_data(dataloader, idx=0):
    img, sem_lbl, inst_lbl = None, None, None
    for i, (img, (sem_lbl, inst_lbl)) in enumerate(dataloader):
        if i != idx:
            continue
    return img, (sem_lbl, inst_lbl)


def main():
    args, cfg_override_args = instanceseg.utils.scripts.parse_args_without_sys(dataset_name='synthetic')
    cfg_override_args.loss_type = 'soft_iou'
    cfg_override_args.size_average = False
    cfg, out_dir, sampler_cfg = configure(dataset_name=args.dataset,
                                          config_idx=args.config,
                                          sampler_name=args.sampler,
                                          script_py_file=__file__,
                                          cfg_override_args=cfg_override_args)
    trainer = setup(args.dataset, cfg, out_dir, sampler_cfg, gpu=args.gpu, resume=args.resume,
                    semantic_init=args.semantic_init)

    img_data, (sem_lbl, inst_lbl) = get_single_img_data(trainer.train_loader, idx=0)
    full_input, sem_lbl, inst_lbl = trainer.prepare_data_for_forward_pass(img_data, (sem_lbl, inst_lbl),
                                                                          requires_grad=False)
    score_1 = trainer.model(full_input)
    score_gt = score_1.clone()
    score_gt[...] = 0
    magnitude = 100
    for c in range(score_1.size(1)):
        score_gt[:, c, :, :] = (sem_lbl == trainer.instance_problem.semantic_instance_class_list[c]).float() * \
                               (inst_lbl == trainer.instance_problem.instance_count_id_list[c]).float() * magnitude
    try:
        assert (score_gt.sum(dim=1) == magnitude).all()  # debug sanity check
    except AssertionError:
        import ipdb
        ipdb.set_trace()
    loss_object = trainer.loss_object

    cost_matrix_gt = loss_object.build_all_sem_cls_cost_matrices_as_tensor_data(
        loss_object.transform_scores_to_predictions(score_gt)[0, ...], sem_lbl[0, ...], inst_lbl[0, ...])
    pred_permutations_gt, avg_loss_gt, loss_components_gt = trainer.compute_loss(score_gt, sem_lbl, inst_lbl)
    pred_permutations_1, avg_loss_1, loss_components_1 = trainer.compute_loss(score_1, sem_lbl, inst_lbl)
    import ipdb;
    ipdb.set_trace()


if __name__ == '__main__':
    main()

import datetime
import os
import os.path as osp
import shutil

import matplotlib.pyplot as plt
import numpy as np
import pytz
import torch
import torch.nn.functional as F
import tqdm

import instanceseg.utils.display as display_pyutils
import instanceseg.utils.export
from instanceseg.analysis import visualization_utils
from instanceseg.datasets import runtime_transformations
from instanceseg.utils import instance_utils
from instanceseg.utils.misc import flatten_dict
from tensorboardX import SummaryWriter

display_pyutils.set_my_rc_defaults()

MY_TIMEZONE = 'America/New_York'


def should_write_activations(iteration, epoch):
    if iteration < 3000:
        return True
    else:
        return False


DEBUG_ASSERTS = True


class ExportConfig(object):
    def __init__(self, export_activations=None, activation_layers_to_export=(), write_instance_metrics=False,
                 run_loss_updates=True):
        self.export_activations = export_activations
        self.activation_layers_to_export = activation_layers_to_export
        self.write_instance_metrics = write_instance_metrics
        self.run_loss_updates = run_loss_updates

        self.write_activation_condition = should_write_activations
        self.which_heatmaps_to_visualize = 'same semantic'  # 'all'

        self.downsample_multiplier_score_images = 0.5
        self.export_component_losses = True

        self.write_lr = True


class TrainerExporter(object):
    log_headers = [
        'epoch',
        'iteration',
        'train/losses',
        'train/acc',
        'train/acc_cls',
        'train/mean_iu',
        'train/fwavacc',
        'valid/losses',
        'valid/acc',
        'valid/acc_cls',
        'valid/mean_iu',
        'valid/fwavacc',
        'elapsed_time',
    ]

    def __init__(self, out_dir, instance_problem, export_config: ExportConfig = None,
                 tensorboard_writer: SummaryWriter=None, metric_makers=None):

        self.export_config = export_config or ExportConfig()

        # Copies of things the trainer was given access to
        self.instance_problem = instance_problem

        # Helper objects
        self.tensorboard_writer = tensorboard_writer

        # Log directory / log files
        self.out_dir = out_dir
        if not osp.exists(self.out_dir):
            os.makedirs(self.out_dir)

        if not osp.exists(osp.join(self.out_dir, 'log.csv')):
            with open(osp.join(self.out_dir, 'log.csv'), 'w') as f:
                f.write(','.join(self.log_headers) + '\n')

        # Logging parameters
        self.timestamp_start = datetime.datetime.now(pytz.timezone(MY_TIMEZONE))

        self.val_losses_stored = []
        self.train_losses_stored = []
        self.joint_train_val_loss_mpl_figure = None  # figure for plotting losses on same plot
        self.iterations_for_losses_stored = []

        self.metric_makers = metric_makers

        # Writing activations

        self.run_loss_updates = True

    def write_eval_metrics(self, eval_metrics, loss, split, epoch, iteration):
        with open(osp.join(self.out_dir, 'log.csv'), 'a') as f:
            elapsed_time = (
                    datetime.datetime.now(pytz.timezone(MY_TIMEZONE)) -
                    self.timestamp_start).total_seconds()
            if split == 'val':
                log = [epoch, iteration] + [''] * 5 + \
                      [loss] + list(eval_metrics) + [elapsed_time]
            elif split == 'train':
                try:
                    eval_metrics_as_list = eval_metrics.tolist()
                except:
                    eval_metrics_as_list = list(eval_metrics)
                log = [epoch, iteration] + [loss] + eval_metrics_as_list + [''] * 5 + [elapsed_time]
            else:
                raise ValueError('split not recognized')
            log = map(str, log)
            f.write(','.join(log) + '\n')

    def update_mpl_joint_train_val_loss_figure(self, train_loss, val_loss, iteration):
        assert train_loss is not None, ValueError
        assert val_loss is not None, ValueError
        figure_name = 'train/val losses'
        ylim_buffer_size = 3
        self.train_losses_stored.append(train_loss)
        self.val_losses_stored.append(val_loss)

        self.iterations_for_losses_stored.append(iteration)
        if self.joint_train_val_loss_mpl_figure is None:
            self.joint_train_val_loss_mpl_figure = plt.figure(figure_name)

        h = plt.figure(figure_name)

        plt.clf()
        train_label = 'train losses'  # TODO(allie): record number of images somewhere.. (we deleted it from here)
        val_label = 'val losses'

        plt.plot(self.iterations_for_losses_stored, self.train_losses_stored, label=train_label,
                 color=display_pyutils.GOOD_COLORS_BY_NAME['blue'])
        plt.plot(self.iterations_for_losses_stored, self.val_losses_stored, label=val_label,
                 color=display_pyutils.GOOD_COLORS_BY_NAME['aqua'])
        plt.xlabel('iteration')
        plt.legend()
        # Set y limits for just the last 10 datapoints
        last_x = max(len(self.train_losses_stored), len(self.val_losses_stored))
        if last_x >= 0:
            ymin = min(min(self.train_losses_stored[(last_x - ylim_buffer_size - 1):]),
                       min(self.val_losses_stored[(last_x - ylim_buffer_size - 1):]))
            ymax = max(max(self.train_losses_stored[(last_x - ylim_buffer_size - 1):]),
                       max(self.val_losses_stored[(last_x - ylim_buffer_size - 1):]))
        else:
            ymin, ymax = None, None
        if self.tensorboard_writer is not None:
            instanceseg.utils.export.log_plots(self.tensorboard_writer, 'joint_loss', [h], iteration)
        filename = os.path.join(self.out_dir, 'val_train_loss.png')
        h.savefig(filename)

        # zoom
        zoom_filename = os.path.join(self.out_dir, 'val_train_loss_zoom_last_{}.png'.format(ylim_buffer_size))
        if ymin is not None:
            plt.ylim(ymin=ymin, ymax=ymax)
            plt.xlim(xmin=(last_x - ylim_buffer_size - 1), xmax=last_x)
            if self.tensorboard_writer is not None:
                instanceseg.utils.export.log_plots(self.tensorboard_writer,
                                                   'joint_loss_last_{}'.format(ylim_buffer_size),
                                                   [h], iteration)
            h.savefig(zoom_filename)
        else:
            shutil.copyfile(filename, zoom_filename)

    def retrieve_and_write_batch_activations(self, batch_input, iteration,
                                             get_activations_fcn):
        """
        get_activations_fcn: example in FCN8sInstance.get_activations(batch_input, layer_names)
        """
        if self.tensorboard_writer is not None:
            activations = get_activations_fcn(batch_input, self.export_config.activation_layers_to_export)
            histogram_activations = activations
            for name, activations in tqdm.tqdm(histogram_activations.items(),
                                               total=len(histogram_activations.items()),
                                               desc='Writing activation distributions', leave=False):
                if name == 'upscore8':
                    channel_labels = self.instance_problem.get_model_channel_labels('{}_{}')
                    assert activations.size(1) == len(channel_labels), '{} != {}'.format(activations.size(1),
                                                                                         len(channel_labels))
                    for c, channel_label in enumerate(channel_labels):
                        self.tensorboard_writer.add_histogram('batch_activations/{}/{}'.format(name, channel_label),
                                                              activations[:, c, :, :].cpu().numpy(),
                                                              iteration, bins='auto')
                elif name == 'conv1x1_instance_to_semantic':
                    channel_labels = self.instance_problem.get_channel_labels('{}_{}')
                    assert activations.size(1) == len(channel_labels)
                    for c, channel_label in enumerate(channel_labels):
                        try:
                            self.tensorboard_writer.add_histogram('batch_activations/{}/{}'.format(name, channel_label),
                                                                  activations[:, c, :, :].cpu().numpy(),
                                                                  iteration, bins='auto')
                        except IndexError as ie:
                            print('WARNING: Didn\'t write activations.  IndexError: {}'.format(ie))
                elif name == 'conv1_1':
                    # This is expensive to write, so we'll just write a representative set.
                    min = torch.min(activations)
                    max = torch.max(activations)
                    mean = torch.mean(activations)
                    representative_set = np.ndarray((100, 3))
                    representative_set[:, 0] = min
                    representative_set[:, 1] = max
                    representative_set[:, 2] = mean
                    self.tensorboard_writer.add_histogram('batch_activations/{}/min_mean_max_all_channels'.format(name),
                                                          representative_set, iteration, bins='auto')
                    continue

                self.tensorboard_writer.add_histogram('batch_activations/{}/all_channels'.format(name),
                                                      activations.cpu().numpy(), iteration, bins='auto')

    def write_loss_updates(self, old_loss, new_loss, old_pred_permutations, new_pred_permutations, iteration):
        loss_improvement = old_loss - new_loss
        num_reassignments = float(np.sum(new_pred_permutations != old_pred_permutations))
        self.tensorboard_writer.add_scalar('A_eval_metrics/train_minibatch_loss_improvement', loss_improvement,
                                           iteration)
        self.tensorboard_writer.add_scalar('A_eval_metrics/reassignment', num_reassignments, iteration)

    def compute_and_write_instance_metrics(self, model, iteration):
        if self.tensorboard_writer is not None:
            for split, metric_maker in tqdm.tqdm(self.metric_makers.items(), desc='Computing instance metrics',
                                                 total=len(self.metric_makers.items()), leave=False):
                metric_maker.clear()
                metric_maker.compute_metrics(model)
                metrics_as_nested_dict = metric_maker.get_aggregated_scalar_metrics_as_nested_dict()
                metrics_as_flattened_dict = flatten_dict(metrics_as_nested_dict)
                for name, metric in metrics_as_flattened_dict.items():
                    self.tensorboard_writer.add_scalar('C_{}_{}'.format(name, split), metric,
                                                       iteration)
                histogram_metrics_as_nested_dict = metric_maker.get_aggregated_histogram_metrics_as_nested_dict()
                histogram_metrics_as_flattened_dict = flatten_dict(histogram_metrics_as_nested_dict)
                if iteration != 0:  # screws up the axes if we do it on the first iteration with weird inits
                    for name, metric in tqdm.tqdm(histogram_metrics_as_flattened_dict.items(),
                                                  total=len(histogram_metrics_as_flattened_dict.items()),
                                                  desc='Writing histogram metrics', leave=False):
                        if torch.is_tensor(metric):
                            self.tensorboard_writer.add_histogram('C_instance_metrics_{}/{}'.format(split, name),
                                                                  metric.numpy(), iteration, bins='auto')
                        elif isinstance(metric, np.ndarray):
                            self.tensorboard_writer.add_histogram('C_instance_metrics_{}/{}'.format(split, name),
                                                                  metric, iteration, bins='auto')
                        elif metric is None:
                            import ipdb;
                            ipdb.set_trace()
                            pass
                        else:
                            raise ValueError('I\'m not sure how to write {} to tensorboard_writer (name is '
                                             '{}'.format(type(metric), name))

    def save_checkpoint(self, epoch, iteration, model, optimizer, best_mean_iu, out_dir=None,
                        out_name='checkpoint.pth.tar'):
        out_dir = out_dir or self.out_dir
        checkpoint_file = osp.join(out_dir, out_name)
        torch.save({
            'epoch': epoch,
            'iteration': iteration,
            'arch': model.__class__.__name__,
            'optim_state_dict': optimizer.state_dict(),
            'model_state_dict': model.state_dict(),
            'best_mean_iu': best_mean_iu,
        }, checkpoint_file)
        return checkpoint_file

    def copy_checkpoint_as_best(self, current_checkpoint_file, out_dir=None, out_name='model_best.pth.tar'):
        out_dir = out_dir or self.out_dir
        best_checkpoint_file = osp.join(out_dir, out_name)
        shutil.copy(current_checkpoint_file, best_checkpoint_file)
        return best_checkpoint_file

    def visualize_one_img_prediction(self, img_untransformed, lp, lt_combined, pp, softmax_scores, true_labels, idx):
        # Segmentations
        segmentation_viz = visualization_utils.visualize_segmentation(
            lbl_pred=lp, lbl_true=lt_combined, pred_permutations=pp, img=img_untransformed,
            n_class=self.instance_problem.n_classes, overlay=False)
        # Scores
        sp = softmax_scores[idx, :, :, :]
        # TODO(allie): Fix this -- bug(?!)
        lp = np.argmax(sp, axis=0)
        if self.export_config.which_heatmaps_to_visualize == 'same semantic':
            inst_sem_classes_present = torch.np.unique(true_labels)
            inst_sem_classes_present = inst_sem_classes_present[inst_sem_classes_present != -1]
            sem_classes_present = np.unique([self.instance_problem.semantic_instance_class_list[c]
                                             for c in inst_sem_classes_present])
            channels_for_these_semantic_classes = [inst_idx for inst_idx, sem_cls in enumerate(
                self.instance_problem.semantic_instance_class_list) if sem_cls in sem_classes_present]
            channels_to_visualize = channels_for_these_semantic_classes
        elif self.export_config.which_heatmaps_to_visualize == 'all':
            channels_to_visualize = list(range(sp.shape[0]))
        else:
            raise ValueError('which heatmaps to visualize is not recognized: {}'.format(
                self.export_config.which_heatmaps_to_visualize))
        channel_labels = self.instance_problem.get_channel_labels('{} {}')
        score_viz = visualization_utils.visualize_heatmaps(scores=sp,
                                                           lbl_true=lt_combined,
                                                           lbl_pred=lp,
                                                           pred_permutations=pp,
                                                           n_class=self.instance_problem.n_classes,
                                                           score_vis_normalizer=sp.max(),
                                                           channel_labels=channel_labels,
                                                           channels_to_visualize=channels_to_visualize,
                                                           input_image=img_untransformed)
        if self.export_config.downsample_multiplier_score_images != 1:
            score_viz = visualization_utils.resize_img_by_multiplier(
                score_viz, self.export_config.downsample_multiplier_score_images)
        return segmentation_viz, score_viz

    def export_score_and_seg_images(self, segmentation_visualizations, score_visualizations, iteration, split):
        self.export_visualizations(segmentation_visualizations, iteration, basename='seg_' + split, tile=True)
        self.export_visualizations(score_visualizations, iteration, basename='score_' + split, tile=False)

    def export_visualizations(self, visualizations, iteration, basename='val_', tile=True, out_dir=None):
        out_dir = out_dir or osp.join(self.out_dir, 'visualization_viz')
        visualization_utils.export_visualizations(visualizations, out_dir, self.tensorboard_writer, iteration,
                                                  basename=basename, tile=tile)

    def run_post_val_epoch(self, label_preds, label_trues, pred_permutations, should_compute_basic_metrics, split,
                           val_loss, val_metrics, write_basic_metrics, write_instance_metrics, epoch, iteration, model):
        if should_compute_basic_metrics:
            val_metrics = self.compute_eval_metrics(label_trues, label_preds, pred_permutations)
            if write_basic_metrics:
                self.write_eval_metrics(val_metrics, val_loss, split, epoch=epoch, iteration=iteration)
                if self.tensorboard_writer is not None:
                    self.tensorboard_writer.add_scalar('A_eval_metrics/{}/losses'.format(split), val_loss, iteration)
                    self.tensorboard_writer.add_scalar('A_eval_metrics/{}/mIOU'.format(split), val_metrics[2],
                                                       iteration)

        if write_instance_metrics:
            self.compute_and_write_instance_metrics(model=model, iteration=iteration)
        return val_metrics

    def run_post_train_iteration(self, full_input, inst_lbl, loss, loss_components, pred_permutations, score, sem_lbl,
                                 epoch, iteration, new_pred_permutations=None, new_loss=None,
                                 get_activations_fcn=None, lrs_by_group=None):
        """
        get_activations_fcn=self.model.get_activations
        """
        inst_lbl_pred = score.data.max(1)[1].cpu().numpy()[:, :, :]
        lbl_true_sem, lbl_true_inst = sem_lbl.data.cpu().numpy(), inst_lbl.data.cpu().numpy()
        eval_metrics = []
        for sem_lbl_np, inst_lbl_np, lp in zip(lbl_true_sem, lbl_true_inst, inst_lbl_pred):
            lt_combined = self.gt_tuple_to_combined(sem_lbl_np, inst_lbl_np)
            acc, acc_cls, mean_iu, fwavacc = \
                self.compute_eval_metrics(
                    label_trues=[lt_combined], label_preds=[lp], permutations=[pred_permutations])
            eval_metrics.append((acc, acc_cls, mean_iu, fwavacc))
        eval_metrics = np.mean(eval_metrics, axis=0)
        self.write_eval_metrics(eval_metrics, loss, split='train', epoch=epoch, iteration=iteration)
        if self.tensorboard_writer is not None:
		# TODO(allie): Check dimensionality of loss to prevent potential bugs
            self.tensorboard_writer.add_scalar('A_eval_metrics/train_minibatch_loss', loss.data.sum(),
                                               iteration)

        if self.export_config.write_lr:
            for group_idx, lr in enumerate(lrs_by_group):
                self.tensorboard_writer.add_scalar('Z_hyperparameters/lr_group{}'.format(group_idx), lr, iteration)

        if self.export_config.export_component_losses:
            for c_idx, c_lbl in enumerate(self.instance_problem.get_model_channel_labels('{}_{}')):
                self.tensorboard_writer.add_scalar('B_component_losses/train/{}'.format(c_lbl),
                                                   loss_components.data[:, c_idx].sum(), iteration)

        if self.export_config.run_loss_updates:
            self.write_loss_updates(old_loss=loss.data[0], new_loss=new_loss.sum(),
                                    old_pred_permutations=pred_permutations,
                                    new_pred_permutations=new_pred_permutations,
                                    iteration=iteration)

            if self.export_config.export_activations and \
                    self.export_config.write_activation_condition(iteration, epoch):
                self.retrieve_and_write_batch_activations(batch_input=full_input, iteration=iteration,
                                                          get_activations_fcn=get_activations_fcn)
        return eval_metrics

    def run_post_val_iteration(self, imgs, inst_lbl, pred_permutations, score, sem_lbl, should_visualize,
                               data_to_img_transformer):
        """
        data_to_img_transformer: img_untransformed, lbl_untransformed = f(img, lbl) : e.g. - resizes, etc.
        """
        true_labels = []
        pred_labels = []
        segmentation_visualizations = []
        score_visualizations = []

        softmax_scores = F.softmax(score, dim=1).data.cpu().numpy()
        inst_lbl_pred = score.data.max(dim=1)[1].cpu().numpy()[:, :, :]
        lbl_true_sem, lbl_true_inst = (sem_lbl.data.cpu(), inst_lbl.data.cpu())
        if DEBUG_ASSERTS:
            assert inst_lbl_pred.shape == lbl_true_inst.shape
        for idx, (img, sem_lbl, inst_lbl, lp) in enumerate(zip(imgs, lbl_true_sem, lbl_true_inst, inst_lbl_pred)):
            # runtime_transformation needs to still run the resize, even for untransformed img, lbl pair
            img_untransformed, lbl_untransformed = data_to_img_transformer(img, (sem_lbl, inst_lbl)) \
                if data_to_img_transformer is not None \
                else (img, (sem_lbl, inst_lbl))
            sem_lbl_np, inst_lbl_np = lbl_untransformed

            pp = pred_permutations[idx, :]
            lt_combined = self.gt_tuple_to_combined(sem_lbl_np, inst_lbl_np)
            true_labels.append(lt_combined)
            pred_labels.append(lp)
            if should_visualize:
                segmentation_viz, score_viz = self.visualize_one_img_prediction(
                    img_untransformed, lp, lt_combined, pp, softmax_scores, true_labels, idx)
                score_visualizations.append(score_viz)
                segmentation_visualizations.append(segmentation_viz)
        return true_labels, pred_labels, segmentation_visualizations, score_visualizations

    def compute_eval_metrics(self, label_trues, label_preds, permutations=None, single_batch=False):
        if permutations is not None:
            if single_batch:
                permutations = [permutations]
            assert type(permutations) == list, \
                NotImplementedError('I''m assuming permutations are a list of ndarrays from multiple batches, '
                                    'not type {}'.format(type(permutations)))
            label_preds_permuted = [instance_utils.permute_labels(label_pred, perms)
                                    for label_pred, perms in zip(label_preds, permutations)]
        else:
            label_preds_permuted = label_preds
        eval_metrics_list = instanceseg.utils.misc.label_accuracy_score(label_trues, label_preds_permuted,
                                                                        n_class=self.instance_problem.n_classes)
        return eval_metrics_list

    def gt_tuple_to_combined(self, sem_lbl, inst_lbl):
        semantic_instance_class_list = self.instance_problem.semantic_instance_class_list
        instance_count_id_list = self.instance_problem.instance_count_id_list
        return instance_utils.combine_semantic_and_instance_labels(sem_lbl, inst_lbl,
                                                                   semantic_instance_class_list,
                                                                   instance_count_id_list)

    @staticmethod
    def untransform_data(data_loader, img, lbl):
        (sem_lbl, inst_lbl) = lbl
        if data_loader.dataset.runtime_transformation is not None:
            runtime_transformation_undo = runtime_transformations.GenericSequenceRuntimeDatasetTransformer(
                [t for t in (data_loader.dataset.runtime_transformation.transformer_sequence or [])
                 if isinstance(t, runtime_transformations.BasicRuntimeDatasetTransformer)])
            img_untransformed, lbl_untransformed = runtime_transformation_undo.untransform(img, (sem_lbl, inst_lbl))
        else:
            img_untransformed, lbl_untransformed = img, (sem_lbl, inst_lbl)
        return img_untransformed, lbl_untransformed


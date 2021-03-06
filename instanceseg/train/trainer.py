import math

import numpy as np
import torch
import tqdm
from torch.autograd import Variable
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.optim.optimizer import Optimizer

import instanceseg
import instanceseg.losses.loss
import instanceseg.utils.export
import instanceseg.utils.misc
from instanceseg.utils.instance_utils import InstanceProblemConfig
from instanceseg.models.fcn8s_instance import FCN8sInstance
from instanceseg.models.model_utils import is_nan, any_nan
from instanceseg.train import metrics, trainer_exporter
from instanceseg.utils import datasets

DEBUG_ASSERTS = True

BINARY_AUGMENT_MULTIPLIER = 100.0
BINARY_AUGMENT_CENTERED = True


class TrainingState(object):
    def __init__(self, max_iteration):
        self.iteration = 0
        self.epoch = 0
        self.max_iteration = max_iteration

    def training_complete(self):
        return self.iteration >= self.max_iteration


class Trainer(object):
    def __init__(self, cuda, model: FCN8sInstance, optimizer: Optimizer, train_loader, val_loader, out_dir, max_iter,
                 instance_problem: InstanceProblemConfig,
                 size_average=True, interval_validate=None, loss_type='cross_entropy', matching_loss=True,
                 tensorboard_writer=None, train_loader_for_val=None, loader_semantic_lbl_only=False,
                 use_semantic_loss=False, augment_input_with_semantic_masks=False, write_instance_metrics=True,
                 generate_new_synthetic_data_each_epoch=False,
                 export_activations=False, activation_layers_to_export=(),
                 lr_scheduler: ReduceLROnPlateau = None):

        # System parameters
        self.cuda = cuda

        # Model objects
        self.model = model

        # Training objects
        self.optim = optimizer

        # Dataset objects
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.train_loader_for_val = train_loader_for_val

        # Problem setup objects
        self.instance_problem = instance_problem

        # Exporting objects
        # Exporting parameters

        # Loss parameters
        self.size_average = size_average
        self.matching_loss = matching_loss
        self.loss_type = loss_type

        # Data loading parameters
        self.loader_semantic_lbl_only = loader_semantic_lbl_only

        self.use_semantic_loss = use_semantic_loss
        self.augment_input_with_semantic_masks = augment_input_with_semantic_masks
        self.generate_new_synthetic_data_each_epoch = generate_new_synthetic_data_each_epoch

        self.write_instance_metrics = write_instance_metrics

        # Stored values
        self.last_val_loss = None

        self.interval_validate = interval_validate if interval_validate is not None else len(self.train_loader)

        self.state = TrainingState(max_iteration=max_iter)
        self.best_mean_iu = 0
        # TODO(allie): clean up max combined class... computing accuracy shouldn't need it.

        self.loss_object = self.build_my_loss()
        self.eval_loss_object_with_matching = self.build_my_loss()  # Uses matching
        self.loss_fcn = self.loss_object.loss_fcn  # scores, sem_lbl, inst_lbl
        self.eval_loss_fcn_with_matching = self.eval_loss_object_with_matching.loss_fcn
        self.lr_scheduler = lr_scheduler

        metric_maker_kwargs = {
            'problem_config': self.instance_problem,
            'component_loss_function': self.eval_loss_fcn_with_matching,
            'augment_function_img_sem': self.augment_image
            if self.augment_input_with_semantic_masks else None
        }
        metric_makers = {
            'val': metrics.InstanceMetrics(self.val_loader, **metric_maker_kwargs),
            'train_for_val': metrics.InstanceMetrics(self.train_loader_for_val, **metric_maker_kwargs)
        }
        export_config = trainer_exporter.ExportConfig(export_activations=export_activations,
                                                      activation_layers_to_export=activation_layers_to_export,
                                                      write_instance_metrics=write_instance_metrics)
        self.exporter = trainer_exporter.TrainerExporter(
            out_dir=out_dir, instance_problem=instance_problem,
            export_config=export_config, tensorboard_writer=tensorboard_writer, metric_makers=metric_makers)

    def prepare_data_for_forward_pass(self, img_data, target, requires_grad=True):
        """
        Loads data and transforms it into Variable based on GPUs, input augmentations, and loader type (if semantic)
        requires_grad: True if training; False if you're not planning to backprop through (for validation / metrics)
        """
        if not self.loader_semantic_lbl_only:
            (sem_lbl, inst_lbl) = target
        else:
            assert self.use_semantic_loss, 'Can''t run instance losses if loader is semantic labels only.  Set ' \
                                           'use_semantic_loss to True'
            assert type(target) is not tuple
            sem_lbl = target
            inst_lbl = torch.zeros_like(sem_lbl)
            inst_lbl[sem_lbl == -1] = -1

        if self.cuda:
            img_data, (sem_lbl, inst_lbl) = img_data.cuda(), (sem_lbl.cuda(), inst_lbl.cuda())
        full_input = img_data if not self.augment_input_with_semantic_masks \
            else self.augment_image(img_data, sem_lbl)
        full_input, sem_lbl, inst_lbl = \
            Variable(full_input, volatile=(not requires_grad)), \
            Variable(sem_lbl, requires_grad=requires_grad), \
            Variable(inst_lbl, requires_grad=requires_grad)
        return full_input, sem_lbl, inst_lbl

    def build_my_loss(self, matching_override=None):
        # permutations, loss, loss_components = f(scores, sem_lbl, inst_lbl)

        matching = matching_override if matching_override is not None else self.matching_loss

        my_loss_object = instanceseg.losses.loss.loss_object_factory(self.loss_type,
                                                                     self.instance_problem.semantic_instance_class_list,
                                                                     self.instance_problem.instance_count_id_list,
                                                                     matching, self.size_average)
        return my_loss_object

    def compute_loss(self, score, sem_lbl, inst_lbl, val_matching_override=False):
        # permutations, loss, loss_components = f(scores, sem_lbl, inst_lbl)
        map_to_semantic = self.instance_problem.map_to_semantic
        if not (sem_lbl.size() == inst_lbl.size() == (score.size(0), score.size(2), score.size(3))):
            raise Exception('Sizes of score, targets are incorrect')

        if map_to_semantic:
            inst_lbl[inst_lbl > 1] = 1
        loss_fcn = self.loss_fcn if not val_matching_override else self.eval_loss_fcn_with_matching
        permutations, total_loss, loss_components = loss_fcn(score, sem_lbl, inst_lbl)
        avg_loss = total_loss / score.size(0)
        return permutations, avg_loss, loss_components

    def augment_image(self, img, sem_lbl):
        semantic_one_hot = datasets.labels_to_one_hot(sem_lbl, self.instance_problem.n_semantic_classes)
        return datasets.augment_channels(img, BINARY_AUGMENT_MULTIPLIER * semantic_one_hot -
                                         (0.5 if BINARY_AUGMENT_CENTERED else 0), dim=1)

    def validate_split(self, split='val', write_basic_metrics=None, write_instance_metrics=None,
                       should_export_visualizations=True):
        """
        If split == 'val': write_metrics, save_checkpoint, update_best_checkpoint default to True.
        If split == 'train': write_metrics, save_checkpoint, update_best_checkpoint default to
            False.
        """
        val_metrics = None
        save_checkpoint = (split == 'val')
        write_instance_metrics = (split == 'val') and self.write_instance_metrics \
            if write_instance_metrics is None else write_instance_metrics
        write_basic_metrics = True if write_basic_metrics is None else write_basic_metrics
        should_compute_basic_metrics = write_basic_metrics or write_instance_metrics or save_checkpoint

        assert split in ['train', 'val']
        if split == 'train':
            data_loader = self.train_loader_for_val
        else:
            data_loader = self.val_loader

        # eval instead of training mode temporarily
        training = self.model.training
        self.model.eval()

        val_loss = 0
        segmentation_visualizations, score_visualizations = [], []
        label_trues, label_preds, scores, pred_permutations = [], [], [], []
        num_images_to_visualize = min(len(data_loader), 9)
        for batch_idx, (img_data, lbls) in tqdm.tqdm(
                enumerate(data_loader), total=len(data_loader),
                desc='Valid iteration (split=%s)=%d' % (split, self.state.iteration), ncols=80,
                leave=False):

            should_visualize = len(segmentation_visualizations) < num_images_to_visualize
            if not (should_compute_basic_metrics or should_visualize):
                # Don't waste computation if we don't need to run on the remaining images
                continue
            true_labels_sb, pred_labels_sb, score_sb, pred_permutations_sb, val_loss_sb, \
            segmentation_visualizations_sb, score_visualizations_sb = \
                self.validate_single_batch(img_data, lbls[0], lbls[1], data_loader=data_loader,
                                           should_visualize=should_visualize)

            label_trues += true_labels_sb
            label_preds += pred_labels_sb
            val_loss += val_loss_sb
            scores += [score_sb]
            pred_permutations += [pred_permutations_sb]
            segmentation_visualizations += segmentation_visualizations_sb
            score_visualizations += score_visualizations_sb

        if should_export_visualizations:
            self.exporter.export_score_and_seg_images(segmentation_visualizations, score_visualizations,
                                                      self.state.iteration, split)
        val_loss /= len(data_loader)
        self.last_val_loss = val_loss

        val_metrics = self.exporter.run_post_val_epoch(label_preds, label_trues, pred_permutations,
                                                       should_compute_basic_metrics, split, val_loss, val_metrics,
                                                       write_basic_metrics, write_instance_metrics,
                                                       self.state.epoch, self.state.iteration, self.model)
        if save_checkpoint:
            self.save_checkpoint_and_update_if_best(mean_iu=val_metrics[2])

        # Restore training settings set prior to function call
        if training:
            self.model.train()

        visualizations = (segmentation_visualizations, score_visualizations)
        return val_loss, val_metrics, visualizations

    def save_checkpoint_and_update_if_best(self, mean_iu):
        current_checkpoint_file = self.exporter.save_checkpoint(self.state.epoch, self.state.iteration, self.model,
                                                                self.optim, self.best_mean_iu)
        if mean_iu > self.best_mean_iu or self.best_mean_iu == 0:
            self.best_mean_iu = mean_iu
            self.exporter.copy_checkpoint_as_best(current_checkpoint_file)

    def validate_single_batch(self, img_data, sem_lbl, inst_lbl, data_loader, should_visualize):

        full_input, sem_lbl, inst_lbl = self.prepare_data_for_forward_pass(img_data, (sem_lbl, inst_lbl),
                                                                           requires_grad=False)
        imgs = img_data.cpu()

        score = self.model(full_input)
        pred_permutations, loss, _ = self.compute_loss(score, sem_lbl, inst_lbl, val_matching_override=True)
        val_loss = float(loss.data[0])
        true_labels, pred_labels, segmentation_visualizations, score_visualizations = \
            self.exporter.run_post_val_iteration(
                imgs, inst_lbl, pred_permutations, score, sem_lbl, should_visualize,
                data_to_img_transformer=lambda i, l: self.exporter.untransform_data(data_loader, i, l))
        return true_labels, pred_labels, score, pred_permutations, val_loss, segmentation_visualizations, \
               score_visualizations

    def train_epoch(self):
        self.model.train()
        if self.lr_scheduler is not None:
            val_loss, val_metrics, _ = self.validate_split('val')
            self.lr_scheduler.step(val_loss, epoch=self.state.epoch)

        if self.generate_new_synthetic_data_each_epoch:
            seed = np.random.randint(100)
            self.train_loader.dataset.raw_dataset.initialize_locations_per_image(seed)
            self.train_loader_for_val.dataset.raw_dataset.initialize_locations_per_image(seed)

        for batch_idx, (img_data, target) in tqdm.tqdm(  # tqdm: progress bar
                enumerate(self.train_loader), total=len(self.train_loader),
                desc='Train epoch=%d' % self.state.epoch, ncols=80, leave=False):

            # Check/update iteration
            iteration = batch_idx + self.state.epoch * len(self.train_loader)
            if self.state.iteration != 0 and (iteration - 1) != self.state.iteration:
                continue  # for resuming
            self.state.iteration = iteration

            # Run validation epochs if it's time
            if self.state.iteration % self.interval_validate == 0:
                self.validate_all_splits()

            # Run training iteration
            self.train_iteration(img_data, target)

            if self.state.training_complete():
                self.validate_all_splits()
                break

    def train_iteration(self, img_data, target):
        assert self.model.training
        full_input, sem_lbl, inst_lbl = self.prepare_data_for_forward_pass(img_data, target, requires_grad=True)
        self.optim.zero_grad()
        score = self.model(full_input)
        pred_permutations, loss, loss_components = self.compute_loss(score, sem_lbl, inst_lbl)
        debug_check_values_are_valid(loss, score, self.state.iteration)

        # if 1:
        #     prediction = self.loss_object.transform_scores_to_predictions(score)
        #     cost_matrices_as_list = self.loss_object.build_all_sem_cls_cost_matrices_as_tensor_data(
        #         prediction[0, ...], sem_lbl[0, ...], inst_lbl[0, ...])
        #
        #     import ipdb; ipdb.set_trace()
        loss.backward()
        self.optim.step()

        try:
            iteration = self.state.iteration
            upscore8_grad = self.model.upscore8.weight.grad
            for channel_idx, channel_name in enumerate(self.instance_problem.get_channel_labels()):
                self.exporter.tensorboard_writer.add_histogram('Z_upscore8_gradients/{}'.format(channel_idx),
                                                               upscore8_grad[channel_idx, :, :, :], self.state.iteration)
            score_pool4_weight_grad = self.model.score_pool4.weight.grad
            score_pool4_bias_grad = self.model.score_pool4.bias.grad
            assert len(score_pool4_bias_grad) == self.instance_problem.n_classes
            for channel_idx, channel_name in enumerate(self.instance_problem.get_channel_labels()):
                self.exporter.tensorboard_writer.add_histogram('Z_score_pool4_weight_gradients/{}'.format(channel_idx),
                                                               score_pool4_weight_grad[channel_idx, :, :, :], self.state.iteration)
                self.exporter.tensorboard_writer.add_histogram('score_pool4_bias_gradients/{}'.format(channel_idx),
                                                               score_pool4_bias_grad[channel_idx], self.state.iteration)
        except:
            import ipdb;
            ipdb.set_trace()
            raise

        if self.exporter.run_loss_updates:
            self.model.eval()
            new_score = self.model(full_input)
            new_pred_permutations, new_loss, new_loss_components = self.compute_loss(new_score, sem_lbl,
                                                                                     inst_lbl)
            # num_reassignments = np.sum(new_pred_permutations != pred_permutations)
            # if not num_reassignments == 0:
            #     self.debug_loss(score, sem_lbl, inst_lbl, new_score, new_loss, loss_components, new_loss_components)

            self.model.train()

        else:
            new_pred_permutations, new_loss = None, None

        group_lrs = []
        for grp_idx, param_group in enumerate(self.optim.param_groups):
            group_lr = self.optim.param_groups[grp_idx]['lr']
            group_lrs.append(group_lr)
        self.exporter.run_post_train_iteration(full_input=full_input,
                                               inst_lbl=inst_lbl, sem_lbl=sem_lbl,
                                               loss=loss, loss_components=loss_components,
                                               pred_permutations=pred_permutations, score=score,
                                               epoch=self.state.epoch, iteration=self.state.iteration,
                                               new_pred_permutations=new_pred_permutations, new_loss=new_loss,
                                               get_activations_fcn=self.model.get_activations, lrs_by_group=group_lrs)

    def debug_loss(self, score, sem_lbl, inst_lbl, new_score, new_loss, loss_components, new_loss_components):
        predictions = self.loss_object.transform_scores_to_predictions(score)
        new_predictions = self.loss_object.transform_scores_to_predictions(new_score)
        old_cost_matrix = self.loss_object.build_all_sem_cls_cost_matrices_as_tensor_data(
            predictions[0, ...], sem_lbl[0, ...], inst_lbl[0, ...])
        new_cost_matrix = self.loss_object.build_all_sem_cls_cost_matrices_as_tensor_data(
            predictions[0, ...], sem_lbl[0, ...], inst_lbl[0, ...])
        ch_idx = 1
        pred_for_ch = predictions[0, ch_idx, :, :]
        binary_gt_for_ch = self.loss_object.get_binary_gt_for_channel(sem_lbl[0, ...], inst_lbl[0, ...], ch_idx)
        import ipdb;
        ipdb.set_trace()
        loss_component_example = self.loss_object.component_loss(single_channel_prediction=pred_for_ch,
                                                                 binary_target=binary_gt_for_ch)

    def train(self):
        max_epoch = int(math.ceil(1. * self.state.max_iteration / len(self.train_loader)))
        for epoch in tqdm.trange(self.state.epoch, max_epoch,
                                 desc='Train', ncols=80, leave=True):
            self.state.epoch = epoch
            self.train_epoch()
            if self.state.training_complete():
                break

    def validate_all_splits(self):
        val_loss, val_metrics, _ = self.validate_split('val')
        if self.train_loader_for_val is not None:
            train_loss, train_metrics, _ = self.validate_split('train')
        else:
            train_loss, train_metrics = None, None
        if train_loss is not None:
            self.exporter.update_mpl_joint_train_val_loss_figure(train_loss, val_loss, self.state.iteration)
        if self.exporter.tensorboard_writer is not None:
            self.exporter.tensorboard_writer.add_scalar('B_intermediate_metrics/val_minus_train_loss', val_loss -
                                                        train_loss,
                                                        self.state.iteration)
        return train_metrics, train_loss, val_metrics, val_loss


def debug_check_values_are_valid(loss, score, iteration):
    if is_nan(loss.data[0]):
        raise ValueError('losses is nan while training')
    if loss.data[0] > 1e4:
        print('WARNING: losses={} at iteration {}'.format(loss.data[0], iteration))
    if any_nan(score.data):
        raise ValueError('score is nan while training')

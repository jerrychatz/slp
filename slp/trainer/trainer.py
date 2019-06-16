import torch
import torch.nn as nn

from ignite.handlers import EarlyStopping
from ignite.contrib.handlers import ProgressBar
from ignite.engine import Engine, Events
from ignite.metrics import RunningAverage, Loss

from slp.trainer.handlers import CheckpointHandler, EvaluationHandler
from slp.util import from_checkpoint, to_device
from slp.util import log
from slp.util import system

LOGGER = log.getLogger('default')


class Trainer(object):
    def __init__(self,
                 model,
                 optimizer,
                 checkpoint_dir='../../checkpoints',
                 experiment_name='experiment',
                 model_checkpoint=None,
                 optimizer_checkpoint=None,
                 metrics=None,
                 patience=10,
                 validate_every=1,
                 accumulation_steps=1,
                 loss_fn=nn.CrossEntropyLoss(),
                 non_blocking=True,
                 dtype=torch.float,
                 device='cpu'):
        self.dtype = dtype
        self.non_blocking = non_blocking
        self.device = device
        self.loss_fn = loss_fn
        self.validate_every = validate_every
        self.patience = patience
        self.accumulation_steps = accumulation_steps
        self.checkpoint_dir = checkpoint_dir

        model_checkpoint = self._check_checkpoint(model_checkpoint)
        optimizer_checkpoint = self._check_checkpoint(optimizer_checkpoint)

        if metrics is None:
            metrics = {}
        if 'loss' not in metrics:
            metrics['loss'] = Loss(self.loss_fn)
        self.model = (from_checkpoint(model_checkpoint,
                                      model,
                                      map_location=torch.device('cpu'))
                      .type(dtype)
                      .to(device))
        self.optimizer = from_checkpoint(optimizer_checkpoint,
                                         optimizer)

        self.trainer = Engine(self.train_step)
        self.train_evaluator = Engine(self.eval_step)
        self.valid_evaluator = Engine(self.eval_step)
        for name, metric in metrics.items():
            metric.attach(self.train_evaluator, name)
            metric.attach(self.valid_evaluator, name)

        self.pbar = ProgressBar()
        self.val_pbar = ProgressBar(desc='Validation')

        self.checkpoint = CheckpointHandler(
            checkpoint_dir, experiment_name, score_name='validation_loss',
            score_function=self._score_fn, n_saved=2,
            require_empty=False, save_as_state_dict=True)

        self.early_stop = EarlyStopping(
            patience, self._score_fn, self.trainer)

        self.val_handler = EvaluationHandler(pbar=self.pbar,
                                             validate_every=1,
                                             early_stopping=self.early_stop)
        self.attach()

    def _check_checkpoint(self, ckpt):
        if system.is_url(ckpt):
            ckpt = system.download_url(ckpt, self.checkpoint_dir)
        return ckpt

    @staticmethod
    def _score_fn(engine):
        """Returns the scoring metric for checkpointing and early stopping

        Args:
            engine (ignite.engine.Engine): The engine that calculates
            the val loss

        Returns:
            (float): The validation loss
        """
        return -engine.state.metrics['loss']

    def parse_batch(self, batch):
        inputs = to_device(batch[0],
                           device=self.device,
                           non_blocking=self.non_blocking)
        targets = to_device(batch[1],
                            device=self.device,
                            non_blocking=self.non_blocking)
        return inputs, targets

    def get_predictions_and_targets(self, batch):
        inputs, targets = self.parse_batch(batch)
        y_pred = self.model(inputs)
        return y_pred, targets

    def train_step(self, engine, batch):
        self.model.train()
        y_pred, targets = self.get_predictions_and_targets(batch)
        loss = self.loss_fn(y_pred, targets)
        loss = loss / self.accumulation_steps
        loss.backward()
        if (self.trainer.state.iteration + 1) % self.accumulation_steps == 0:
            self.optimizer.step()
            self.optimizer.zero_grad()
        return loss.item()

    def eval_step(self, engine, batch):
        self.model.eval()
        with torch.no_grad():
            y_pred, targets = self.get_predictions_and_targets(batch)
            return y_pred, targets

    def predict(self, dataloader):
        self.evaluator.run(dataloader)

    def fit(self, train_loader, val_loader, epochs=50):
        self.val_handler.attach(self.trainer,
                                self.train_evaluator,
                                train_loader,
                                validation=False)
        self.val_handler.attach(self.trainer,
                                self.valid_evaluator,
                                val_loader,
                                validation=True)
        self.model.zero_grad()
        self.trainer.run(train_loader, max_epochs=epochs)

    def attach(self):
        ra = RunningAverage(output_transform=lambda x: x)
        ra.attach(self.trainer, "Train Loss")
        self.pbar.attach(self.trainer, ['Train Loss'])
        self.val_pbar.attach(self.train_evaluator)
        self.val_pbar.attach(self.valid_evaluator)
        self.valid_evaluator.add_event_handler(Events.COMPLETED,
                                               self.early_stop)
        ckpt = {
            'model': self.model,
            'optimizer': self.optimizer
        }
        self.valid_evaluator.add_event_handler(Events.COMPLETED,
                                               self.checkpoint,
                                               ckpt)

        def graceful_exit(engine, e):
            if isinstance(e, KeyboardInterrupt):
                engine.terminate()
                LOGGER.warn("CTRL-C caught. Exiting gracefully...")
            else:
                raise(e)

        self.trainer.add_event_handler(Events.EXCEPTION_RAISED, graceful_exit)
        self.train_evaluator.add_event_handler(Events.EXCEPTION_RAISED,
                                               graceful_exit)
        self.valid_evaluator.add_event_handler(Events.EXCEPTION_RAISED,
                                               graceful_exit)


class AutoencoderTrainer(Trainer):
    def parse_batch(self, batch):
        inputs = to_device(batch[0],
                           device=self.device,
                           non_blocking=self.non_blocking)
        return inputs, inputs


class SequentialTrainer(Trainer):
    def parse_batch(self, batch):
        inputs = to_device(batch[0],
                           device=self.device,
                           non_blocking=self.non_blocking)
        targets = to_device(batch[1],
                            device=self.device,
                            non_blocking=self.non_blocking)
        lengths = to_device(batch[2],
                            device=self.device,
                            non_blocking=self.non_blocking)
        return inputs, targets, lengths

    def get_predictions_and_targets(self, batch):
        inputs, targets, lengths = self.parse_batch(batch)
        y_pred = self.model(inputs, lengths)
        return y_pred, targets


class Seq2seqTrainer(SequentialTrainer):
    def parse_batch(self, batch):
        inputs = to_device(batch[0],
                           device=self.device,
                           non_blocking=self.non_blocking)
        lengths = to_device(batch[1],
                            device=self.device,
                            non_blocking=self.non_blocking)
        return inputs, inputs, lengths


class TransformerTrainer(Trainer):
    def parse_batch(self, batch):
        inputs = to_device(batch[0],
                           device=self.device,
                           non_blocking=self.non_blocking)
        targets = to_device(batch[1],
                            device=self.device,
                            non_blocking=self.non_blocking)
        pad_source_mask = to_device(batch[2],
                                    device=self.device,
                                    non_blocking=self.non_blocking)
        pad_target_mask = to_device(batch[3],
                                    device=self.device,
                                    non_blocking=self.non_blocking)
        subsequent_mask = to_device(batch[4],
                                    device=self.device,
                                    non_blocking=self.non_blocking)
        return (inputs, targets, pad_source_mask,
                pad_target_mask, subsequent_mask)

    def get_predictions_and_targets(self, batch):
        inputs, targets, pad_source_mask, pad_target_mask, subsequent_mask = \
            self.parse_batch(batch)
        y_pred = self.model(inputs,
                            targets,
                            pad_source_mask=pad_source_mask,
                            pad_target_mask=pad_target_mask,
                            subsequent_mask=subsequent_mask)
        # TODO: BEAMSEARCH!!
        return y_pred, targets
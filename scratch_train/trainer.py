from cmath import nan
from transformers.trainer_callback import (
    CallbackHandler,
    DefaultFlowCallback,
    PrinterCallback,
    ProgressCallback,
    TrainerCallback,
    TrainerControl,
    TrainerState,
)
from transformers.trainer_pt_utils import (
    DistributedLengthGroupedSampler,
    DistributedSamplerWithLoop,
    DistributedTensorGatherer,
    IterableDatasetShard,
    LabelSmoother,
    LengthGroupedSampler,
    SequentialDistributedSampler,
    ShardSampler,
    distributed_broadcast_scalars,
    distributed_concat,
    find_batch_size,
    get_parameter_names,
    nested_concat,
    nested_detach,
    nested_numpify,
    nested_truncate,
    nested_xla_mesh_reduce,
    reissue_pt_warnings,
)
from transformers.trainer_utils import (
    PREFIX_CHECKPOINT_DIR,
    BestRun,
    EvalLoopOutput,
    EvalPrediction,
    FSDPOption,
    HPSearchBackend,
    HubStrategy,
    IntervalStrategy,
    PredictionOutput,
    RemoveColumnsCollator,
    ShardedDDPOption,
    TrainerMemoryTracker,
    TrainOutput,
    default_compute_objective,
    default_hp_space,
    denumpify_detensorize,
    enable_full_determinism,
    find_executable_batch_size,
    get_last_checkpoint,
    has_length,
    number_of_arguments,
    seed_worker,
    set_seed,
    speed_metrics,
)
from transformers.training_args import OptimizerNames, ParallelMode, TrainingArguments
from transformers.utils import (
    CONFIG_NAME,
    WEIGHTS_INDEX_NAME,
    WEIGHTS_NAME,
    find_labels,
    get_full_repo_name,
    is_apex_available,
    is_datasets_available,
    is_in_notebook,
    is_sagemaker_dp_enabled,
    is_sagemaker_mp_enabled,
    is_torch_tpu_available,
    logging,
)
from transformers import Trainer, DataCollator, TrainingArguments, PreTrainedTokenizerBase, EvalPrediction, TrainerCallback
from transformers.modeling_utils import PreTrainedModel
from transformers.file_utils import is_sagemaker_mp_enabled
from transformers.trainer_pt_utils import nested_detach
from transformers.trainer_utils import EvalLoopOutput, has_length
import numpy as np
import torch
import torch.distributed as dist
from packaging import version
from torch.utils.data import DataLoader, Dataset, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from transformers.utils import logging
from huggingface_hub import Repository
import torch.nn.functional as F
from torch import nn
from torch.cuda.amp import autocast
import os
logger = logging.get_logger(__name__)
from logging import log
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union


class SMTrainer(Trainer):
    def __init__(
        self,
        model: Union[PreTrainedModel, torch.nn.Module] = None,
        args: TrainingArguments = None,
        data_collator: Optional[DataCollator] = None,
        train_dataset: Optional[Dataset] = None,
        eval_dataset: Optional[Dataset] = None,
        tokenizer: Optional[PreTrainedTokenizerBase] = None,
        model_init: Callable[[], PreTrainedModel] = None,
        compute_metrics: Optional[Callable[[EvalPrediction], Dict]] = None,
        callbacks: Optional[List[TrainerCallback]] = None,
        optimizers: Tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR] = (None, None),
    ):
        super().__init__(model=model, args=args, data_collator=data_collator, train_dataset=train_dataset,
        eval_dataset=eval_dataset, tokenizer=tokenizer, model_init=model_init, compute_metrics=compute_metrics,
        callbacks=callbacks, optimizers=optimizers)
        print(args)

    
    def evaluation_loop(
        self,
        dataloader: DataLoader,
        description: str,
        prediction_loss_only: Optional[bool] = None,
        ignore_keys: Optional[List[str]] = None,
        metric_key_prefix: str = "eval",
    ) -> EvalLoopOutput:
        """
        Prediction/evaluation loop, shared by `Trainer.evaluate()` and `Trainer.predict()`.

        Works both with or without labels.
        """
        args = self.args

        prediction_loss_only = prediction_loss_only if prediction_loss_only is not None else args.prediction_loss_only

        # if eval is called w/o train init deepspeed here
        if args.deepspeed and not self.deepspeed:

            # XXX: eval doesn't have `resume_from_checkpoint` arg but we should be able to do eval
            # from the checkpoint eventually
            deepspeed_engine, _, _ = deepspeed_init(
                self, num_training_steps=0, resume_from_checkpoint=None, inference=True
            )
            self.model = deepspeed_engine.module
            self.model_wrapped = deepspeed_engine
            self.deepspeed = deepspeed_engine

        model = self._wrap_model(self.model, training=False)

        # if full fp16 or bf16 eval is wanted and this ``evaluation`` or ``predict`` isn't called
        # while ``train`` is running, cast it to the right dtype first and then put on device
        if not self.is_in_train:
            if args.fp16_full_eval:
                model = model.to(dtype=torch.float16, device=args.device)
            elif args.bf16_full_eval:
                model = model.to(dtype=torch.bfloat16, device=args.device)

        batch_size = self.args.eval_batch_size

        logger.info(f"***** Running {description} *****")
        if has_length(dataloader):
            logger.info(f"  Num examples = {self.num_examples(dataloader)}")
        else:
            logger.info("  Num examples: Unknown")
        logger.info(f"  Batch size = {batch_size}")

        model.eval()

        self.callback_handler.eval_dataloader = dataloader
        # Do this before wrapping.
        eval_dataset = getattr(dataloader, "dataset", None)

        if is_torch_tpu_available():
            dataloader = pl.ParallelLoader(dataloader, [args.device]).per_device_loader(args.device)

        if args.past_index >= 0:
            self._past = None

        # Initialize containers
        # losses/preds/labels on GPU/TPU (accumulated for eval_accumulation_steps)
        losses_host = None
        preds_host = None
        labels_host = None
        inputs_host = None

        # losses/preds/labels on CPU (final containers)
        all_losses = None
        all_preds = None
        all_labels = None
        all_inputs = None
        # Will be useful when we have an iterable dataset so don't know its length.

        observed_num_examples = 0
        # Main evaluation loop
        cal_param_accuracy = []
        for step, inputs in enumerate(dataloader):
            # Update the observed num examples
            observed_batch_size = find_batch_size(inputs)
            if observed_batch_size is not None:
                observed_num_examples += observed_batch_size
                # For batch samplers, batch_size is not known by the dataloader in advance.
                if batch_size is None:
                    batch_size = observed_batch_size

            # Prediction step
            prediction_loss_only = False#done since args not working properly
            loss, logits, labels = self.prediction_step(model, inputs, prediction_loss_only, ignore_keys=ignore_keys)
            
            _, prediction = torch.max(logits, -1)
            cal_param_accuracy.append(self.compute_TP(prediction, labels))
            prediction_loss_only, logits, labels = True, None, None#done since logits/labels tensor should not be stored at gpu for memenory efficeny

            inputs_decode = inputs["input_ids"] if args.include_inputs_for_metrics else None
            
            if is_torch_tpu_available():
                xm.mark_step()

            # Update containers on host
            if loss is not None:
                losses = self._nested_gather(loss.repeat(batch_size))
                losses_host = losses if losses_host is None else torch.cat((losses_host, losses), dim=0)
            if labels is not None:
                labels = self._pad_across_processes(labels)
                labels = self._nested_gather(labels)
                labels_host = labels if labels_host is None else nested_concat(labels_host, labels, padding_index=-100)
            if inputs_decode is not None:
                inputs_decode = self._pad_across_processes(inputs_decode)
                inputs_decode = self._nested_gather(inputs_decode)
                inputs_host = (
                    inputs_decode
                    if inputs_host is None
                    else nested_concat(inputs_host, inputs_decode, padding_index=-100)
                )
            if logits is not None:
                logits = self._pad_across_processes(logits)
                logits = self._nested_gather(logits)
                if self.preprocess_logits_for_metrics is not None:
                    logits = self.preprocess_logits_for_metrics(logits, labels)
                preds_host = logits if preds_host is None else nested_concat(preds_host, logits, padding_index=-100)
            self.control = self.callback_handler.on_prediction_step(args, self.state, self.control)

            # Gather all tensors and put them back on the CPU if we have done enough accumulation steps.
            if args.eval_accumulation_steps is not None and (step + 1) % args.eval_accumulation_steps == 0:
                if losses_host is not None:
                    losses = nested_numpify(losses_host)
                    all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)
                if preds_host is not None:
                    logits = nested_numpify(preds_host)
                    all_preds = logits if all_preds is None else nested_concat(all_preds, logits, padding_index=-100)
                if inputs_host is not None:
                    inputs_decode = nested_numpify(inputs_host)
                    all_inputs = (
                        inputs_decode
                        if all_inputs is None
                        else nested_concat(all_inputs, inputs_decode, padding_index=-100)
                    )
                if labels_host is not None:
                    labels = nested_numpify(labels_host)
                    all_labels = (
                        labels if all_labels is None else nested_concat(all_labels, labels, padding_index=-100)
                    )

                # Set back to None to begin a new accumulation
                losses_host, preds_host, inputs_host, labels_host = None, None, None, None

        if args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of the evaluation loop
            delattr(self, "_past")

        # Gather all remaining tensors and put them back on the CPU
        if losses_host is not None:
            losses = nested_numpify(losses_host)
            all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)
        if preds_host is not None:
            logits = nested_numpify(preds_host)
            all_preds = logits if all_preds is None else nested_concat(all_preds, logits, padding_index=-100)
        if inputs_host is not None:
            inputs_decode = nested_numpify(inputs_host)
            all_inputs = (
                inputs_decode if all_inputs is None else nested_concat(all_inputs, inputs_decode, padding_index=-100)
            )
        if labels_host is not None:
            labels = nested_numpify(labels_host)
            all_labels = labels if all_labels is None else nested_concat(all_labels, labels, padding_index=-100)

        # Number of samples
        if has_length(eval_dataset):
            num_samples = len(eval_dataset)
        # The instance check is weird and does not actually check for the type, but whether the dataset has the right
        # methods. Therefore we need to make sure it also has the attribute.
        elif isinstance(eval_dataset, IterableDatasetShard) and hasattr(eval_dataset, "num_examples"):
            num_samples = eval_dataset.num_examples
        else:
            if has_length(dataloader):
                num_samples = self.num_examples(dataloader)
            else:  # both len(dataloader.dataset) and len(dataloader) fail
                num_samples = observed_num_examples

        # Number of losses has been rounded to a multiple of batch_size and in a distributed training, the number of
        # samplers has been rounded to a multiple of batch_size, so we truncate.
        if all_losses is not None:
            all_losses = all_losses[:num_samples]
        if all_preds is not None:
            all_preds = nested_truncate(all_preds, num_samples)
        if all_labels is not None:
            all_labels = nested_truncate(all_labels, num_samples)
        if all_inputs is not None:
            all_inputs = nested_truncate(all_inputs, num_samples)

        # Metrics!
        if self.compute_metrics is not None and all_preds is not None and all_labels is not None:
            if args.include_inputs_for_metrics:
                metrics = self.compute_metrics(
                    EvalPrediction(predictions=all_preds, label_ids=all_labels, inputs=all_inputs)
                )
            else:
                metrics = self.compute_metrics(EvalPrediction(predictions=all_preds, label_ids=all_labels))
        else:
            metrics = {}

        # To be JSON-serializable, we need to remove numpy types or zero-d tensors
        metrics = denumpify_detensorize(metrics)

        if all_losses is not None:
            metrics[f"{metric_key_prefix}_loss"] = all_losses.mean().item()
        # logging accuracy metrics:
        metrics["eval_accuracy"] = self.compute_accuracy(cal_param_accuracy)
        # Prefix all keys with metric_key_prefix + '_'
        for key in list(metrics.keys()):
            if not key.startswith(f"{metric_key_prefix}_"):
                metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)

        return EvalLoopOutput(predictions=all_preds, label_ids=all_labels, metrics=metrics, num_samples=num_samples)

    def compute_TP(self, pred, true):
        '''returns list: [number of correctly classfied, number of samples]
        '''
        # max_len = max(list(map(lambda a:len(a), pred)))
        # pred, true = fill_batch(pred, max_len, 0), fill_batch(true, max_len, -100)

        pred, true = np.array(pred.cpu()), np.array(true.cpu())
        
        acc = np.sum(np.multiply((pred == true),(true != -100))), np.sum(true != -100)
        return acc
    def compute_accuracy(self, acc):
        '''
        input: acc : [[correctly classfied, number of samples], [], [], [], ..., ] list of list for each prediction
        output: accuracy float
        '''
        tp, total = 0, 0
        for item in acc:
            tp += item[0]
            total += item[1]
        return tp/total

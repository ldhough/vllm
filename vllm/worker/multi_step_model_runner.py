import dataclasses
import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

try:
    from vllm.attention.backends.flash_attn import FlashAttentionMetadata
except ModuleNotFoundError:
    # vllm_flash_attn is not installed, use the identical ROCm FA metadata
    from vllm.attention.backends.rocm_flash_attn import (
        ROCmFlashAttentionMetadata as FlashAttentionMetadata)

import torch

from vllm import _custom_ops as ops
from vllm.distributed import get_pp_group
from vllm.logger import init_logger
from vllm.sequence import (CompletionSequenceGroupOutput, IntermediateTensors,
                           Logprob, SamplerOutput, SequenceGroupMetadata,
                           SequenceOutput)
from vllm.worker.model_runner import (GPUModelRunnerBase,
                                      ModelInputForGPUWithSamplingMetadata)
from vllm.worker.model_runner_base import (
    BroadcastableModelInput, _init_attn_metadata_from_tensor_dict,
    _init_frozen_model_input_from_tensor_dict,
    _init_sampling_metadata_from_tensor_dict)

from ..model_executor.model_loader.tensorizer import TensorizerConfig

if TYPE_CHECKING:
    from vllm.attention.backends.abstract import AttentionBackend

logger = init_logger(__name__)


@dataclass
class ModelOutput:
    """The output of a single model forward pass.

    The sampler_output_ready_event is set when the tensors in
    sampler_output are ready (the model+sampler forward pass has
    completed). We use the event to synchronize the GPU->CPU transfer,
    which we want to only run when the data has been written to the
    GPU tensors. Until the event is ready, the tensors in sampler_output
    will have garbage data.

    There are two scenarios:
    1. The output tensors are ready and we can pythonize them immediately.
    2. The output tensors are not ready and we need to wait for the event to be
    ready.
    """
    sampler_output: SamplerOutput
    sampler_output_ready_event: torch.cuda.Event
    sampled_token_ids: Optional[torch.Tensor] = None
    pythonized: bool = False

    def pythonize(self, input_metadata: "StatefulModelInput",
                  copy_stream: torch.cuda.Stream,
                  pinned_sampled_token_buffer: torch.Tensor) -> None:
        """Pythonize the output. Blocking."""
        if not self.pythonized:
            self._pythonize_sampler_output(input_metadata, copy_stream,
                                           pinned_sampled_token_buffer, True)
            self.pythonized = True

    def maybe_pythonize(self, input_metadata: "StatefulModelInput",
                        copy_stream: torch.cuda.Stream,
                        pinned_sampled_token_buffer: torch.Tensor) -> None:
        """Pythonize the output if ready, else return None. Non-blocking."""
        if not self.pythonized:
            self.pythonized = self._pythonize_sampler_output(
                input_metadata, copy_stream, pinned_sampled_token_buffer,
                False)

    def _pythonize_sampler_output(self, input_metadata: "StatefulModelInput",
                                  copy_stream: torch.cuda.Stream,
                                  pinned_sampled_token_buffer: torch.Tensor,
                                  blocking: bool) -> bool:
        """
        If blocking is set, will block until the forward pass for the output is
        ready and pythonize the output.  
        """
        assert self.sampled_token_ids is not None
        if not blocking and not self.sampler_output_ready_event.query():
            return False

        if blocking:
            self.sampler_output_ready_event.synchronize()
        with torch.cuda.stream(copy_stream):
            _pythonize_sampler_output(input_metadata, self.sampler_output,
                                      pinned_sampled_token_buffer,
                                      self.sampled_token_ids)
        return True


@dataclass(frozen=False)
class StatefulModelInput(BroadcastableModelInput):
    # actual frozen model input dataclass passed to _base_model_runner
    frozen_model_input: Optional[ModelInputForGPUWithSamplingMetadata] = None

    # list of model outputs for each step, may not be all pythonized
    cached_outputs: List[ModelOutput] = field(default_factory=list)

    # used to pass sampled token ids from the last step to the current step for
    # TP workers. Used to append to end of outputs and used by advance_step
    last_sampled_token_ids: Optional[torch.Tensor] = None
    current_step: int = 0
    is_multi_step: bool = True
    is_last_step: bool = False
    is_first_multi_step: bool = False
    # ping-pong data structures for multi-step to wait on the previous step
    step_cuda_events: List[torch.cuda.Event] = field(
        default_factory=lambda: [torch.cuda.Event(blocking=True)] * 2)
    num_seqs: int = -1
    num_queries: int = -1

    def as_broadcastable_tensor_dict(self) -> Dict[str, Any]:
        assert self.frozen_model_input is not None
        tensor_dict = self.frozen_model_input.as_broadcastable_tensor_dict()
        new_tensor_dict = {
            'last_sampled_token_ids': self.last_sampled_token_ids,
            'current_step': self.current_step,
            'is_multi_step': self.is_multi_step,
            'is_last_step': self.is_last_step,
            'is_first_multi_step': self.is_first_multi_step,
            'num_seqs': self.num_seqs,
            'num_queries': self.num_queries,
        }
        tensor_dict.update(new_tensor_dict)
        return tensor_dict

    @classmethod
    def from_broadcasted_tensor_dict(
        cls,
        tensor_dict: Dict[str, Any],
        attn_backend: Optional["AttentionBackend"] = None,
    ) -> "StatefulModelInput":
        tensor_dict = _init_sampling_metadata_from_tensor_dict(tensor_dict)
        if attn_backend is not None:
            tensor_dict = _init_attn_metadata_from_tensor_dict(
                attn_backend, tensor_dict)
        tensor_dict = _init_frozen_model_input_from_tensor_dict(
            ModelInputForGPUWithSamplingMetadata, tensor_dict)

        return cls(**tensor_dict)

    def record_step_event(self, current_stream: torch.cuda.Stream):
        # record the event for the current step so that the next step can sync
        # on it. We modulo by 2 to keep the events in a circular buffer and
        # support any attn backends that may be supported in the future. ie
        # Flashinfer would want two DecodeWrappers to overlap the CPU and GPU.
        self.step_cuda_events[self.current_step & 1] = \
            torch.cuda.Event(blocking=True)
        self.step_cuda_events[self.current_step & 1].record(current_stream)

    def wait_previous_step(self):
        # These cuda events are an explicit synchronization to ensure that
        # advance_step() (for other attn backends that may be supported in the
        # future) do not clobber any data structures that is also used by any
        # enqueued forwards steps. For distributed case, only a single event is
        # needed, but for single GPU case, since we can let the CPU run much
        # further ahead, two events allow us to overlap the advance_step with
        # the previous forward (ie using two DecodeWrappers for flashinfer
        # backend)
        self.step_cuda_events[(self.current_step + 1) & 1].wait()

    def add_sampler_output(self,
                           sampler_output: SamplerOutput,
                           sampled_token_ids: Optional[torch.Tensor] = None):
        self.cached_outputs.append(
            ModelOutput(sampler_output=sampler_output,
                        sampler_output_ready_event=None,
                        sampled_token_ids=sampled_token_ids,
                        pythonized=False))


# MutableModelInputForGPUWithMultiStepMetadata is not subclass of
# ModelInputForGPU but it wraps the actual input dataclass and adds multi-step
# metadata
# mypy: disable-error-code=type-var
class MultiStepModelRunner(GPUModelRunnerBase[StatefulModelInput]):
    # mypy: enable-error-code=type-var

    def __init__(self, base_model_runner: GPUModelRunnerBase, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # uses the base model runner to execute the model and wraps it with
        # multi-step logic
        self._base_model_runner: GPUModelRunnerBase = base_model_runner

        self.is_multi_step = self.scheduler_config.is_multi_step
        # used to copy tensors from GPU to CPU asynchronously
        self._copy_stream = torch.cuda.Stream()
        self.pinned_sampled_token_ids: Optional[torch.Tensor] = None

    def make_model_input_from_broadcasted_tensor_dict(
            self, tensor_dict: Dict[str, Any]) -> StatefulModelInput:
        model_input = (StatefulModelInput.from_broadcasted_tensor_dict(
            tensor_dict,
            attn_backend=self.attn_backend,
        ))
        return model_input

    def prepare_model_input(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        virtual_engine: int = 0,
        finished_requests_ids: Optional[List[str]] = None
    ) -> StatefulModelInput:
        frozen_model_input = self._base_model_runner.prepare_model_input(
            seq_group_metadata_list, virtual_engine, finished_requests_ids)

        model_input = StatefulModelInput(
            frozen_model_input=frozen_model_input,
            num_seqs=len(frozen_model_input.seq_lens),
            num_queries=len(frozen_model_input.query_lens),
        )
        return model_input

    def _async_process_outputs(self, model_input: StatefulModelInput,
                               output_proc_callback: Callable):
        # Proceed with pythonization and output_proc in order.
        # Stop on the first one that fails to pythonize
        cont = True
        for model_output in model_input.cached_outputs:
            if not model_output.pythonized:
                model_output.maybe_pythonize(model_input, self._copy_stream,
                                             self.pinned_sampled_token_ids)
                if model_output.pythonized:
                    output_proc_callback(
                        sampler_output=model_output.sampler_output)
                else:
                    cont = False

            if not cont:
                break

    def _final_process_outputs(self, model_input: StatefulModelInput,
                               output_proc_callback: Optional[Callable]):
        assert model_input.frozen_model_input is not None

        outputs = []
        for output_id in range(len(model_input.cached_outputs)):
            is_last_output = output_id == len(model_input.cached_outputs) - 1

            output = model_input.cached_outputs[output_id]
            if not output.pythonized:
                output.pythonize(model_input, self._copy_stream,
                                 self.pinned_sampled_token_ids)

                if model_input.frozen_model_input.use_async_and_multi_step:
                    assert output_proc_callback is not None
                    output_proc_callback(sampler_output=output.sampler_output,
                                         is_last_output=is_last_output)

            outputs.append(output.sampler_output)

        return outputs

    @torch.inference_mode()
    def execute_model(
        self,
        model_input: StatefulModelInput,
        kv_caches: List[torch.Tensor],
        intermediate_tensors: Optional[IntermediateTensors] = None,
        num_steps: int = 1,
    ) -> Optional[Union[List[SamplerOutput], IntermediateTensors]]:
        """ 
        Execute the model for a single step and update multi-step
        metadata
        """
        assert num_steps == 1, "MultiStepModelRunner only supports num_steps=1"
        frozen_model_input = model_input.frozen_model_input
        assert frozen_model_input is not None

        # path for warm up runs
        if not model_input.is_multi_step:
            return self._base_model_runner.execute_model(
                frozen_model_input, kv_caches, intermediate_tensors, num_steps)

        # make sure we skip the sampler on the lask rank and only pythonize
        # if CPU is ahead.
        if self.is_driver_worker and get_pp_group().is_last_rank:
            if self.pinned_sampled_token_ids is None:
                self.pinned_sampled_token_ids = torch.zeros(
                    (self.scheduler_config.max_num_seqs, 1),
                    dtype=torch.long,
                    device="cpu",
                    pin_memory=True)

            self._base_model_runner.model.sampler.include_gpu_probs_tensor = (
                True)
            if frozen_model_input.sampling_metadata:
                frozen_model_input.sampling_metadata.skip_sampler_cpu_output = (
                    True)

        # some pre-execute model logic for multi-step:
        #   - if it's the first step, we need to reset the sampling tensors
        #   - if it's not the first step, we need to advance the step using the
        #   appended sampler output from last iteration
        #   - also maybe pythonize if CPU is ahead of GPU

        current_stream = torch.cuda.current_stream()
        if not model_input.is_first_multi_step:
            # Explicitly block on the previous step's forward to make sure we
            # don't clobber any GPU tensors still in use.
            # This is not needed for flashattn backend, but for other attn
            # backends such as flashinfer that performs extra CPU operations on
            # input metadata we may need to synchronize any CPU operations that
            # might clobber enqueued forwards. (prevents CPU from running too
            # far ahead if needed)
            model_input.wait_previous_step()
            model_input = self._advance_step(
                model_input, model_input.cached_outputs[-1].sampler_output)

        output_proc_callback = None
        if frozen_model_input.use_async_and_multi_step:
            output_proc_callback = frozen_model_input.async_callback
            assert output_proc_callback is not None
            async_callback = functools.partial(
                self._async_process_outputs,
                model_input=model_input,
                output_proc_callback=output_proc_callback)

            frozen_model_input = dataclasses.replace(  # type: ignore
                model_input.frozen_model_input,
                async_callback=async_callback)
            assert frozen_model_input is not None

        # Execute the model
        output = self._base_model_runner.execute_model(frozen_model_input,
                                                       kv_caches,
                                                       intermediate_tensors,
                                                       num_steps=1)

        # record the event for the current step so that the next step can sync
        model_input.record_step_event(current_stream)

        if get_pp_group().is_last_rank and self.is_driver_worker:
            assert len(
                output
            ) == 1, "MultiStepModelRunner requires single-step base_models"

            # event for the pythonization so that we only pythonize if the
            # tensors are ready. May be able to be combined with the step event
            output_ready_event = torch.cuda.Event()
            output_ready_event.record(current_stream)
            if self.parallel_config.pipeline_parallel_size > 1:
                output[0].sampled_token_ids_cpu = output[
                    0].sampled_token_ids.cpu()
            model_input.cached_outputs.append(
                ModelOutput(output[0], output_ready_event,
                            output[0].sampled_token_ids, False))
            # make sure we dont try to serialize any GPU tensors
            output[0].sampled_token_ids = None
            output[0].sampled_token_probs = None
            output[0].logprobs = None
            # Pythonize the output if CPU is ahead and the previous step is
            # ready.
            if not frozen_model_input.use_async_and_multi_step:
                for model_output in model_input.cached_outputs:
                    model_output.maybe_pythonize(model_input,
                                                 self._copy_stream,
                                                 self.pinned_sampled_token_ids)

        model_input.current_step += 1

        if not get_pp_group().is_last_rank:
            # Should be IntermediateTensors
            assert isinstance(output, IntermediateTensors)
            return output
        if not self.is_driver_worker:
            return []

        # Pythonize the output and block if needed since it is the last step
        if model_input.is_last_step:
            outputs = self._final_process_outputs(model_input,
                                                  output_proc_callback)
            return outputs

        # should be [SamplerOutput]
        return output

    def _update_sampling_metadata(self, sampling_metadata, num_seqs,
                                  num_queries):

        assert sampling_metadata.num_prompts == 0
        assert len(sampling_metadata.seq_groups) == num_queries
        assert sampling_metadata.selected_token_indices.shape == (
            num_queries, )
        # assert sampling_metadata.categorized_sample_indices == TODO: Add if needed # noqa: E501

        # Verify that all sequences are decodes
        for i in range(num_queries):
            seq_group = sampling_metadata.seq_groups[i]

            assert seq_group.is_prompt is False  # No prompt
            assert seq_group.prompt_logprob_indices == []  # No prompt
            assert seq_group.sample_indices == [i]  # Simple
            assert seq_group.seq_len is None  # Decode
            assert seq_group.query_len is None  # Decode

    def _advance_step(self, model_input: StatefulModelInput,
                      out: SamplerOutput) -> StatefulModelInput:
        frozen_model_input = model_input.frozen_model_input
        assert frozen_model_input is not None
        assert frozen_model_input.attn_metadata is not None

        num_seqs = model_input.num_seqs
        num_queries = model_input.num_queries
        assert num_seqs > 0
        assert num_queries > 0
        assert num_seqs >= num_queries

        attn_metadata = frozen_model_input.attn_metadata
        assert isinstance(attn_metadata, FlashAttentionMetadata)
        attn_metadata.advance_step(num_seqs, num_queries)

        # Update GPU tensors
        ops.advance_step(
            num_seqs=num_seqs,
            num_queries=num_queries,
            block_size=self.block_size,
            input_tokens=frozen_model_input.input_tokens,
            sampled_token_ids=model_input.cached_outputs[-1].sampled_token_ids,
            input_positions=frozen_model_input.input_positions,
            seq_lens=attn_metadata.seq_lens_tensor,
            slot_mapping=attn_metadata.slot_mapping,
            block_tables=attn_metadata.block_tables)

        if frozen_model_input.seq_lens is not None:
            for i in range(num_queries):
                frozen_model_input.seq_lens[i] = attn_metadata.seq_lens[i]

        return model_input

    def load_model(self) -> None:
        return self._base_model_runner.load_model()

    def save_sharded_state(
        self,
        path: str,
        pattern: Optional[str] = None,
        max_size: Optional[int] = None,
    ) -> None:
        return self._base_model_runner.save_sharded_state(
            path, pattern, max_size)

    def save_tensorized_model(self,
                              tensorizer_config: TensorizerConfig) -> None:
        return self._base_model_runner.save_tensorized_model(tensorizer_config)

    def profile_run(self) -> None:
        return self._base_model_runner.profile_run()

    def remove_all_loras(self):
        return self._base_model_runner.remove_all_loras()

    def capture_model(self, kv_caches: List[List]) -> None:
        return self._base_model_runner.capture_model(kv_caches)

    @property
    def vocab_size(self) -> int:
        return self._base_model_runner.vocab_size


def _pythonize_sampler_output(model_input: StatefulModelInput,
                              output: SamplerOutput,
                              pinned_sampled_token_buffer: torch.Tensor,
                              sampled_token_ids: torch.Tensor) -> None:
    """ This function is only called when the output tensors are ready. 
    See ModelOutput
    """

    assert model_input.frozen_model_input is not None

    frozen_model_input = model_input.frozen_model_input
    assert frozen_model_input.sampling_metadata is not None
    # samples generation should have been skipped
    assert not output.outputs

    pinned_buffer = pinned_sampled_token_buffer[:model_input.num_queries]

    # CPU GPU sync
    pinned_buffer = pinned_buffer.copy_(sampled_token_ids, non_blocking=False)

    # this will not block as the tensors are already on CPU
    samples_list = pinned_buffer.tolist()

    sampling_metadata = frozen_model_input.sampling_metadata

    for (seq_group, sample_result) in zip(sampling_metadata.seq_groups,
                                          samples_list):
        seq_ids = seq_group.seq_ids
        next_token_ids = sample_result
        parent_ids = [0]
        seq_outputs: List[SequenceOutput] = []
        if seq_group.sampling_params.logits_processors:
            assert len(seq_group.sampling_params.logits_processors) == 0, (
                "Logits Processors are not supported in multi-step decoding")
        for parent_id, next_token_id in zip(parent_ids, next_token_ids):
            # TODO(will): support logprobs
            # Hard coded logprob
            seq_outputs.append(
                SequenceOutput(seq_ids[parent_id], next_token_id,
                               {next_token_id: Logprob(logprob=-1)}))
        output.outputs.append(CompletionSequenceGroupOutput(seq_outputs, None))
    assert len(output.outputs) > 0

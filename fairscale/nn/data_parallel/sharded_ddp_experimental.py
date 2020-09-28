# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

"""
A distributed data parallel class that shards the model and the optimizer into pieces.

See https://github.com/pytorch/pytorch/issues/42849 for more context

"""

# from functools import partial
from typing import Any, Dict, Iterable, List, Type

import torch
from torch import nn

# import torch.distributed as dist
# from torch.distributed.algorithms.ddp_comm_hooks import DDPCommHookType, register_ddp_comm_hook


def _slice_module(module: nn.Sequential, number_shards: int) -> List[List[nn.Module]]:
    # Naive sharding for now, slice by the number of layers
    # This is probably suboptimal if the complexity or size of the layers vary by a lot
    def chunks(lst: List[nn.Module], n: int) -> Iterable[List[nn.Module]]:
        for i in range(0, len(lst), n):
            yield lst[i : i + n]

    return list(chunks(list(module.modules()), number_shards))


class ModelShard(nn.Module):
    """
    Wrap one shard of the model, make it possible to load parameters on the fly for the FW pass and gather gradients
    """

    def __init__(self, cpu_model_shard: nn.Module, owner_rank: int, pg: Any):
        super().__init__()
        self.owner_rank = owner_rank
        self.process_group = pg

        for i, (_, param) in enumerate(cpu_model_shard.named_parameters()):
            self.register_parameter(str(i), param)

    def forward_load(self) -> None:
        # materialize local GPU parameters, can be enhance with bucketing
        _ = list(
            map(
                lambda x: x.wait(),
                map(lambda p: self.process_group.broadcast(p, self.owner_rank, async_op=True), self.parameters()),
            )
        )

    def forward_drop(self) -> None:
        # drop all local parameters
        for p in self.parameters():
            p.data.set_(torch.zeros([0]))

    def reduce_grads(self) -> None:
        _ = list(
            map(
                lambda x: x.wait(),
                map(lambda p: self.process_group.reduce(p, self.owner_rank, async_op=True), self.parameters()),
            )
        )


class ShardSyncLayer(torch.autograd.Function):
    """
     The shard sync layer is a synchronization point between model shards.
     In the forward pass, it drops parameters in the previous shard and
     loads parameters for the next shard. In the backward pass, it does
     the reverse and also gathers gradients to the owner.
     It does not change or create any outputs at all, instead it just
     forward the input as the output.
     """

    @staticmethod
    def forward(ctx: Any, prev_shard: ModelShard, next_shard: ModelShard, *inputs: Any) -> Any:  # type: ignore
        if prev_shard:
            prev_shard.forward_drop()
        if next_shard:
            next_shard.forward_load()

        ctx.prev_shard = prev_shard
        ctx.next_shard = next_shard

        return inputs

    @staticmethod
    def backward(ctx, *grad_outputs):  # type: ignore
        ctx.next_shard.reduce_grads()
        ctx.next_shard.backward_drop()
        ctx.prev_shard.backward_load()
        return grad_outputs


# FIXME: A better option to handle grads, use custom hooks

# def _ddp_comm_hook_wrapper(comm_hook: Any, model: torch.nn.parallel.DistributedDataParallel, state: Any) -> None:
#     model._register_comm_hook(state, comm_hook)  # type: ignore


# def _reduce_hook(process_group: torch.distributed.group, bucket: dist._GradBucket) -> torch.futures.Future:  # type: ignore
#     """
#        Reduce all gradients onto rank 0. Destroy the gradients on every other rank
#     """
#     # FIXME: this is utterly broken

#     world_size = process_group.size()  # type: ignore

#     tensor = bucket.get_tensors()[0]
#     fut = dist.reduce(tensor, dst=0, group=process_group, async_op=True).get_future()  # type: ignore

#     def then_callback(fut: Any) -> Any:
#         if dist.get_rank() == 0:
#             return [fut.value()[0].div_(world_size)]
#         else:
#             return None

#     return fut.then(then_callback)


# class CustomHooks(DDPCommHookType):
#     REDUCE = partial(_ddp_comm_hook_wrapper, comm_hook=_reduce_hook)


class ShardedDataParallelExperimental(nn.Module):
    """Implements distributed data parallel training with optimizer state sharding.

    This experiments with a novel way to get to the full zero suite
    The model is sharded, and we create a process group per shard. The normal distributed data parallel
    algorithm can be used on a per-model shard basis, all the gradients being centralized on a given rank
    (which is model-shard dependent, so that the gradients redundancy can be removed). Each model shard
    can finally be updated by a standard pytorch optimizer, no OSS wrapper needed.

    Args:
        module (~torch.nn.Sequential): module to be parallelized
        optimizer (~torch.optim.Optimizer): optimizer to be used for training
        optimizer_params(Dict): extra parameters for the optimizer
        world_size (int): number of parallel workers
        process_group (optional): the c10d process group to be used for
            distributed gradient reduction. If None, the default WORLD process group
            will be used.
    """

    def __init__(
        self,
        module: nn.Sequential,  # hard pre-requisite for now, easier model slicing
        optimizer: Type[torch.optim.Optimizer],
        optimizer_params: Dict[str, Any],
        world_size: int,
        process_group: Any = None,
    ):
        super().__init__()

        self.module = module
        self.world_size = world_size
        self.process_group = process_group if process_group is not None else torch.distributed.group.WORLD
        self.rank = torch.distributed.get_rank(self.process_group)
        self.backend = torch.distributed.get_backend(group=self.process_group)  # type: ignore

        # Slice the model
        module_slices = _slice_module(module, self.world_size)

        # Create one data parallel process group per shard.
        self.shards: List[nn.Module] = []

        for i_slice, module_shard in enumerate(module_slices):
            self.shards.append(ModelShard(nn.Sequential(*module_shard), owner_rank=i_slice, pg=self.process_group))

            # Use one normal optimizer per shard
            if i_slice == self.rank:
                self.optimizer = optimizer(nn.Sequential(*module_shard).parameters(), **optimizer_params)

    def forward(self, *inputs: Any, **kwargs: Any) -> Any:
        for prev, next in zip([None, *self.shards], [*self.shards, None]):
            inputs = prev(inputs) if prev else inputs
            inputs = ShardSyncLayer.apply(prev, next, *inputs)

        return inputs
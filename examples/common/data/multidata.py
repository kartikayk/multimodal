# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import warnings
from collections import OrderedDict
from typing import List

import torch
from common.data.iteration_strategies import (
    IterationStrategy,
    IterationStrategyFactory,
    DEFAULT_ITERATION_STRATEGY_FACTORY,
)
from pytorch_lightning import LightningDataModule


class MultiDataLoader:
    # NOTE: Please check MMF's MultiDataLoader if you want to support
    # epoch based sampling funcs.
    def __init__(
        self,
        loaders: OrderedDict[str, torch.utils.data.DataLoader],
        iteration_strategy: IterationStrategy = None,
    ):
        """MultiDataLoader takes in a list of dataloaders and a sampling function
        and cycles between these dataloaders after each batch based on the index
        provided by the sampling function passed. Useful for doing multi-tasking
        over multiple datasets

        Args:
            loaders (List[torch.utils.data.DataLoader]): List of dataloaders on
                which the multitasking has to be done.

            sampling_func (Optional[Callable], optional): Function which will return
                the next index to be selected. Defaults to equally weight sampling.
        """
        if loaders is None or len(loaders) == 0:
            warnings.warn(
                "Empty loaders passed into MultiDataLoader. This can have "
                "unintended consequences."
            )

        if iteration_strategy is None:
            iteration_strategy = DEFAULT_ITERATION_STRATEGY_FACTORY(loaders)
        self.iteration_strategy = iteration_strategy
        self.loaders = loaders
        self.num_datasets = len(self.loaders)
        self.iterators = [None] * len(self.loaders)
        self.current_index = 0
        self.set_samplers()

    def set_samplers(self):
        self.samplers: List[torch.utils.data.Sampler] = []
        for loader in self.loaders.values():
            if hasattr(loader, "sampler"):
                self.samplers.append(loader.sampler)

    def __iter__(self):
        self.iterators = []

        for loader in self.loaders.values():
            self.iterators.append(iter(loader))

        return self

    def __next__(self):
        """
        Calculation of next batch is performed using following logic.

        Current chosen iterator is set in the change_dataloader function
        based on the `sampling_func` function passed to `__init__` of the
        dataloader which is called to get the index of next selected dataloader.

        If we get the next batch from iterator without any StopIteration exception,
        we return it as it is.

        Epochs don't make sense in case of using `sampling_func` unless you add
        extra logic to support epoch-based sampling functions. MMF does this in
        a different way, so take a look at IterationStrategies there to understand
        how this can be possibly done.

        Think of a case of random (equal) proportional sampling for dataset x and y
        where x is half the size of y. When x will complete its 2 epochs, y will
        have only 1 epoch completed. **So please don't use max_epochs or epoch
        based training in this case as it won't be honored**. If an iterator is
        finished, we just reignite it in this case and finished iterators
        variable isn't used. This means that this case will never reach the
        __iter__ function ever again.


        Returns:
            Dict: Contains two keys, one "batch" containing the batch from current
                selected dataloader and "datamodule_index" which is index of
                currently selected dataloader.
        """
        self.change_dataloader()
        try:
            next_batch = next(self.current_iterator)
        except StopIteration:
            iterator = iter(self.loaders[self.current_index])
            self.iterators[self.current_index] = iterator
            self.current_iterator = iterator
            next_batch = next(self.current_iterator)

        return {"batch": next_batch, "datamodule_index": self.current_index}

    def change_dataloader(self):
        choice = 0

        if self.num_datasets <= 1:
            self.current_index = choice
            self.current_iterator = self.iterators[self.current_index]
            return

        choice = [self.iteration_strategy()]
        if torch.distributed.is_available() and torch.distributed.is_initialized():
            # This broadcast is probably unnecessary with lightning if everything
            # is already properly seeded. But,to be on safe side, we can still
            # do this.
            # There are also some smarter ways to do this to avoid any broadcasting
            # by basically having a fixed generator with a fixed seed which will
            # always work deterministically.
            # TODO: Check if not doing this provides any speed benefits.
            torch.distributed.broadcast_object_list(choice, 0)

        self.current_index = choice[0]
        self.current_iterator = self.iterators[self.current_index]

    def set_epoch(self, epoch: int):
        if torch.distributed.is_available() and torch.distributed.is_initialized():
            for sampler in self.samplers:
                if sampler is not None and hasattr(sampler, "set_epoch"):
                    sampler.set_epoch(epoch)


class MultiDataModule(LightningDataModule):
    """MultiDataModule is just an abstraction over MultiDataLoader
    that will allow us to integrate it with Lightning.
    """

    # NOTE: Add rest of the functions that should be called on child datamodules
    # as required
    def __init__(
        self,
        datamodules: OrderedDict[str, LightningDataModule],
        iteration_strategy_factory: IterationStrategyFactory = DEFAULT_ITERATION_STRATEGY_FACTORY,
    ):
        super().__init__()
        self.datamodules = datamodules
        self.datamodules_list = list(datamodules.values())
        self.iteration_strategy_factory = iteration_strategy_factory
        self.current_datamodule_idx = 0

    def setup(self, stage=None):
        for datamodule in self.datamodules.values():
            datamodule.setup(stage)

    def prepare_data(self):
        for datamodule in self.datamodules.values():
            datamodule.prepare_data()

    def train_dataloader(self) -> MultiDataLoader:
        return self._build_multi_dataloader("train")

    def val_dataloader(self) -> MultiDataLoader:
        return self._build_multi_dataloader("val")

    def test_dataloader(self) -> MultiDataLoader:
        return self._build_multi_dataloader("test")

    def _build_multi_dataloader(self, split="train"):
        dataloaders = {}
        for key, datamodule in self.datamodules.items():
            dataloaders[key] = getattr(datamodule, f"{split}_dataloader")()
        return MultiDataLoader(
            dataloaders,
            self.iteration_strategy_factory(dataloaders),
        )

    def on_before_batch_transfer(self, batch, *args):
        batch, index = batch["batch"], batch["datamodule_index"]
        self.current_datamodule_idx = index
        return self.datamodules_list[
            self.current_datamodule_idx
        ].on_before_batch_transfer(batch, *args)

    def on_after_batch_transfer(self, batch, *args):
        return self.datamodules_list[
            self.current_datamodule_idx
        ].on_after_batch_transfer(batch, *args)

    def teardown(self, stage):
        for datamodule in self.datamodules.values():
            datamodule.teardown(stage)

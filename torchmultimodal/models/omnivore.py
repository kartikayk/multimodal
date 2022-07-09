# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


from typing import Callable, List, Optional

import torch
from torch import nn
from torchmultimodal.modules.encoders.swin_transformer_3d_encoder import (
    PatchEmbed3d,
    SwinTransformer3d,
)


def _imagenet1k_head(input_dim: int) -> nn.Module:
    return nn.Linear(input_dim, 1000, bias=True)


def _kinetics400_head(input_dim: int) -> nn.Module:
    return nn.Sequential(
        nn.Dropout(p=0.5),
        nn.Linear(input_dim, 400, bias=True),
    )


def _sunrgbd_head(input_dim: int) -> nn.Module:
    return nn.Linear(input_dim, 19, bias=True)


def _multimodal_head(input_dim: int) -> nn.ModuleDict:
    return nn.ModuleDict(
        {
            "image": _imagenet1k_head(input_dim),
            "rgbd": _sunrgbd_head(input_dim),
            "video": _kinetics400_head(input_dim),
        }
    )


class Omnivore(nn.Module):
    """Omnivore is a model that accept multiple vision modality.

    Omnivore (https://arxiv.org/abs/2201.08377) is a single model that able to do classification
    on images, videos, and single-view 3D data using the same shared parameters of the encoder.

    Args:   encoder (nn.Module): Instantiated encoder.
                See SwinTransformer3dEncoder class.
            heads (Optinal[nn.ModuleDict]): Dictionary of multiple heads for each dataset type

    Inputs: x (Tensor): 5 Dimensional batched video tensor with format of B C D H W
                where B is batch, C is channel, D is time, H is height, and W is width.
            input_type (str): The dataset type of the input, this will used to choose
                the correct head.
    """

    def __init__(self, encoder: nn.Module, heads: nn.ModuleDict):
        super().__init__()
        self.encoder = encoder
        self.heads = heads

    def forward(self, x: torch.Tensor, input_type: str):
        x = self.encoder(x)
        assert (
            input_type in self.heads
        ), f"Unsupported input_type: {input_type}, please use one of {list(self.heads.keys())}"
        x = self.heads[input_type](x)
        return x


class PatchEmbedOmnivore(nn.Module):
    """Patch Embedding strategy for Omnivore model
    It will use common PatchEmbed3d for image and video,
    for single view depth image it will have separate embedding for the depth channel
    and add the embedding result with the RGB channel
    reference: https://arxiv.org/abs/2201.08377

    Args:
        patch_size (Tuple[int, int, int]): Patch token size. Default: (2, 4, 4)
        embed_dim (int): Number of linear projection output channels. Default: 96
        norm_layer (nn.Module, optional): Normalization layer. Default: None
    """

    def __init__(
        self,
        patch_size: List[int],
        embed_dim: int = 96,
        norm_layer: Optional[Callable[..., nn.Module]] = None,
    ):
        super().__init__()
        self.patch_embed = PatchEmbed3d(
            patch_size=patch_size,
            embed_dim=embed_dim,
            norm_layer=norm_layer,
        )

        self.depth_patch_embed = PatchEmbed3d(
            patch_size=patch_size,
            in_channels=1,
            embed_dim=embed_dim,
            norm_layer=norm_layer,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: B C D H W
        # Note: D here represent time
        assert x.ndim == 5
        has_depth = x.shape[1] == 4

        if has_depth:
            x_rgb = self.patch_embed(x[:, :3, ...])
            x_d = self.depth_patch_embed(x[:, 3:, ...])
            x = x_rgb + x_d
        else:
            x = self.patch_embed(x)
        return x


def _omnivore_swin_t_encoder() -> SwinTransformer3d:
    encoder = SwinTransformer3d(
        patch_size=[2, 4, 4],
        embed_dim=96,
        depths=[2, 2, 6, 2],
        num_heads=[3, 6, 12, 24],
        window_size=[8, 7, 7],
        stochastic_depth_prob=0.2,
        norm_layer=nn.LayerNorm,
        patch_embed=PatchEmbedOmnivore,
        num_classes=None,
    )
    return encoder


# TODO: add pretrained weight capability
def omnivore_swin_t(encoder_only=False) -> nn.Module:
    encoder = _omnivore_swin_t_encoder()
    if encoder_only:
        return encoder

    heads = _multimodal_head(input_dim=encoder.num_features)
    return Omnivore(encoder, heads)

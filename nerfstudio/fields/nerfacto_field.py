# Copyright 2022 The Nerfstudio Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Field for compound nerf model, adds scene contraction and image embeddings to instant ngp
"""


from typing import Dict, Optional, Tuple
from enum import Enum

import numpy as np
import torch
from torch import nn
from torch.nn.parameter import Parameter
from torchtyping import TensorType

from nerfstudio.cameras.rays import RaySamples
from nerfstudio.data.scene_box import SceneBox
from nerfstudio.field_components.activations import trunc_exp
from nerfstudio.field_components.embedding import Embedding
from nerfstudio.field_components.encodings import Encoding, HashEncoding, SHEncoding
from nerfstudio.field_components.field_heads import (
    DensityFieldHead,
    FieldHead,
    FieldHeadNames,
    PredNormalsFieldHead,
    RGBFieldHead,
    SemanticFieldHead,
    TransientDensityFieldHead,
    TransientRGBFieldHead,
    UncertaintyFieldHead,
)
from nerfstudio.field_components.mlp import MLP
from nerfstudio.field_components.spatial_distortions import (
    SceneContraction,
    SpatialDistortion,
)
from nerfstudio.fields.base_field import Field

try:
    import tinycudann as tcnn
except ImportError:
    # tinycudann module doesn't exist
    pass


def get_normalized_directions(directions: TensorType["bs":..., 3]):
    """SH encoding must be in the range [0, 1]

    Args:
        directions: batch of directions
    """
    return (directions + 1.0) / 2.0

class InputWavelengthStyle(Enum):
    NONE = 0
    BEFORE_BASE = 1
    INSIDE_BASE = 2
    AFTER_BASE = 3

class TCNNNerfactoField(Field):
    """Compound Field that uses TCNN

    Args:
        aabb: parameters of scene aabb bounds
        num_images: number of images in the dataset
        num_layers: number of hidden layers
        hidden_dim: dimension of hidden layers
        geo_feat_dim: output geo feat dimensions
        num_levels: number of levels of the hashmap for the base mlp
        max_res: maximum resolution of the hashmap for the base mlp
        log2_hashmap_size: size of the hashmap for the base mlp
        num_layers_color: number of hidden layers for color network
        num_layers_transient: number of hidden layers for transient network
        hidden_dim_color: dimension of hidden layers for color network
        hidden_dim_transient: dimension of hidden layers for transient network
        appearance_embedding_dim: dimension of appearance embedding
        transient_embedding_dim: dimension of transient embedding
        use_transient_embedding: whether to use transient embedding
        use_semantics: whether to use semantic segmentation
        num_semantic_classes: number of semantic classes
        use_pred_normals: whether to use predicted normals
        use_average_appearance_embedding: whether to use average appearance embedding or zeros for inference
        spatial_distortion: spatial distortion to apply to the scene
        num_output_color_channels: Number of output channels for the network to predict for color
        num_output_density_channels: Number of output channels for the network to predict for density
        use_input_wavelength_like_position: If True, use the wavelength of the input rays as an input feature like position
    """

    def __init__(
        self,
        aabb,
        num_images: int,
        num_layers: int = 2,
        hidden_dim: int = 64,
        geo_feat_dim: int = 15,
        num_levels: int = 16,
        max_res: int = 2048,
        log2_hashmap_size: int = 19,
        num_layers_density: int = 3,
        num_layers_color: int = 3,
        num_layers_transient: int = 2,
        hidden_dim_density: int = 64,
        hidden_dim_color: int = 64,
        hidden_dim_transient: int = 64,
        appearance_embedding_dim: int = 32,
        transient_embedding_dim: int = 16,
        use_transient_embedding: bool = False,
        use_semantics: bool = False,
        num_semantic_classes: int = 100,
        use_pred_normals: bool = False,
        use_average_appearance_embedding: bool = False,
        spatial_distortion: Optional[SpatialDistortion] = None,
        num_output_color_channels: int = 3,
        num_output_density_channels: int = 1,
        wavelength_style: InputWavelengthStyle = InputWavelengthStyle.NONE,
        num_wavelength_encoding_freqs: int = 2,
    ) -> None:
        super().__init__()

        self.aabb = Parameter(aabb, requires_grad=False)
        self.geo_feat_dim = geo_feat_dim

        self.spatial_distortion = spatial_distortion
        self.num_images = num_images
        self.appearance_embedding_dim = appearance_embedding_dim
        self.embedding_appearance = Embedding(self.num_images, self.appearance_embedding_dim)
        self.use_average_appearance_embedding = use_average_appearance_embedding
        self.use_transient_embedding = use_transient_embedding
        self.use_semantics = use_semantics
        self.use_pred_normals = use_pred_normals
        self.num_output_density_channels = num_output_density_channels
        self.wavelength_style = wavelength_style

        base_res = 16
        features_per_level = 2
        growth_factor = np.exp((np.log(max_res) - np.log(base_res)) / (num_levels - 1))

        self.direction_encoding = tcnn.Encoding(
            n_input_dims=3,
            encoding_config={
                "otype": "SphericalHarmonics",
                "degree": 4,
            },
        )

        self.position_encoding = tcnn.Encoding(
            n_input_dims=3,
            encoding_config={"otype": "Frequency", "n_frequencies": 2},
        )

        self.wavelength_encoding = tcnn.Encoding(
            n_input_dims=1,
            encoding_config={"otype": "Frequency", "n_frequencies": num_wavelength_encoding_freqs},
        )


        if wavelength_style == InputWavelengthStyle.INSIDE_BASE:
            raise NotImplementedError("Wavelength style not implemented")
        in_dims = 4 if (wavelength_style == InputWavelengthStyle.BEFORE_BASE) else 3
        out_dims = self.geo_feat_dim + (0 if wavelength_style == InputWavelengthStyle.AFTER_BASE
                                        else num_output_density_channels)
        self.mlp_base = tcnn.NetworkWithInputEncoding(
            n_input_dims=in_dims,
            n_output_dims=out_dims,
            encoding_config={
                "otype": "HashGrid",
                "n_levels": num_levels,
                "n_features_per_level": features_per_level,
                "log2_hashmap_size": log2_hashmap_size,
                "base_resolution": base_res,
                "per_level_scale": growth_factor,
            },
            network_config={
                "otype": "FullyFusedMLP",
                "activation": "ReLU",
                "output_activation": "None",
                "n_neurons": hidden_dim,
                "n_hidden_layers": num_layers - 1,
            },
        )

        # transients
        if self.use_transient_embedding:
            self.transient_embedding_dim = transient_embedding_dim
            self.embedding_transient = Embedding(self.num_images, self.transient_embedding_dim)
            self.mlp_transient = tcnn.Network(
                n_input_dims=self.geo_feat_dim + self.transient_embedding_dim,
                n_output_dims=hidden_dim_transient,
                network_config={
                    "otype": "FullyFusedMLP",
                    "activation": "ReLU",
                    "output_activation": "None",
                    "n_neurons": hidden_dim_transient,
                    "n_hidden_layers": num_layers_transient - 1,
                },
            )
            self.field_head_transient_uncertainty = UncertaintyFieldHead(in_dim=self.mlp_transient.n_output_dims)
            self.field_head_transient_rgb = TransientRGBFieldHead(in_dim=self.mlp_transient.n_output_dims)
            self.field_head_transient_density = TransientDensityFieldHead(in_dim=self.mlp_transient.n_output_dims)

        # semantics
        if self.use_semantics:
            self.mlp_semantics = tcnn.Network(
                n_input_dims=self.geo_feat_dim,
                n_output_dims=hidden_dim_transient,
                network_config={
                    "otype": "FullyFusedMLP",
                    "activation": "ReLU",
                    "output_activation": "None",
                    "n_neurons": 64,
                    "n_hidden_layers": 1,
                },
            )
            self.field_head_semantics = SemanticFieldHead(
                in_dim=self.mlp_semantics.n_output_dims, num_classes=num_semantic_classes
            )

        # predicted normals
        if self.use_pred_normals:
            self.mlp_pred_normals = tcnn.Network(
                n_input_dims=self.geo_feat_dim + self.position_encoding.n_output_dims,
                n_output_dims=hidden_dim_transient,
                network_config={
                    "otype": "FullyFusedMLP",
                    "activation": "ReLU",
                    "output_activation": "None",
                    "n_neurons": 64,
                    "n_hidden_layers": 2,
                },
            )
            self.field_head_pred_normals = PredNormalsFieldHead(in_dim=self.mlp_pred_normals.n_output_dims)

        self.density_head = tcnn.Network(
            n_input_dims=self.geo_feat_dim + self.wavelength_encoding.n_output_dims,
            n_output_dims=num_output_density_channels,
            network_config={
                "otype": "FullyFusedMLP",
                "activation": "ReLU",
                "output_activation": "None",
                "n_neurons": hidden_dim_density,
                "n_hidden_layers": num_layers_density - 1,
            }
        )

        # nout = (num_output_color_channels if wavelength_style == InputWavelengthStyle.NONE else 1) + \
        #        (num_output_density_channels if wavelength_style == InputWavelengthStyle.AFTER_BASE else 0)
        nout = (num_output_color_channels if wavelength_style == InputWavelengthStyle.NONE else 1)
        self.mlp_head = tcnn.Network(
            n_input_dims=self.direction_encoding.n_output_dims + self.geo_feat_dim + self.appearance_embedding_dim + (self.wavelength_encoding.n_output_dims if wavelength_style == InputWavelengthStyle.AFTER_BASE else 0),
            n_output_dims=nout,
            network_config={
                "otype": "FullyFusedMLP",
                "activation": "ReLU",
                "output_activation": "Sigmoid",
                "n_neurons": hidden_dim_color,
                "n_hidden_layers": num_layers_color - 1,
            },
        )

    def get_density(self, ray_samples: RaySamples):
        """Computes and returns the densities."""
        if self.spatial_distortion is not None:
            positions = ray_samples.frustums.get_positions()
            positions = self.spatial_distortion(positions)
            positions = (positions + 2.0) / 4.0
        else:
            positions = SceneBox.get_normalized_positions(ray_samples.frustums.get_positions(), self.aabb)
        self._sample_locations = positions
        if not self._sample_locations.requires_grad:
            self._sample_locations.requires_grad = True
        positions_flat = positions.view(-1, 3)
        # if self.wavelength_style != InputWavelengthStyle.NONE:
        if self.wavelength_style == InputWavelengthStyle.BEFORE_BASE:
            if "wavelengths" in ray_samples.metadata:
                wavelengths = ray_samples.metadata["wavelengths"].view(-1, 1)
                positions_flat = torch.cat([positions_flat, wavelengths], dim=-1)
            elif "set_of_wavelengths" in ray_samples.metadata:
                raise NotImplementedError("idk")
                wavelengths = ray_samples.metadata["set_of_wavelengths"]
                n_wavelengths = wavelengths.shape[0]
                nrays, nimgs, _ = positions.shape
                wavelengths = torch.ones((nrays, nimgs, 1), dtype=torch.float32, device=wavelengths.device) * wavelengths.view(1, 1, -1)
                positions_flat = torch.cat([
                    positions.view(nrays, nimgs, 1, 3).repeat(1, 1, n_wavelengths, 1),
                    wavelengths.view(nrays, nimgs, n_wavelengths, 1)
                ],
                                           dim=-1).view(-1, 4)
            else:
                raise RuntimeError("Wavelengths are not provided.")
        if self.wavelength_style == InputWavelengthStyle.BEFORE_BASE and "set_of_wavelengths" in ray_samples.metadata:
            h = self.mlp_base(positions_flat)
            h = h.view(*ray_samples.frustums.shape, -1)
            # h should have shape ((# ray samples) * (# wavelengths), 1 + geo_feat_dim)
            raise Exception()
            # .view(*ray_samples.frustums.shape, -1)
            # h = h.view(n_wavelengths, *density.shape)
        else:
            h = self.mlp_base(positions_flat).view(*ray_samples.frustums.shape, -1)
        if self.wavelength_style == InputWavelengthStyle.AFTER_BASE:
            if "wavelengths" in ray_samples.metadata:
                raise Exception("not right")
                wavelength_encodings = self.wavelength_encoding(ray_samples.metadata["wavelengths"].reshape(-1, 1))
                base_mlp_out = torch.cat([h.view(-1, h.shape[-1]), wavelength_encodings], dim=-1)
                base_mlp_out = torch.sigmoid(base_mlp_out)
                density_before_activation = self.density_head(base_mlp_out)
            else:
                dataset = 'rosemary'  # TODO(gerry): This is a holdover from data trained in different ways.  Very bad.
                if not (dataset == 'tools' or dataset == 'origami'):
                    x = h[:, :, None, :].broadcast_to((-1, -1, len(ray_samples.wavelengths), -1))
                    wavelength_encodings = self.wavelength_encoding(ray_samples.wavelengths.view(-1, 1))
                    y = wavelength_encodings[None, None, :, :].broadcast_to(
                        (*x.shape[:-1], self.wavelength_encoding.n_output_dims))
                    x = torch.cat([x, y], dim=-1)
                    base_mlp_out = x.view(-1, x.shape[-1])
                    base_mlp_out = torch.sigmoid(base_mlp_out)
                    density_before_activation = self.density_head(base_mlp_out)
                else:
                    h = torch.relu(h)
                    x = h[:, :, None, :].broadcast_to((-1, -1, len(ray_samples.wavelengths), -1))
                    wavelength_encodings = self.wavelength_encoding(ray_samples.wavelengths.view(-1, 1))
                    y = wavelength_encodings[None, None, :, :].broadcast_to(
                        (*x.shape[:-1], self.wavelength_encoding.n_output_dims))
                    x = torch.cat([x, y], dim=-1)
                    base_mlp_out = x.view(-1, x.shape[-1])
                    density_before_activation = self.density_head(base_mlp_out * 1.0)
                    # density_before_activation = torch.sigmoid(density_before_activation)
                    # print(f'{torch.max(h).item():6.3f}, {torch.max(x).item():6.3f}, {torch.max(y).item():6.3f}, {torch.max(base_mlp_out).item():6.3f}, {torch.max(density_before_activation).item():6.3f}')
        else:
            density_before_activation, base_mlp_out = torch.split(
                h, [self.num_output_density_channels, self.geo_feat_dim], dim=-1)
            base_mlp_out = torch.sigmoid(base_mlp_out)
        self._density_before_activation = density_before_activation

        # Rectifying the density with an exponential is much more stable than a ReLU or
        # softplus, because it enables high post-activation (float32) density outputs
        # from smaller internal (float16) parameters.
        density = trunc_exp(density_before_activation.to(positions))
        return density, base_mlp_out

    def get_outputs(self, ray_samples: RaySamples, density_embedding: Optional[TensorType] = None):
        assert density_embedding is not None
        outputs = {}
        if ray_samples.camera_indices is None:
            raise AttributeError("Camera indices are not provided.")
        camera_indices = ray_samples.camera_indices.squeeze()
        directions = get_normalized_directions(ray_samples.frustums.directions)
        directions_flat = directions.view(-1, 3)
        d = self.direction_encoding(directions_flat)

        outputs_shape = ray_samples.frustums.directions.shape[:-1]

        # appearance
        if self.training:
            embedded_appearance = self.embedding_appearance(camera_indices)
        else:
            if self.use_average_appearance_embedding:
                embedded_appearance = torch.ones(
                    (*directions.shape[:-1], self.appearance_embedding_dim), device=directions.device
                ) * self.embedding_appearance.mean(dim=0)
            else:
                embedded_appearance = torch.zeros(
                    (*directions.shape[:-1], self.appearance_embedding_dim), device=directions.device
                )

        # transients
        if self.use_transient_embedding and self.training:
            # Note: if this errors-out, it's probably because density_embedding has got wavelength appended
            embedded_transient = self.embedding_transient(camera_indices)
            transient_input = torch.cat(
                [
                    density_embedding.view(-1, self.geo_feat_dim),
                    embedded_transient.view(-1, self.transient_embedding_dim),
                ],
                dim=-1,
            )
            x = self.mlp_transient(transient_input).view(*outputs_shape, -1).to(directions)
            outputs[FieldHeadNames.UNCERTAINTY] = self.field_head_transient_uncertainty(x)
            outputs[FieldHeadNames.TRANSIENT_RGB] = self.field_head_transient_rgb(x)
            outputs[FieldHeadNames.TRANSIENT_DENSITY] = self.field_head_transient_density(x)

        # semantics
        if self.use_semantics:
            density_embedding_copy = density_embedding.clone().detach()
            semantics_input = torch.cat(
                [
                    density_embedding_copy.view(-1, self.geo_feat_dim),
                ],
                dim=-1,
            )
            x = self.mlp_semantics(semantics_input).view(*outputs_shape, -1).to(directions)
            outputs[FieldHeadNames.SEMANTICS] = self.field_head_semantics(x)

        # predicted normals
        if self.use_pred_normals:
            positions = ray_samples.frustums.get_positions()

            positions_flat = self.position_encoding(positions.view(-1, 3))
            pred_normals_inp = torch.cat([positions_flat, density_embedding.view(-1, self.geo_feat_dim)], dim=-1)

            x = self.mlp_pred_normals(pred_normals_inp).view(*outputs_shape, -1).to(directions)
            outputs[FieldHeadNames.PRED_NORMALS] = self.field_head_pred_normals(x)

        if self.wavelength_style == InputWavelengthStyle.AFTER_BASE and ("wavelengths" not in ray_samples.metadata):
            d = d[:, None, :].expand((d.shape[0], len(ray_samples.wavelengths), d.shape[-1]))
            density_embedding = density_embedding.view(-1, len(ray_samples.wavelengths), density_embedding.shape[-1])
            embedded_appearance = embedded_appearance.view(-1, self.appearance_embedding_dim)
            embedded_appearance = embedded_appearance[:, None, :].expand((embedded_appearance.shape[0], len(ray_samples.wavelengths), embedded_appearance.shape[-1]))
            h = torch.cat([d, density_embedding, embedded_appearance], dim=-1).view(-1, self.mlp_head.n_input_dims)

        else:
            h = torch.cat(
                [
                    d,
                    density_embedding.view(-1, self.geo_feat_dim + (self.wavelength_encoding.n_output_dims if self.wavelength_style == InputWavelengthStyle.AFTER_BASE else 0)),
                    embedded_appearance.view(-1, self.appearance_embedding_dim),
                ],
                dim=-1,
            )
        rgb = self.mlp_head(h).view(*outputs_shape, -1).to(directions)
        outputs.update({FieldHeadNames.RGB: rgb})

        return outputs


class TorchNerfactoField(Field):
    """
    PyTorch implementation of the compound field.
    """

    def __init__(
        self,
        aabb,
        num_images: int,
        position_encoding: Encoding = HashEncoding(),
        direction_encoding: Encoding = SHEncoding(),
        base_mlp_num_layers: int = 3,
        base_mlp_layer_width: int = 64,
        head_mlp_num_layers: int = 2,
        head_mlp_layer_width: int = 32,
        appearance_embedding_dim: int = 40,
        skip_connections: Tuple = (4,),
        field_heads: Tuple[FieldHead] = (RGBFieldHead(),),
        spatial_distortion: SpatialDistortion = SceneContraction(),
    ) -> None:
        super().__init__()
        self.aabb = Parameter(aabb, requires_grad=False)
        self.spatial_distortion = spatial_distortion
        self.num_images = num_images
        self.appearance_embedding_dim = appearance_embedding_dim
        self.embedding_appearance = Embedding(self.num_images, self.appearance_embedding_dim)

        self.position_encoding = position_encoding
        self.direction_encoding = direction_encoding

        self.mlp_base = MLP(
            in_dim=self.position_encoding.get_out_dim(),
            num_layers=base_mlp_num_layers,
            layer_width=base_mlp_layer_width,
            skip_connections=skip_connections,
            out_activation=nn.ReLU(),
        )

        self.mlp_head = MLP(
            in_dim=self.mlp_base.get_out_dim() + self.direction_encoding.get_out_dim() + self.appearance_embedding_dim,
            num_layers=head_mlp_num_layers,
            layer_width=head_mlp_layer_width,
            out_activation=nn.ReLU(),
        )

        self.field_output_density = DensityFieldHead(in_dim=self.mlp_base.get_out_dim())
        self.field_heads = nn.ModuleList(field_heads)
        for field_head in self.field_heads:
            field_head.set_in_dim(self.mlp_head.get_out_dim())  # type: ignore

    def get_density(self, ray_samples: RaySamples):
        if self.spatial_distortion is not None:
            positions = ray_samples.frustums.get_positions()
            positions = self.spatial_distortion(positions)
        else:
            positions = ray_samples.frustums.get_positions()
        encoded_xyz = self.position_encoding(positions)
        base_mlp_out = self.mlp_base(encoded_xyz)
        density = self.field_output_density(base_mlp_out)
        return density, base_mlp_out

    def get_outputs(
        self, ray_samples: RaySamples, density_embedding: Optional[TensorType] = None
    ) -> Dict[FieldHeadNames, TensorType]:

        outputs_shape = ray_samples.frustums.directions.shape[:-1]

        if ray_samples.camera_indices is None:
            raise AttributeError("Camera indices are not provided.")
        camera_indices = ray_samples.camera_indices.squeeze()
        if self.training:
            embedded_appearance = self.embedding_appearance(camera_indices)
        else:
            embedded_appearance = torch.zeros(
                (*outputs_shape, self.appearance_embedding_dim),
                device=ray_samples.frustums.directions.device,
            )

        outputs = {}
        for field_head in self.field_heads:
            encoded_dir = self.direction_encoding(ray_samples.frustums.directions)
            mlp_out = self.mlp_head(
                torch.cat(
                    [
                        encoded_dir,
                        density_embedding,  # type:ignore
                        embedded_appearance.view(-1, self.appearance_embedding_dim),
                    ],
                    dim=-1,  # type:ignore
                )
            )
            outputs[field_head.field_head_name] = field_head(mlp_out)
        return outputs


field_implementation_to_class = {"tcnn": TCNNNerfactoField, "torch": TorchNerfactoField}

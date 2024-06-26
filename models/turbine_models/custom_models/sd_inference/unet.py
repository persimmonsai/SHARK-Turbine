# Copyright 2023 Nod Labs, Inc
#
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

import os
import sys

from iree import runtime as ireert
from iree.compiler.ir import Context
import numpy as np
from shark_turbine.aot import *
from shark_turbine.dynamo.passes import (
    DEFAULT_DECOMPOSITIONS,
)
from turbine_models.custom_models.sd_inference import utils
import torch
import torch._dynamo as dynamo
from diffusers import UNet2DConditionModel

import safetensors
import argparse
from turbine_models.turbine_tank import turbine_tank

parser = argparse.ArgumentParser()
parser.add_argument(
    "--hf_auth_token", type=str, help="The Hugging Face auth token, required"
)
parser.add_argument(
    "--hf_model_name",
    type=str,
    help="HF model name",
    default="CompVis/stable-diffusion-v1-4",
)
parser.add_argument(
    "--batch_size", type=int, default=1, help="Batch size for inference"
)
parser.add_argument(
    "--height", type=int, default=512, help="Height of Stable Diffusion"
)
parser.add_argument("--width", type=int, default=512, help="Width of Stable Diffusion")
parser.add_argument(
    "--precision", type=str, default="fp16", help="Precision of Stable Diffusion"
)
parser.add_argument(
    "--max_length", type=int, default=77, help="Sequence Length of Stable Diffusion"
)
parser.add_argument("--compile_to", type=str, help="torch, linalg, vmfb")
parser.add_argument("--external_weight_path", type=str, default="")
parser.add_argument(
    "--external_weights",
    type=str,
    default=None,
    help="saves ir/vmfb without global weights for size and readability, options [safetensors]",
)
parser.add_argument("--device", type=str, default="cpu", help="cpu, cuda, vulkan, rocm")
# TODO: Bring in detection for target triple
parser.add_argument(
    "--iree_target_triple",
    type=str,
    default="",
    help="Specify vulkan target triple or rocm/cuda target device.",
)
parser.add_argument("--vulkan_max_allocation", type=str, default="4294967296")


class UnetModel(torch.nn.Module):
    def __init__(self, hf_model_name, hf_auth_token=None):
        super().__init__()
        self.unet = UNet2DConditionModel.from_pretrained(
            hf_model_name,
            subfolder="unet",
        )

    def forward(self, sample, timestep, encoder_hidden_states, guidance_scale):
        samples = torch.cat([sample] * 2)
        unet_out = self.unet.forward(
            samples, timestep, encoder_hidden_states, return_dict=False
        )[0]
        noise_pred_uncond, noise_pred_text = unet_out.chunk(2)
        noise_pred = noise_pred_uncond + guidance_scale * (
            noise_pred_text - noise_pred_uncond
        )
        return noise_pred


def export_unet_model(
    unet_model,
    hf_model_name,
    batch_size,
    height,
    width,
    precision="fp32",
    max_length=77,
    hf_auth_token=None,
    compile_to="torch",
    external_weights=None,
    external_weight_path=None,
    device=None,
    target_triple=None,
    max_alloc=None,
    upload_ir=False,
    decomp_attn=True,
):
    mapper = {}
    decomp_list = DEFAULT_DECOMPOSITIONS
    if decomp_attn:
        decomp_list.extend(
            [
                torch.ops.aten._scaled_dot_product_flash_attention_for_cpu,
                torch.ops.aten._scaled_dot_product_flash_attention.default,
            ]
        )
    dtype = torch.float16 if precision == "fp16" else torch.float32
    unet_model = unet_model.to(dtype)
    utils.save_external_weights(
        mapper, unet_model, external_weights, external_weight_path
    )
    encoder_hidden_states_sizes = (
        unet_model.unet.config.layers_per_block,
        max_length,
        unet_model.unet.config.cross_attention_dim,
    )

    sample = (batch_size, unet_model.unet.config.in_channels, height // 8, width // 8)

    class CompiledUnet(CompiledModule):
        if external_weights:
            params = export_parameters(
                unet_model, external=True, external_scope="", name_mapper=mapper.get
            )
        else:
            params = export_parameters(unet_model)

        def main(
            self,
            sample=AbstractTensor(*sample, dtype=dtype),
            timestep=AbstractTensor(1, dtype=dtype),
            encoder_hidden_states=AbstractTensor(
                *encoder_hidden_states_sizes, dtype=dtype
            ),
            guidance_scale=AbstractTensor(1, dtype=dtype),
        ):
            return jittable(unet_model.forward, decompose_ops=decomp_list)(
                sample, timestep, encoder_hidden_states, guidance_scale
            )

    import_to = "INPUT" if compile_to == "linalg" else "IMPORT"
    inst = CompiledUnet(context=Context(), import_to=import_to)

    module_str = str(CompiledModule.get_mlir_module(inst))
    safe_name = utils.create_safe_name(hf_model_name, "-unet")
    if upload_ir:
        with open(f"{safe_name}.mlir", "w+") as f:
            f.write(module_str)
        model_name_upload = hf_model_name.replace("/", "-")
        model_name_upload += "_unet"
        blob_name = turbine_tank.uploadToBlobStorage(
            str(os.path.abspath(f"{safe_name}.mlir")),
            f"{model_name_upload}/{model_name_upload}.mlir",
        )
    if compile_to != "vmfb":
        return module_str
    else:
        utils.compile_to_vmfb(module_str, device, target_triple, max_alloc, safe_name)
        if upload_ir:
            return blob_name


if __name__ == "__main__":
    args = parser.parse_args()
    unet_model = UnetModel(
        args.hf_model_name,
        args.hf_auth_token,
    )
    mod_str = export_unet_model(
        unet_model,
        args.hf_model_name,
        args.batch_size,
        args.height,
        args.width,
        args.precision,
        args.max_length,
        args.hf_auth_token,
        args.compile_to,
        args.external_weights,
        args.external_weight_path,
        args.device,
        args.iree_target_triple,
        args.vulkan_max_allocation,
    )
    safe_name = utils.create_safe_name(args.hf_model_name, "-unet")
    with open(f"{safe_name}.mlir", "w+") as f:
        f.write(mod_str)
    print("Saved to", safe_name + ".mlir")

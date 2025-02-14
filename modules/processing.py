import hashlib
import json
import math
import os
import random
import sys
import time
from typing import Any, Dict, List

import cv2
import numpy as np
import torch
from einops import rearrange, repeat
from ldm.data.util import AddMiDaS
from ldm.models.diffusion.ddpm import LatentDepth2ImageDiffusion
from PIL import Image, ImageOps
from skimage import exposure

import modules.face_restoration
import modules.images as images
import modules.sd_hijack
import modules.sd_models as sd_models
import modules.sd_samplers_common as sd_samplers_common
import modules.sd_vae as sd_vae
import modules.shared as shared
from modules import (
    devices,
    errors,
    extra_networks,
    lowvram,
    masking,
    prompt_parser,
    rng,
    scripts,
    sd_samplers,
    sd_unet,
    sd_vae_approx,
)
from modules.rng import slerp  # noqa: F401
from modules.sd_hijack import model_hijack
from modules.shared import cmd_opts, opts, state

# some of those options should not be changed at all because they would break the model, so I removed them from options.
opt_C = 4
opt_f = 8


def setup_color_correction(image):
    print("Calibrating color correction.")
    correction_target = cv2.cvtColor(np.asarray(image.copy()), cv2.COLOR_RGB2LAB)
    return correction_target


def apply_color_correction(correction, original_image):
    from blendmodes.blend import BlendType, blendLayers

    print("Applying color correction.")
    image = Image.fromarray(
        cv2.cvtColor(
            exposure.match_histograms(
                cv2.cvtColor(np.asarray(original_image), cv2.COLOR_RGB2LAB),
                correction,
                channel_axis=2,
            ),
            cv2.COLOR_LAB2RGB,
        ).astype("uint8")
    )

    image = blendLayers(image, original_image, BlendType.LUMINOSITY)

    return image.convert("RGB")


def apply_overlay(image, paste_loc, index, overlays):
    if overlays is None or index >= len(overlays):
        return image

    overlay = overlays[index]

    if paste_loc is not None:
        x, y, w, h = paste_loc
        base_image = Image.new("RGBA", (overlay.width, overlay.height))
        image = images.resize_image(1, image, w, h)
        base_image.paste(image, (x, y))
        image = base_image

    image = image.convert("RGBA")
    image.alpha_composite(overlay)
    image = image.convert("RGB")

    return image


def create_binary_mask(image):
    if image.mode == "RGBA" and image.getextrema()[-1] != (255, 255):
        image = image.split()[-1].convert("L").point(lambda x: 255 if x > 128 else 0)
    else:
        image = image.convert("L")
    return image


def txt2img_image_conditioning(sd_model, x, width, height):
    if sd_model.model.conditioning_key in {"hybrid", "concat"}:  # Inpainting models
        # The "masked-image" in this case will just be all zeros since the entire image is masked.
        image_conditioning = torch.zeros(x.shape[0], 3, height, width, device=x.device)
        image_conditioning = sd_model.get_first_stage_encoding(
            sd_model.encode_first_stage(image_conditioning)
        )

        # Add the fake full 1s mask to the first dimension.
        image_conditioning = torch.nn.functional.pad(
            image_conditioning, (0, 0, 0, 0, 1, 0), value=1.0
        )
        image_conditioning = image_conditioning.to(x.dtype)

        return image_conditioning

    elif sd_model.model.conditioning_key == "crossattn-adm":  # UnCLIP models
        return x.new_zeros(
            x.shape[0],
            2 * sd_model.noise_augmentor.time_embed.dim,
            dtype=x.dtype,
            device=x.device,
        )

    else:
        # Dummy zero conditioning if we're not using inpainting or unclip models.
        # Still takes up a bit of memory, but no encoder call.
        # Pretty sure we can just make this a 1x1 image since its not going to be used besides its batch size.
        return x.new_zeros(x.shape[0], 5, 1, 1, dtype=x.dtype, device=x.device)


class StableDiffusionProcessing:
    """
    The first set of paramaters: sd_models -> do_not_reload_embeddings represent the minimum required to create a StableDiffusionProcessing
    """

    cached_uc = [None, None]
    cached_c = [None, None]

    def __init__(
        self,
        sd_model=None,
        outpath_samples=None,
        outpath_grids=None,
        prompt: str = "",
        styles: List[str] = None,
        seed: int = -1,
        subseed: int = -1,
        subseed_strength: float = 0,
        seed_resize_from_h: int = -1,
        seed_resize_from_w: int = -1,
        seed_enable_extras: bool = True,
        sampler_name: str = None,
        batch_size: int = 1,
        n_iter: int = 1,
        steps: int = 50,
        cfg_scale: float = 7.0,
        width: int = 512,
        height: int = 512,
        restore_faces: bool = False,
        tiling: bool = False,
        do_not_save_samples: bool = False,
        do_not_save_grid: bool = False,
        extra_generation_params: Dict[Any, Any] = None,
        negative_prompt: str = None,
        eta: float = None,
        do_not_reload_embeddings: bool = False,
        denoising_strength: float = 0,
        ddim_discretize: str = None,
        s_min_uncond: float = 0.0,
        s_churn: float = 0.0,
        s_tmax: float = None,
        s_tmin: float = 0.0,
        s_noise: float = 1.0,
        rng: rng.ImageRNG = None,
        override_settings: Dict[str, Any] = None,
        override_settings_restore_afterwards: bool = True,
        sampler_index: int = None,
        script_args: list = None,
    ):
        if sampler_index is not None:
            print(
                "sampler_index argument for StableDiffusionProcessing does not do anything; use sampler_name",
                file=sys.stderr,
            )

        self.outpath_samples: str = outpath_samples
        self.outpath_grids: str = outpath_grids
        self.prompt: str = prompt
        self.prompt_for_display: str = None
        self.negative_prompt: str = negative_prompt or ""
        self.styles: list = styles or []
        self.seed: int = seed
        self.subseed: int = subseed
        self.subseed_strength: float = subseed_strength
        self.seed_resize_from_h: int = seed_resize_from_h
        self.seed_resize_from_w: int = seed_resize_from_w
        self.sampler_name: str = sampler_name
        self.batch_size: int = batch_size
        self.n_iter: int = n_iter
        self.steps: int = steps
        self.cfg_scale: float = cfg_scale
        self.width: int = width
        self.height: int = height
        self.restore_faces: bool = restore_faces
        self.tiling: bool = tiling
        self.do_not_save_samples: bool = do_not_save_samples
        self.do_not_save_grid: bool = do_not_save_grid
        self.extra_generation_params: dict = extra_generation_params or {}
        self.eta = eta
        self.do_not_reload_embeddings = do_not_reload_embeddings
        self.paste_to = None
        self.color_corrections = None
        self.denoising_strength: float = denoising_strength
        self.sampler_noise_scheduler_override = None
        self.ddim_discretize = ddim_discretize or opts.ddim_discretize
        self.s_min_uncond = s_min_uncond or opts.s_min_uncond
        self.s_churn = s_churn or opts.s_churn
        self.s_tmin = s_tmin or opts.s_tmin
        self.s_tmax = s_tmax or float(
            "inf"
        )  # not representable as a standard ui option
        self.s_noise = s_noise or opts.s_noise
        self.override_settings = {
            k: v
            for k, v in (override_settings or {}).items()
            if k not in shared.restricted_opts
        }
        self.override_settings_restore_afterwards = override_settings_restore_afterwards
        self.is_using_inpainting_conditioning = False
        self.disable_extra_networks = False
        self.token_merging_ratio = 0
        self.token_merging_ratio_hr = 0

        if not seed_enable_extras:
            self.subseed = -1
            self.subseed_strength = 0
            self.seed_resize_from_h = 0
            self.seed_resize_from_w = 0

        self.scripts = None
        self.script_args = script_args
        self.all_prompts = None
        self.all_negative_prompts = None
        self.all_seeds = None
        self.all_subseeds = None
        self.iteration = 0
        self.is_hr_pass = False
        self.sampler = None

        self.prompts = None
        self.negative_prompts = None
        self.extra_network_data = None
        self.seeds = None
        self.subseeds = None

        self.step_multiplier = 1
        self.cached_uc = StableDiffusionProcessing.cached_uc
        self.cached_c = StableDiffusionProcessing.cached_c
        self.uc = None
        self.c = None
        self.rng = rng

        self.user = None

        self.sd_model_hash = self.sd_model.sd_model_hash
        self.sd_model_name = self.sd_model.sd_checkpoint_info.name_for_extra
        self.sd_vae_name = sd_vae.get_loaded_vae_name()
        self.sd_vae_hash = sd_vae.get_loaded_vae_hash()

    @property
    def sd_model(self):
        return shared.sd_model

    def txt2img_image_conditioning(self, x, width=None, height=None):
        self.is_using_inpainting_conditioning = (
            self.sd_model.model.conditioning_key in {"hybrid", "concat"}
        )

        ret = txt2img_image_conditioning(
            self.sd_model, x, width or self.width, height or self.height
        )
        return ret

    def depth2img_image_conditioning(self, source_image):
        # Use the AddMiDaS helper to Format our source image to suit the MiDaS model
        transformer = AddMiDaS(model_type="dpt_hybrid")
        transformed = transformer({"jpg": rearrange(source_image[0], "c h w -> h w c")})
        midas_in = torch.from_numpy(transformed["midas_in"][None, ...]).to(
            device=shared.device
        )
        midas_in = repeat(midas_in, "1 ... -> n ...", n=self.batch_size)

        conditioning_image = self.sd_model.get_first_stage_encoding(
            self.sd_model.encode_first_stage(source_image)
        )
        conditioning = torch.nn.functional.interpolate(
            self.sd_model.depth_model(midas_in),
            size=conditioning_image.shape[2:],
            mode="bicubic",
            align_corners=False,
        )

        (depth_min, depth_max) = torch.aminmax(conditioning)
        conditioning = 2.0 * (conditioning - depth_min) / (depth_max - depth_min) - 1.0
        return conditioning

    def edit_image_conditioning(self, source_image):
        conditioning_image = self.sd_model.encode_first_stage(source_image).mode()

        return conditioning_image

    def unclip_image_conditioning(self, source_image):
        c_adm = self.sd_model.embedder(source_image)
        if self.sd_model.noise_augmentor is not None:
            noise_level = 0  # TODO: Allow other noise levels?
            c_adm, noise_level_emb = self.sd_model.noise_augmentor(
                c_adm,
                noise_level=repeat(
                    torch.tensor([noise_level]).to(c_adm.device),
                    "1 -> b",
                    b=c_adm.shape[0],
                ),
            )
            c_adm = torch.cat((c_adm, noise_level_emb), 1)
        return c_adm

    def inpainting_image_conditioning(
        self, source_image, latent_image, image_mask=None
    ):
        self.is_using_inpainting_conditioning = True

        # Handle the different mask inputs
        if image_mask is not None:
            if torch.is_tensor(image_mask):
                conditioning_mask = image_mask
            else:
                conditioning_mask = np.array(image_mask.convert("L"))
                conditioning_mask = conditioning_mask.astype(np.float32) / 255.0
                conditioning_mask = torch.from_numpy(conditioning_mask[None, None])

                # Inpainting model uses a discretized mask as input, so we round to either 1.0 or 0.0
                conditioning_mask = torch.round(conditioning_mask)
        else:
            conditioning_mask = source_image.new_ones(1, 1, *source_image.shape[-2:])

        # Create another latent image, this time with a masked version of the original input.
        # Smoothly interpolate between the masked and unmasked latent conditioning image using a parameter.
        conditioning_mask = conditioning_mask.to(
            device=source_image.device, dtype=source_image.dtype
        )
        conditioning_image = torch.lerp(
            source_image,
            source_image * (1.0 - conditioning_mask),
            getattr(self, "inpainting_mask_weight", shared.opts.inpainting_mask_weight),
        )

        # Encode the new masked image using first stage of network.
        conditioning_image = self.sd_model.get_first_stage_encoding(
            self.sd_model.encode_first_stage(conditioning_image)
        )

        # Create the concatenated conditioning tensor to be fed to `c_concat`
        conditioning_mask = torch.nn.functional.interpolate(
            conditioning_mask, size=latent_image.shape[-2:]
        )
        conditioning_mask = conditioning_mask.expand(
            conditioning_image.shape[0], -1, -1, -1
        )
        image_conditioning = torch.cat([conditioning_mask, conditioning_image], dim=1)
        image_conditioning = image_conditioning.to(shared.device).type(
            self.sd_model.dtype
        )

        return image_conditioning

    def img2img_image_conditioning(self, source_image, latent_image, image_mask=None):
        source_image = devices.cond_cast_float(source_image)

        # HACK: Using introspection as the Depth2Image model doesn't appear to uniquely
        # identify itself with a field common to all models. The conditioning_key is also hybrid.
        if isinstance(self.sd_model, LatentDepth2ImageDiffusion):
            return self.depth2img_image_conditioning(source_image)

        if self.sd_model.cond_stage_key == "edit":
            return self.edit_image_conditioning(source_image)

        if self.sampler.conditioning_key in {"hybrid", "concat"}:
            return self.inpainting_image_conditioning(
                source_image, latent_image, image_mask=image_mask
            )

        if self.sampler.conditioning_key == "crossattn-adm":
            return self.unclip_image_conditioning(source_image)

        # Dummy zero conditioning if we're not using inpainting or depth model.
        return latent_image.new_zeros(latent_image.shape[0], 5, 1, 1)

    def init(self, all_prompts, all_seeds, all_subseeds):
        pass

    def sample(
        self,
        conditioning,
        unconditional_conditioning,
        seeds,
        subseeds,
        subseed_strength,
        prompts,
    ):
        raise NotImplementedError()

    def close(self):
        self.sampler = None
        self.c = None
        self.uc = None
        if not opts.experimental_persistent_cond_cache:
            StableDiffusionProcessing.cached_c = [None, None]
            StableDiffusionProcessing.cached_uc = [None, None]

    def get_token_merging_ratio(self, for_hr=False):
        if for_hr:
            return (
                self.token_merging_ratio_hr
                or opts.token_merging_ratio_hr
                or self.token_merging_ratio
                or opts.token_merging_ratio
            )

        return self.token_merging_ratio or opts.token_merging_ratio

    def setup_prompts(self):
        if type(self.prompt) == list:
            self.all_prompts = self.prompt
        else:
            self.all_prompts = self.batch_size * self.n_iter * [self.prompt]

        if type(self.negative_prompt) == list:
            self.all_negative_prompts = self.negative_prompt
        else:
            self.all_negative_prompts = (
                self.batch_size * self.n_iter * [self.negative_prompt]
            )

        self.all_prompts = [
            shared.prompt_styles.apply_styles_to_prompt(x, self.styles)
            for x in self.all_prompts
        ]
        self.all_negative_prompts = [
            shared.prompt_styles.apply_negative_styles_to_prompt(x, self.styles)
            for x in self.all_negative_prompts
        ]

        self.main_prompt = self.all_prompts[0]
        self.main_negative_prompt = self.all_negative_prompts[0]

    def get_conds_with_caching(
        self, function, required_prompts, steps, caches, extra_network_data
    ):
        """
        Returns the result of calling function(shared.sd_model, required_prompts, steps)
        using a cache to store the result if the same arguments have been used before.

        cache is an array containing two elements. The first element is a tuple
        representing the previously used arguments, or None if no arguments
        have been used before. The second element is where the previously
        computed result is stored.

        caches is a list with items described above.
        """

        cached_params = (
            required_prompts,
            steps,
            opts.CLIP_stop_at_last_layers,
            shared.sd_model.sd_checkpoint_info,
            extra_network_data,
            opts.sdxl_crop_left,
            opts.sdxl_crop_top,
            self.width,
            self.height,
        )

        for cache in caches:
            if cache[0] is not None and cached_params == cache[0]:
                return cache[1]

        cache = caches[0]

        with devices.autocast():
            cache[1] = function(shared.sd_model, required_prompts, steps)

        cache[0] = cached_params
        return cache[1]

    def setup_conds(self):
        prompts = prompt_parser.SdConditioning(
            self.prompts, width=self.width, height=self.height
        )
        negative_prompts = prompt_parser.SdConditioning(
            self.negative_prompts,
            width=self.width,
            height=self.height,
            is_negative_prompt=True,
        )

        sampler_config = sd_samplers.find_sampler_config(self.sampler_name)
        self.step_multiplier = (
            2
            if sampler_config and sampler_config.options.get("second_order", False)
            else 1
        )
        self.uc = self.get_conds_with_caching(
            prompt_parser.get_learned_conditioning,
            negative_prompts,
            self.steps * self.step_multiplier,
            [self.cached_uc],
            self.extra_network_data,
        )
        self.c = self.get_conds_with_caching(
            prompt_parser.get_multicond_learned_conditioning,
            prompts,
            self.steps * self.step_multiplier,
            [self.cached_c],
            self.extra_network_data,
        )

    def parse_extra_network_prompts(self):
        self.prompts, self.extra_network_data = extra_networks.parse_prompts(
            self.prompts
        )


class Processed:
    def __init__(
        self,
        p: StableDiffusionProcessing,
        images_list,
        seed=-1,
        info="",
        subseed=None,
        all_prompts=None,
        all_negative_prompts=None,
        all_seeds=None,
        all_subseeds=None,
        index_of_first_image=0,
        infotexts=None,
        comments="",
    ):
        self.images = images_list
        self.prompt = p.prompt
        self.negative_prompt = p.negative_prompt
        self.seed = seed
        self.subseed = subseed
        self.subseed_strength = p.subseed_strength
        self.info = info
        self.comments = comments
        self.width = p.width
        self.height = p.height
        self.sampler_name = p.sampler_name
        self.cfg_scale = p.cfg_scale
        self.image_cfg_scale = getattr(p, "image_cfg_scale", None)
        self.steps = p.steps
        self.batch_size = p.batch_size
        self.restore_faces = p.restore_faces
        self.face_restoration_model = (
            opts.face_restoration_model if p.restore_faces else None
        )
        self.sd_model_hash = shared.sd_model.sd_model_hash
        self.seed_resize_from_w = p.seed_resize_from_w
        self.seed_resize_from_h = p.seed_resize_from_h
        self.denoising_strength = getattr(p, "denoising_strength", None)
        self.extra_generation_params = p.extra_generation_params
        self.index_of_first_image = index_of_first_image
        self.styles = p.styles
        self.job_timestamp = state.job_timestamp
        self.clip_skip = opts.CLIP_stop_at_last_layers
        self.token_merging_ratio = p.token_merging_ratio
        self.token_merging_ratio_hr = p.token_merging_ratio_hr

        self.eta = p.eta
        self.ddim_discretize = p.ddim_discretize
        self.s_churn = p.s_churn
        self.s_tmin = p.s_tmin
        self.s_tmax = p.s_tmax
        self.s_noise = p.s_noise
        self.s_min_uncond = p.s_min_uncond
        self.sampler_noise_scheduler_override = p.sampler_noise_scheduler_override
        self.prompt = self.prompt if type(self.prompt) != list else self.prompt[0]
        self.negative_prompt = (
            self.negative_prompt
            if type(self.negative_prompt) != list
            else self.negative_prompt[0]
        )
        self.seed = (
            int(self.seed if type(self.seed) != list else self.seed[0])
            if self.seed is not None
            else -1
        )
        self.subseed = (
            int(self.subseed if type(self.subseed) != list else self.subseed[0])
            if self.subseed is not None
            else -1
        )
        self.is_using_inpainting_conditioning = p.is_using_inpainting_conditioning

        self.all_prompts = all_prompts or p.all_prompts or [self.prompt]
        self.all_negative_prompts = (
            all_negative_prompts or p.all_negative_prompts or [self.negative_prompt]
        )
        self.all_seeds = all_seeds or p.all_seeds or [self.seed]
        self.all_subseeds = all_subseeds or p.all_subseeds or [self.subseed]
        self.infotexts = infotexts or [info]

    def js(self):
        obj = {
            "prompt": self.all_prompts[0],
            "all_prompts": self.all_prompts,
            "negative_prompt": self.all_negative_prompts[0],
            "all_negative_prompts": self.all_negative_prompts,
            "seed": self.seed,
            "all_seeds": self.all_seeds,
            "subseed": self.subseed,
            "all_subseeds": self.all_subseeds,
            "subseed_strength": self.subseed_strength,
            "width": self.width,
            "height": self.height,
            "sampler_name": self.sampler_name,
            "cfg_scale": self.cfg_scale,
            "steps": self.steps,
            "batch_size": self.batch_size,
            "restore_faces": self.restore_faces,
            "face_restoration_model": self.face_restoration_model,
            "sd_model_hash": self.sd_model_hash,
            "seed_resize_from_w": self.seed_resize_from_w,
            "seed_resize_from_h": self.seed_resize_from_h,
            "denoising_strength": self.denoising_strength,
            "extra_generation_params": self.extra_generation_params,
            "index_of_first_image": self.index_of_first_image,
            "infotexts": self.infotexts,
            "styles": self.styles,
            "job_timestamp": self.job_timestamp,
            "clip_skip": self.clip_skip,
            "is_using_inpainting_conditioning": self.is_using_inpainting_conditioning,
        }

        return json.dumps(obj)

    def get_token_merging_ratio(self, for_hr=False):
        return self.token_merging_ratio_hr if for_hr else self.token_merging_ratio


# from https://discuss.pytorch.org/t/help-regarding-slerp-function-for-generative-model-sampling/32475/3
def slerp(val, low, high):
    low_norm = low / torch.norm(low, dim=1, keepdim=True)
    high_norm = high / torch.norm(high, dim=1, keepdim=True)
    dot = (low_norm * high_norm).sum(1)

    if dot.mean() > 0.9995:
        return low * val + high * (1 - val)

    omega = torch.acos(dot)
    so = torch.sin(omega)
    res = (torch.sin((1.0 - val) * omega) / so).unsqueeze(1) * low + (
        torch.sin(val * omega) / so
    ).unsqueeze(1) * high
    return res


def create_random_tensors(
    shape,
    seeds,
    subseeds=None,
    subseed_strength=0.0,
    seed_resize_from_h=0,
    seed_resize_from_w=0,
    p=None,
):
    g = rng.ImageRNG(
        shape,
        seeds,
        subseeds=subseeds,
        subseed_strength=subseed_strength,
        seed_resize_from_h=seed_resize_from_h,
        seed_resize_from_w=seed_resize_from_w,
    )
    return g.next()


def decode_latent_batch(model, batch, target_device=None, check_for_nans=False):
    samples = []

    for i in range(batch.shape[0]):
        sample = decode_first_stage(model, batch[i : i + 1])[0]

        if check_for_nans:
            try:
                devices.test_for_nans(sample, "vae")
            except devices.NansException as e:
                if (
                    devices.dtype_vae == torch.float32
                    or not shared.opts.auto_vae_precision
                ):
                    raise e

                errors.print_error_explanation(
                    "A tensor with all NaNs was produced in VAE.\n"
                    "Converted VAE into 32-bit float and retry.\n"
                )

                devices.dtype_vae = torch.float32
                model.first_stage_model.to(devices.dtype_vae)
                batch = batch.to(devices.dtype_vae)

                sample = decode_first_stage(model, batch[i : i + 1])[0]

        if target_device is not None:
            sample = sample.to(target_device)

        samples.append(sample)

    return samples


def decode_first_stage(model, x):
    x = model.decode_first_stage(x.to(model.device).to(devices.dtype_vae))

    return x


def get_fixed_seed(seed):
    if seed is None or seed == "" or seed == -1:
        return int(random.randrange(4294967294))

    return seed


def fix_seed(p):
    p.seed = get_fixed_seed(p.seed)
    p.subseed = get_fixed_seed(p.subseed)


def program_version():
    return "v1.6.0"


def create_infotext(
    p,
    all_prompts,
    all_seeds,
    all_subseeds,
    comments=None,
    iteration=0,
    position_in_batch=0,
    use_main_prompt=False,
    index=None,
    all_negative_prompts=None,
):
    if index is None:
        index = position_in_batch + iteration * p.batch_size

    if all_negative_prompts is None:
        all_negative_prompts = p.all_negative_prompts

    clip_skip = getattr(p, "clip_skip", opts.CLIP_stop_at_last_layers)
    enable_hr = getattr(p, "enable_hr", False)
    token_merging_ratio = p.get_token_merging_ratio()
    token_merging_ratio_hr = p.get_token_merging_ratio(for_hr=True)

    uses_ensd = opts.eta_noise_seed_delta != 0
    if uses_ensd:
        uses_ensd = sd_samplers_common.is_sampler_using_eta_noise_seed_delta(p)

    generation_params = {
        "Steps": p.steps,
        "Sampler": p.sampler_name,
        "CFG scale": p.cfg_scale,
        "Image CFG scale": getattr(p, "image_cfg_scale", None),
        "Seed": p.all_seeds[0] if use_main_prompt else all_seeds[index],
        "Face restoration": opts.face_restoration_model if p.restore_faces else None,
        "Size": f"{p.width}x{p.height}",
        "Model hash": p.sd_model_hash if opts.add_model_hash_to_info else None,
        "Model": p.sd_model_name if opts.add_model_name_to_info else None,
        "VAE hash": p.sd_vae_hash if opts.add_model_hash_to_info else None,
        "VAE": p.sd_vae_name if opts.add_model_name_to_info else None,
        "Variation seed": (
            None
            if p.subseed_strength == 0
            else (p.all_subseeds[0] if use_main_prompt else all_subseeds[index])
        ),
        "Variation seed strength": (
            None if p.subseed_strength == 0 else p.subseed_strength
        ),
        "Seed resize from": (
            None
            if p.seed_resize_from_w <= 0 or p.seed_resize_from_h <= 0
            else f"{p.seed_resize_from_w}x{p.seed_resize_from_h}"
        ),
        "Denoising strength": getattr(p, "denoising_strength", None),
        "Conditional mask weight": getattr(
            p, "inpainting_mask_weight", shared.opts.inpainting_mask_weight
        )
        if p.is_using_inpainting_conditioning
        else None,
        "Clip skip": None if clip_skip <= 1 else clip_skip,
        "ENSD": opts.eta_noise_seed_delta if uses_ensd else None,
        "Token merging ratio": None
        if token_merging_ratio == 0
        else token_merging_ratio,
        "Token merging ratio hr": None
        if not enable_hr or token_merging_ratio_hr == 0
        else token_merging_ratio_hr,
        "Init image hash": getattr(p, "init_img_hash", None),
        "RNG": opts.randn_source if opts.randn_source != "GPU" else None,
        "NGMS": None if p.s_min_uncond == 0 else p.s_min_uncond,
        "Tiling": "True" if p.tiling else None,
        **p.extra_generation_params,
        "Version": program_version() if opts.add_version_to_infotext else None,
        "User": p.user if opts.add_user_name_to_info else None,
    }

    def quote(text):
        if "," not in str(text) and "\n" not in str(text) and ":" not in str(text):
            return text

        return json.dumps(text, ensure_ascii=False)

    generation_params_text = ", ".join(
        [
            k if k == v else f"{k}: {quote(v)}"
            for k, v in generation_params.items()
            if v is not None
        ]
    )

    # prompt_text = p.main_prompt if use_main_prompt else all_prompts[index]
    prompt_text = p.main_prompt
    negative_prompt_text = (
        # f"\nNegative prompt: {p.main_negative_prompt if use_main_prompt else all_negative_prompts[index]}"
        f"\nNegative prompt: {p.main_negative_prompt}"
        if all_negative_prompts[index]
        else ""
    )

    infotext = f"{prompt_text}{negative_prompt_text}\n{generation_params_text}".strip()
    return infotext


def process_images(p: StableDiffusionProcessing) -> Processed:
    if p.scripts is not None:
        p.scripts.before_process(p)

    res = process_images_inner(p)

    # stored_opts = {k: opts.data[k] for k in p.override_settings.keys()}

    # try:
    #     # if no checkpoint override or the override checkpoint can't be found, remove override entry and load opts checkpoint
    #     if (
    #         sd_models.checkpoint_aliases.get(
    #             p.override_settings.get("sd_model_checkpoint")
    #         )
    #         is None
    #     ):
    #         p.override_settings.pop("sd_model_checkpoint", None)
    #         sd_models.reload_model_weights()

    #     for k, v in p.override_settings.items():
    #         setattr(opts, k, v)

    #         if k == "sd_model_checkpoint":
    #             sd_models.reload_model_weights()

    #         if k == "sd_vae":
    #             sd_vae.reload_vae_weights()

    #     sd_models.apply_token_merging(p.sd_model, p.get_token_merging_ratio())

    #     res = process_images_inner(p)

    # finally:
    #     pass
    #     sd_models.apply_token_merging(p.sd_model, 0)

    #     # restore opts to original state
    #     if p.override_settings_restore_afterwards:
    #         for k, v in stored_opts.items():
    #             setattr(opts, k, v)

    #             if k == "sd_vae":
    #                 sd_vae.reload_vae_weights()

    return res


def process_images_inner(p: StableDiffusionProcessing) -> Processed:
    """this is the main loop that both txt2img and img2img use; it calls func_init once inside all the scopes and func_sample once per batch"""

    if type(p.prompt) == list:
        assert len(p.prompt) > 0
    else:
        assert p.prompt is not None

    devices.torch_gc()

    seed = get_fixed_seed(p.seed)
    subseed = get_fixed_seed(p.subseed)

    modules.sd_hijack.model_hijack.apply_circular(p.tiling)
    modules.sd_hijack.model_hijack.clear_comments()

    comments = {}

    p.setup_prompts()

    if type(seed) == list:
        p.all_seeds = seed
    else:
        p.all_seeds = [
            int(seed) + (x if p.subseed_strength == 0 else 0)
            for x in range(len(p.all_prompts))
        ]

    if type(subseed) == list:
        p.all_subseeds = subseed
    else:
        p.all_subseeds = [int(subseed) + x for x in range(len(p.all_prompts))]

    if os.path.exists(cmd_opts.embeddings_dir) and not p.do_not_reload_embeddings:
        model_hijack.embedding_db.load_textual_inversion_embeddings()

    if p.scripts is not None:
        p.scripts.process(p)

    infotexts = []
    output_images = []

    with torch.no_grad(), p.sd_model.ema_scope():
        with devices.autocast():
            p.init(p.all_prompts, p.all_seeds, p.all_subseeds)

            # for OSX, loading the model during sampling changes the generated picture, so it is loaded here
            if (
                shared.opts.live_previews_enable
                and opts.show_progress_type == "Approx NN"
            ):
                sd_vae_approx.model()

            sd_unet.apply_unet()

        if state.job_count == -1:
            state.job_count = p.n_iter

        for n in range(p.n_iter):
            p.iteration = n

            if state.skipped:
                state.skipped = False

            if state.interrupted:
                break

            p.prompts = p.all_prompts[n * p.batch_size : (n + 1) * p.batch_size]
            p.negative_prompts = p.all_negative_prompts[
                n * p.batch_size : (n + 1) * p.batch_size
            ]
            p.seeds = p.all_seeds[n * p.batch_size : (n + 1) * p.batch_size]
            p.subseeds = p.all_subseeds[n * p.batch_size : (n + 1) * p.batch_size]

            p.rng = rng.ImageRNG(
                (opt_C, p.height // opt_f, p.width // opt_f),
                p.seeds,
                subseeds=p.subseeds,
                subseed_strength=p.subseed_strength,
                seed_resize_from_h=p.seed_resize_from_h,
                seed_resize_from_w=p.seed_resize_from_w,
            )

            if p.scripts is not None:
                p.scripts.before_process_batch(
                    p,
                    batch_number=n,
                    prompts=p.prompts,
                    seeds=p.seeds,
                    subseeds=p.subseeds,
                )

            def infotext(index=0, use_main_prompt=False):
                return create_infotext(
                    p,
                    p.prompts,
                    p.seeds,
                    p.subseeds,
                    use_main_prompt=use_main_prompt,
                    index=index,
                    all_negative_prompts=p.negative_prompts,
                )

            if len(p.prompts) == 0:
                break

            p.parse_extra_network_prompts()

            if not p.disable_extra_networks:
                with devices.autocast():
                    extra_networks.activate(p, p.extra_network_data)

            if p.scripts is not None:
                p.scripts.process_batch(
                    p,
                    batch_number=n,
                    prompts=p.prompts,
                    seeds=p.seeds,
                    subseeds=p.subseeds,
                )

            p.setup_conds()

            for comment in model_hijack.comments:
                comments[comment] = 1

            p.extra_generation_params.update(model_hijack.extra_generation_params)

            if p.n_iter > 1:
                shared.state.job = f"Batch {n+1} out of {p.n_iter}"

            with devices.without_autocast() if devices.unet_needs_upcast else devices.autocast():
                samples_ddim = p.sample(
                    conditioning=p.c,
                    unconditional_conditioning=p.uc,
                    seeds=p.seeds,
                    subseeds=p.subseeds,
                    subseed_strength=p.subseed_strength,
                    prompts=p.prompts,
                )
            # x_samples_ddim = decode_latent_batch(
            #     p.sd_model, samples_ddim, target_device=devices.cpu, check_for_nans=True
            # )
            # x_samples_ddim = torch.stack(x_samples_ddim).float()
            # x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
            decode_start_time = time.monotonic()
            print(f"Decoding latents in {samples_ddim.device}...")
            x_samples_ddim = decode_latent_batch(
                p.sd_model,
                samples_ddim,
                # target_device=devices.device,
                check_for_nans=True,
            )
            x_samples_ddim = torch.stack(x_samples_ddim).float()
            x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
            print(f"done in {round(time.monotonic() - decode_start_time, 2)}s")

            move_to_cpu_start_time = time.monotonic()
            print("Move latents to cpu...")
            x_samples_ddim = x_samples_ddim.to(devices.cpu)
            print(f"done in {round(time.monotonic() - move_to_cpu_start_time, 2)}s")
            del samples_ddim

            if lowvram.is_enabled(shared.sd_model):
                lowvram.send_everything_to_cpu()

            devices.torch_gc()

            if p.scripts is not None:
                p.scripts.postprocess_batch(p, x_samples_ddim, batch_number=n)

                p.prompts = p.all_prompts[n * p.batch_size : (n + 1) * p.batch_size]
                p.negative_prompts = p.all_negative_prompts[
                    n * p.batch_size : (n + 1) * p.batch_size
                ]

                batch_params = scripts.PostprocessBatchListArgs(list(x_samples_ddim))
                p.scripts.postprocess_batch_list(p, batch_params, batch_number=n)
                x_samples_ddim = batch_params.images

            for i, x_sample in enumerate(x_samples_ddim):
                p.batch_index = i

                x_sample = 255.0 * np.moveaxis(x_sample.cpu().numpy(), 0, 2)
                x_sample = x_sample.astype(np.uint8)

                if p.restore_faces:
                    if (
                        opts.save
                        and not p.do_not_save_samples
                        and opts.save_images_before_face_restoration
                    ):
                        images.save_image(
                            Image.fromarray(x_sample),
                            p.outpath_samples,
                            "",
                            p.seeds[i],
                            p.prompts[i],
                            opts.samples_format,
                            info="",
                            p=p,
                            suffix="-before-face-restoration",
                        )

                    devices.torch_gc()

                    x_sample = modules.face_restoration.restore_faces(x_sample)
                    devices.torch_gc()

                image = Image.fromarray(x_sample)

                if p.scripts is not None:
                    pp = scripts.PostprocessImageArgs(image)
                    p.scripts.postprocess_image(p, pp)
                    image = pp.image

                image = apply_overlay(image, p.paste_to, i, p.overlay_images)

                text = infotext(i)
                infotexts.append(text)
                image.info["parameters"] = text
                output_images.append(image)

            del x_samples_ddim

            devices.torch_gc()

            state.nextjob()

        p.color_corrections = None

        index_of_first_image = 0

    if not p.disable_extra_networks and p.extra_network_data:
        extra_networks.deactivate(p, p.extra_network_data)

    devices.torch_gc()

    res = Processed(
        p,
        images_list=output_images,
        seed=p.all_seeds[0],
        info=infotexts[0],
        comments="".join(f"{comment}\n" for comment in comments),
        subseed=p.all_subseeds[0],
        index_of_first_image=index_of_first_image,
        infotexts=infotexts,
    )

    if p.scripts is not None:
        p.scripts.postprocess(p, res)

    return res


def old_hires_fix_first_pass_dimensions(width, height):
    """old algorithm for auto-calculating first pass size"""

    desired_pixel_count = 512 * 512
    actual_pixel_count = width * height
    scale = math.sqrt(desired_pixel_count / actual_pixel_count)
    width = math.ceil(scale * width / 64) * 64
    height = math.ceil(scale * height / 64) * 64

    return width, height


class StableDiffusionProcessingTxt2Img(StableDiffusionProcessing):
    sampler = None
    cached_hr_uc = [None, None]
    cached_hr_c = [None, None]

    def __init__(
        self,
        enable_hr: bool = False,
        denoising_strength: float = 0.75,
        firstphase_width: int = 0,
        firstphase_height: int = 0,
        hr_scale: float = 2.0,
        hr_upscaler: str = None,
        hr_second_pass_steps: int = 0,
        hr_resize_x: int = 0,
        hr_resize_y: int = 0,
        hr_sampler_name: str = None,
        hr_prompt: str = "",
        hr_negative_prompt: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.enable_hr = enable_hr
        self.denoising_strength = denoising_strength
        self.hr_scale = hr_scale
        self.hr_upscaler = hr_upscaler
        self.hr_second_pass_steps = hr_second_pass_steps
        self.hr_resize_x = hr_resize_x
        self.hr_resize_y = hr_resize_y
        self.hr_upscale_to_x = hr_resize_x
        self.hr_upscale_to_y = hr_resize_y
        self.hr_sampler_name = hr_sampler_name
        self.hr_prompt = hr_prompt
        self.hr_negative_prompt = hr_negative_prompt
        self.all_hr_prompts = None
        self.all_hr_negative_prompts = None

        if firstphase_width != 0 or firstphase_height != 0:
            self.hr_upscale_to_x = self.width
            self.hr_upscale_to_y = self.height
            self.width = firstphase_width
            self.height = firstphase_height

        self.truncate_x = 0
        self.truncate_y = 0
        self.applied_old_hires_behavior_to = None

        self.hr_prompts = None
        self.hr_negative_prompts = None
        self.hr_extra_network_data = None

        self.cached_hr_uc = StableDiffusionProcessingTxt2Img.cached_hr_uc
        self.cached_hr_c = StableDiffusionProcessingTxt2Img.cached_hr_c
        self.hr_c = None
        self.hr_uc = None
        self.overlay_images = None

    def init(self, all_prompts, all_seeds, all_subseeds):
        if self.enable_hr:
            if (
                self.hr_sampler_name is not None
                and self.hr_sampler_name != self.sampler_name
            ):
                self.extra_generation_params["Hires sampler"] = self.hr_sampler_name

            if tuple(self.hr_prompt) != tuple(self.prompt):
                self.extra_generation_params["Hires prompt"] = self.hr_prompt

            if tuple(self.hr_negative_prompt) != tuple(self.negative_prompt):
                self.extra_generation_params[
                    "Hires negative prompt"
                ] = self.hr_negative_prompt

            if (
                opts.use_old_hires_fix_width_height
                and self.applied_old_hires_behavior_to != (self.width, self.height)
            ):
                self.hr_resize_x = self.width
                self.hr_resize_y = self.height
                self.hr_upscale_to_x = self.width
                self.hr_upscale_to_y = self.height

                self.width, self.height = old_hires_fix_first_pass_dimensions(
                    self.width, self.height
                )
                self.applied_old_hires_behavior_to = (self.width, self.height)

            if self.hr_resize_x == 0 and self.hr_resize_y == 0:
                self.extra_generation_params["Hires upscale"] = self.hr_scale
                self.hr_upscale_to_x = int(self.width * self.hr_scale)
                self.hr_upscale_to_y = int(self.height * self.hr_scale)
            else:
                self.extra_generation_params[
                    "Hires resize"
                ] = f"{self.hr_resize_x}x{self.hr_resize_y}"

                if self.hr_resize_y == 0:
                    self.hr_upscale_to_x = self.hr_resize_x
                    self.hr_upscale_to_y = self.hr_resize_x * self.height // self.width
                elif self.hr_resize_x == 0:
                    self.hr_upscale_to_x = self.hr_resize_y * self.width // self.height
                    self.hr_upscale_to_y = self.hr_resize_y
                else:
                    target_w = self.hr_resize_x
                    target_h = self.hr_resize_y
                    src_ratio = self.width / self.height
                    dst_ratio = self.hr_resize_x / self.hr_resize_y

                    if src_ratio < dst_ratio:
                        self.hr_upscale_to_x = self.hr_resize_x
                        self.hr_upscale_to_y = (
                            self.hr_resize_x * self.height // self.width
                        )
                    else:
                        self.hr_upscale_to_x = (
                            self.hr_resize_y * self.width // self.height
                        )
                        self.hr_upscale_to_y = self.hr_resize_y

                    self.truncate_x = (self.hr_upscale_to_x - target_w) // opt_f
                    self.truncate_y = (self.hr_upscale_to_y - target_h) // opt_f

            # special case: the user has chosen to do nothing
            if (
                self.hr_upscale_to_x == self.width
                and self.hr_upscale_to_y == self.height
            ):
                self.enable_hr = False
                self.denoising_strength = None
                self.extra_generation_params.pop("Hires upscale", None)
                self.extra_generation_params.pop("Hires resize", None)
                return

            if not state.processing_has_refined_job_count:
                if state.job_count == -1:
                    state.job_count = self.n_iter

                shared.total_tqdm.updateTotal(
                    (self.steps + (self.hr_second_pass_steps or self.steps))
                    * state.job_count
                )
                state.job_count = state.job_count * 2
                state.processing_has_refined_job_count = True

            if self.hr_second_pass_steps:
                self.extra_generation_params["Hires steps"] = self.hr_second_pass_steps

            if self.hr_upscaler is not None:
                self.extra_generation_params["Hires upscaler"] = self.hr_upscaler

    def sample(
        self,
        conditioning,
        unconditional_conditioning,
        seeds,
        subseeds,
        subseed_strength,
        prompts,
    ):
        self.sampler = sd_samplers.create_sampler(self.sampler_name, self.sd_model)

        latent_scale_mode = (
            shared.latent_upscale_modes.get(self.hr_upscaler, None)
            if self.hr_upscaler is not None
            else shared.latent_upscale_modes.get(
                shared.latent_upscale_default_mode, "nearest"
            )
        )
        if self.enable_hr and latent_scale_mode is None:
            if not any(x.name == self.hr_upscaler for x in shared.sd_upscalers):
                raise Exception(f"could not find upscaler named {self.hr_upscaler}")

        x = self.rng.next()

        samples = self.sampler.sample(
            self,
            x,
            conditioning,
            unconditional_conditioning,
            image_conditioning=self.txt2img_image_conditioning(x),
        )

        if not self.enable_hr:
            return samples

        self.is_hr_pass = True

        target_width = self.hr_upscale_to_x
        target_height = self.hr_upscale_to_y

        if latent_scale_mode is not None:
            samples = torch.nn.functional.interpolate(
                samples,
                size=(target_height // opt_f, target_width // opt_f),
                mode=latent_scale_mode["mode"],
                antialias=latent_scale_mode["antialias"],
            )

            # Avoid making the inpainting conditioning unless necessary as
            # this does need some extra compute to decode / encode the image again.
            if (
                getattr(
                    self, "inpainting_mask_weight", shared.opts.inpainting_mask_weight
                )
                < 1.0
            ):
                image_conditioning = self.img2img_image_conditioning(
                    decode_first_stage(self.sd_model, samples), samples
                )
            else:
                image_conditioning = self.txt2img_image_conditioning(samples)
        else:
            decoded_samples = decode_first_stage(self.sd_model, samples)
            lowres_samples = torch.clamp(
                (decoded_samples + 1.0) / 2.0, min=0.0, max=1.0
            )

            batch_images = []
            for i, x_sample in enumerate(lowres_samples):
                x_sample = 255.0 * np.moveaxis(x_sample.cpu().numpy(), 0, 2)
                x_sample = x_sample.astype(np.uint8)
                image = Image.fromarray(x_sample)

                image = images.resize_image(
                    0,
                    image,
                    target_width,
                    target_height,
                    upscaler_name=self.hr_upscaler,
                )
                image = np.array(image).astype(np.float32) / 255.0
                image = np.moveaxis(image, 2, 0)
                batch_images.append(image)

            decoded_samples = torch.from_numpy(np.array(batch_images))
            decoded_samples = decoded_samples.to(shared.device)
            decoded_samples = 2.0 * decoded_samples - 1.0

            samples = self.sd_model.get_first_stage_encoding(
                self.sd_model.encode_first_stage(decoded_samples)
            )

            image_conditioning = self.img2img_image_conditioning(
                decoded_samples, samples
            )

        shared.state.nextjob()

        img2img_sampler_name = self.hr_sampler_name or self.sampler_name

        if self.sampler_name in [
            "PLMS",
            "UniPC",
        ]:  # PLMS/UniPC do not support img2img so we just silently switch to DDIM
            img2img_sampler_name = "DDIM"

        self.sampler = sd_samplers.create_sampler(img2img_sampler_name, self.sd_model)

        samples = samples[
            :,
            :,
            self.truncate_y // 2 : samples.shape[2] - (self.truncate_y + 1) // 2,
            self.truncate_x // 2 : samples.shape[3] - (self.truncate_x + 1) // 2,
        ]

        self.rng = rng.ImageRNG(
            samples.shape[1:],
            self.seeds,
            subseeds=self.subseeds,
            subseed_strength=self.subseed_strength,
            seed_resize_from_h=self.seed_resize_from_h,
            seed_resize_from_w=self.seed_resize_from_w,
        )
        noise = self.rng.next()

        # GC now before running the next img2img to prevent running out of memory
        x = None
        devices.torch_gc()

        if not self.disable_extra_networks:
            with devices.autocast():
                extra_networks.activate(self, self.hr_extra_network_data)

        with devices.autocast():
            self.calculate_hr_conds()

        sd_models.apply_token_merging(
            self.sd_model, self.get_token_merging_ratio(for_hr=True)
        )

        if self.scripts is not None:
            self.scripts.before_hr(self)

        samples = self.sampler.sample_img2img(
            self,
            samples,
            noise,
            self.hr_c,
            self.hr_uc,
            steps=self.hr_second_pass_steps or self.steps,
            image_conditioning=image_conditioning,
        )

        sd_models.apply_token_merging(self.sd_model, self.get_token_merging_ratio())

        self.is_hr_pass = False

        return samples

    def close(self):
        super().close()
        self.hr_c = None
        self.hr_uc = None
        if not opts.experimental_persistent_cond_cache:
            StableDiffusionProcessingTxt2Img.cached_hr_uc = [None, None]
            StableDiffusionProcessingTxt2Img.cached_hr_c = [None, None]

    def setup_prompts(self):
        super().setup_prompts()

        if not self.enable_hr:
            return

        if self.hr_prompt == "":
            self.hr_prompt = self.prompt

        if self.hr_negative_prompt == "":
            self.hr_negative_prompt = self.negative_prompt

        if type(self.hr_prompt) == list:
            self.all_hr_prompts = self.hr_prompt
        else:
            self.all_hr_prompts = self.batch_size * self.n_iter * [self.hr_prompt]

        if type(self.hr_negative_prompt) == list:
            self.all_hr_negative_prompts = self.hr_negative_prompt
        else:
            self.all_hr_negative_prompts = (
                self.batch_size * self.n_iter * [self.hr_negative_prompt]
            )

        self.all_hr_prompts = [
            shared.prompt_styles.apply_styles_to_prompt(x, self.styles)
            for x in self.all_hr_prompts
        ]
        self.all_hr_negative_prompts = [
            shared.prompt_styles.apply_negative_styles_to_prompt(x, self.styles)
            for x in self.all_hr_negative_prompts
        ]

    def calculate_hr_conds(self):
        if self.hr_c is not None:
            return

        self.hr_uc = self.get_conds_with_caching(
            prompt_parser.get_learned_conditioning,
            self.hr_negative_prompts,
            self.steps * self.step_multiplier,
            [self.cached_hr_uc, self.cached_uc],
            self.hr_extra_network_data,
        )
        self.hr_c = self.get_conds_with_caching(
            prompt_parser.get_multicond_learned_conditioning,
            self.hr_prompts,
            self.steps * self.step_multiplier,
            [self.cached_hr_c, self.cached_c],
            self.hr_extra_network_data,
        )

    def setup_conds(self):
        super().setup_conds()

        self.hr_uc = None
        self.hr_c = None

        if self.enable_hr:
            if shared.opts.hires_fix_use_firstpass_conds:
                self.calculate_hr_conds()

            elif lowvram.is_enabled(
                shared.sd_model
            ):  # if in lowvram mode, we need to calculate conds right away, before the cond NN is unloaded
                with devices.autocast():
                    extra_networks.activate(self, self.hr_extra_network_data)

                self.calculate_hr_conds()

                with devices.autocast():
                    extra_networks.activate(self, self.extra_network_data)

    def parse_extra_network_prompts(self):
        res = super().parse_extra_network_prompts()

        if self.enable_hr:
            self.hr_prompts = self.all_hr_prompts[
                self.iteration
                * self.batch_size : (self.iteration + 1)
                * self.batch_size
            ]
            self.hr_negative_prompts = self.all_hr_negative_prompts[
                self.iteration
                * self.batch_size : (self.iteration + 1)
                * self.batch_size
            ]

            self.hr_prompts, self.hr_extra_network_data = extra_networks.parse_prompts(
                self.hr_prompts
            )

        return res


class StableDiffusionProcessingImg2Img(StableDiffusionProcessing):
    sampler = None

    def __init__(
        self,
        init_images: list = None,
        resize_mode: int = 0,
        denoising_strength: float = 0.75,
        image_cfg_scale: float = None,
        mask: Any = None,
        mask_blur: int = None,
        mask_blur_x: int = 4,
        mask_blur_y: int = 4,
        inpainting_fill: int = 0,
        inpaint_full_res: bool = True,
        inpaint_full_res_padding: int = 0,
        inpainting_mask_invert: int = 0,
        initial_noise_multiplier: float = None,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.init_images = init_images
        self.resize_mode: int = resize_mode
        self.denoising_strength: float = denoising_strength
        self.image_cfg_scale: float = (
            image_cfg_scale if shared.sd_model.cond_stage_key == "edit" else None
        )
        self.init_latent = None
        self.image_mask = mask
        self.latent_mask = None
        self.mask_for_overlay = None
        if mask_blur is not None:
            mask_blur_x = mask_blur
            mask_blur_y = mask_blur
        self.mask_blur_x = mask_blur_x
        self.mask_blur_y = mask_blur_y
        self.inpainting_fill = inpainting_fill
        self.inpaint_full_res = inpaint_full_res
        self.inpaint_full_res_padding = inpaint_full_res_padding
        self.inpainting_mask_invert = inpainting_mask_invert
        self.initial_noise_multiplier = (
            opts.initial_noise_multiplier
            if initial_noise_multiplier is None
            else initial_noise_multiplier
        )
        self.mask = None
        self.nmask = None
        self.image_conditioning = None
        self.init_img_hash = None
        self.mask_for_overlay = None
        self.init_latent = None

        self.overlay_images = None

    @property
    def mask_blur(self):
        if self.mask_blur_x == self.mask_blur_y:
            return self.mask_blur_x
        return None

    @mask_blur.setter
    def mask_blur(self, value):
        if isinstance(value, int):
            self.mask_blur_x = value
            self.mask_blur_y = value

    def init(self, all_prompts, all_seeds, all_subseeds):
        self.image_cfg_scale: float = (
            self.image_cfg_scale if shared.sd_model.cond_stage_key == "edit" else None
        )

        self.sampler = sd_samplers.create_sampler(self.sampler_name, self.sd_model)
        crop_region = None

        image_mask = self.image_mask

        if image_mask is not None:
            # image_mask is passed in as RGBA by Gradio to support alpha masks,
            # but we still want to support binary masks.
            image_mask = create_binary_mask(image_mask)

            if self.inpainting_mask_invert:
                image_mask = ImageOps.invert(image_mask)

            if self.mask_blur_x > 0:
                np_mask = np.array(image_mask)
                kernel_size = 2 * int(2.5 * self.mask_blur_x + 0.5) + 1
                np_mask = cv2.GaussianBlur(np_mask, (kernel_size, 1), self.mask_blur_x)
                image_mask = Image.fromarray(np_mask)

            if self.mask_blur_y > 0:
                np_mask = np.array(image_mask)
                kernel_size = 2 * int(2.5 * self.mask_blur_y + 0.5) + 1
                np_mask = cv2.GaussianBlur(np_mask, (1, kernel_size), self.mask_blur_y)
                image_mask = Image.fromarray(np_mask)

            if self.inpaint_full_res:
                self.mask_for_overlay = image_mask
                mask = image_mask.convert("L")
                crop_region = masking.get_crop_region(
                    np.array(mask), self.inpaint_full_res_padding
                )
                crop_region = masking.expand_crop_region(
                    crop_region, self.width, self.height, mask.width, mask.height
                )
                x1, y1, x2, y2 = crop_region

                mask = mask.crop(crop_region)
                image_mask = images.resize_image(2, mask, self.width, self.height)
                self.paste_to = (x1, y1, x2 - x1, y2 - y1)
            else:
                image_mask = images.resize_image(
                    self.resize_mode, image_mask, self.width, self.height
                )
                np_mask = np.array(image_mask)
                np_mask = np.clip((np_mask.astype(np.float32)) * 2, 0, 255).astype(
                    np.uint8
                )
                self.mask_for_overlay = Image.fromarray(np_mask)

            self.overlay_images = []

        latent_mask = self.latent_mask if self.latent_mask is not None else image_mask

        add_color_corrections = (
            opts.img2img_color_correction and self.color_corrections is None
        )
        if add_color_corrections:
            self.color_corrections = []
        imgs = []
        for img in self.init_images:
            # Save init image
            if opts.save_init_img:
                self.init_img_hash = hashlib.md5(img.tobytes()).hexdigest()
                images.save_image(
                    img,
                    path=opts.outdir_init_images,
                    basename=None,
                    forced_filename=self.init_img_hash,
                    save_to_dirs=False,
                )

            image = images.flatten(img, opts.img2img_background_color)

            if crop_region is None and self.resize_mode != 3:
                image = images.resize_image(
                    self.resize_mode, image, self.width, self.height
                )

            if image_mask is not None:
                image_masked = Image.new("RGBa", (image.width, image.height))
                image_masked.paste(
                    image.convert("RGBA").convert("RGBa"),
                    mask=ImageOps.invert(self.mask_for_overlay.convert("L")),
                )

                self.overlay_images.append(image_masked.convert("RGBA"))

            # crop_region is not None if we are doing inpaint full res
            if crop_region is not None:
                image = image.crop(crop_region)
                image = images.resize_image(2, image, self.width, self.height)

            if image_mask is not None:
                if self.inpainting_fill != 1:
                    image = masking.fill(image, latent_mask)

            if add_color_corrections:
                self.color_corrections.append(setup_color_correction(image))

            image = np.array(image).astype(np.float32) / 255.0
            image = np.moveaxis(image, 2, 0)

            imgs.append(image)

        if len(imgs) == 1:
            batch_images = np.expand_dims(imgs[0], axis=0).repeat(
                self.batch_size, axis=0
            )
            if self.overlay_images is not None:
                self.overlay_images = self.overlay_images * self.batch_size

            if self.color_corrections is not None and len(self.color_corrections) == 1:
                self.color_corrections = self.color_corrections * self.batch_size

        elif len(imgs) <= self.batch_size:
            self.batch_size = len(imgs)
            batch_images = np.array(imgs)
        else:
            raise RuntimeError(
                f"bad number of images passed: {len(imgs)}; expecting {self.batch_size} or less"
            )

        image = torch.from_numpy(batch_images)
        image = image.to(shared.device, dtype=devices.dtype_vae)

        if opts.sd_vae_encode_method != "Full":
            self.extra_generation_params["VAE Encoder"] = opts.sd_vae_encode_method

        self.init_latent = sd_samplers_common.images_tensor_to_samples(
            image,
            sd_samplers_common.approximation_indexes.get(opts.sd_vae_encode_method),
            self.sd_model,
        )
        devices.torch_gc()

        if self.resize_mode == 3:
            self.init_latent = torch.nn.functional.interpolate(
                self.init_latent,
                size=(self.height // opt_f, self.width // opt_f),
                mode="bilinear",
            )

        if image_mask is not None:
            init_mask = latent_mask
            latmask = init_mask.convert("RGB").resize(
                (self.init_latent.shape[3], self.init_latent.shape[2])
            )
            latmask = np.moveaxis(np.array(latmask, dtype=np.float32), 2, 0) / 255
            latmask = latmask[0]
            latmask = np.around(latmask)
            latmask = np.tile(latmask[None], (4, 1, 1))

            self.mask = (
                torch.asarray(1.0 - latmask).to(shared.device).type(self.sd_model.dtype)
            )
            self.nmask = (
                torch.asarray(latmask).to(shared.device).type(self.sd_model.dtype)
            )

            # this needs to be fixed to be done in sample() using actual seeds for batches
            if self.inpainting_fill == 2:
                self.init_latent = (
                    self.init_latent * self.mask
                    + create_random_tensors(
                        self.init_latent.shape[1:],
                        all_seeds[0 : self.init_latent.shape[0]],
                    )
                    * self.nmask
                )
            elif self.inpainting_fill == 3:
                self.init_latent = self.init_latent * self.mask

        self.image_conditioning = self.img2img_image_conditioning(
            image * 2 - 1, self.init_latent, image_mask
        )

    def sample(
        self,
        conditioning,
        unconditional_conditioning,
        seeds,
        subseeds,
        subseed_strength,
        prompts,
    ):
        x = self.rng.next()

        if self.initial_noise_multiplier != 1.0:
            self.extra_generation_params[
                "Noise multiplier"
            ] = self.initial_noise_multiplier
            x *= self.initial_noise_multiplier

        samples = self.sampler.sample_img2img(
            self,
            self.init_latent,
            x,
            conditioning,
            unconditional_conditioning,
            image_conditioning=self.image_conditioning,
        )

        if self.mask is not None:
            samples = samples * self.nmask + self.init_latent * self.mask

        del x
        devices.torch_gc()

        return samples

    def get_token_merging_ratio(self, for_hr=False):
        return (
            self.token_merging_ratio
            or (
                "token_merging_ratio" in self.override_settings
                and opts.token_merging_ratio
            )
            or opts.token_merging_ratio_img2img
            or opts.token_merging_ratio
        )

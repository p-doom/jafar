from typing import Dict, Any

import jax
import jax.numpy as jnp
import flax.linen as nn

from utils.nn import STTransformer


class DynamicsMaskGIT(nn.Module):
    """MaskGIT dynamics model"""

    model_dim: int
    ffn_dim: int
    num_latents: int
    num_blocks: int
    num_heads: int
    dropout: float
    mask_limit: float
    param_dtype: jnp.dtype
    dtype: jnp.dtype
    use_flash_attention: bool

    def setup(self):
        self.dynamics = STTransformer(
            self.model_dim,
            self.ffn_dim,
            self.num_latents,
            self.num_blocks,
            self.num_heads,
            self.dropout,
            self.param_dtype,
            self.dtype,
            use_flash_attention=self.use_flash_attention,
        )
        self.patch_embed = nn.Embed(self.num_latents, self.model_dim)
        self.mask_token = self.param(
            "mask_token",
            nn.initializers.lecun_uniform(),
            (1, 1, 1, self.model_dim),
        )
        self.action_up = nn.Dense(
            self.model_dim,
            param_dtype=self.param_dtype,
            dtype=self.dtype,
        )

    def __call__(self, batch: Dict[str, Any], training: bool = True) -> Dict[str, Any]:
        # --- Mask videos ---
        vid_embed = self.patch_embed(batch["video_tokens"])
        if training:
            batch_size = vid_embed.shape[0]
            _rng_prob, *_rngs_mask = jax.random.split(batch["mask_rng"], batch_size + 1)
            mask_prob = jax.random.uniform(
                _rng_prob, shape=(batch_size,), minval=self.mask_limit
            )
            per_sample_shape = vid_embed.shape[1:-1]
            mask = jax.vmap(
                lambda rng, prob: jax.random.bernoulli(rng, prob, per_sample_shape),
                in_axes=(0, 0),
            )(jnp.asarray(_rngs_mask), mask_prob)
            mask = mask.at[:, 0].set(False)
            vid_embed = jnp.where(jnp.expand_dims(mask, -1), self.mask_token, vid_embed)
        else:
            mask = None

        # --- Predict transition ---
        act_embed = self.action_up(batch["latent_actions"])
        vid_embed += jnp.pad(act_embed, ((0, 0), (1, 0), (0, 0), (0, 0)))
        logits = self.dynamics(vid_embed)
        return dict(token_logits=logits, mask=mask)

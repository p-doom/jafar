from dataclasses import dataclass
import os
import time

import einops
from flax.training import orbax_utils
from flax.training.train_state import TrainState
import optax
import orbax
from orbax.checkpoint import PyTreeCheckpointer
import numpy as np
import dm_pix as pix
import jax
import jax.numpy as jnp
import tyro
import wandb

from models.tokenizer import TokenizerVQVAE
from utils.dataloader import get_dataloader

ts = int(time.time())


@dataclass
class Args:
    # Experiment
    num_steps: int = 300_000
    seed: int = 0
    seq_len: int = 16
    image_channels: int = 3
    image_resolution: int = 64
    data_dir: str = "data/coinrun_episodes"
    checkpoint: str = ""
    # Optimization
    vq_beta: float = 0.25
    batch_size: int = 48
    min_lr: float = 3e-4
    max_lr: float = 3e-4
    warmup_steps: int = 10000
    # Tokenizer
    model_dim: int = 512
    latent_dim: int = 32
    num_latents: int = 1024
    patch_size: int = 4
    num_blocks: int = 8
    num_heads: int = 8
    dropout: float = 0.0
    codebook_dropout: float = 0.01
    # Logging
    log: bool = False
    entity: str = ""
    project: str = ""
    log_interval: int = 5
    log_image_interval: int = 250
    ckpt_dir: str = ""
    log_checkpoint_interval: int = 10000
    log_gradients: bool = False


args = tyro.cli(Args)


def tokenizer_loss_fn(params, state, inputs):
    # --- Compute loss ---
    outputs = state.apply_fn(
        params, inputs, training=True, rngs={"dropout": inputs["rng"]}
    )
    mse = jnp.square(inputs["videos"] - outputs["recon"]).mean()
    q_loss = jnp.square(jax.lax.stop_gradient(outputs["emb"]) - outputs["z"]).mean()
    commitment_loss = jnp.square(
        outputs["emb"] - jax.lax.stop_gradient(outputs["z"])
    ).mean()
    loss = mse + q_loss + args.vq_beta * commitment_loss

    # --- Compute validation metrics ---
    gt = inputs["videos"].clip(0, 1).reshape(-1, *inputs["videos"].shape[2:])
    recon = outputs["recon"].clip(0, 1).reshape(-1, *outputs["recon"].shape[2:])
    psnr = pix.psnr(gt, recon).mean()
    ssim = pix.ssim(gt, recon).mean()
    _, index_counts = jnp.unique_counts(
        jnp.ravel(outputs["indices"]), size=args.num_latents, fill_value=0
    )
    codebook_usage = (index_counts != 0).mean()
    metrics = dict(
        loss=loss,
        mse=mse,
        q_loss=q_loss,
        commitment_loss=commitment_loss,
        psnr=psnr,
        ssim=ssim,
        codebook_usage=codebook_usage,
    )
    return loss, (outputs["recon"], metrics)


@jax.jit
def train_step(state, inputs):
    grad_fn = jax.value_and_grad(tokenizer_loss_fn, has_aux=True, allow_int=True)
    (loss, (recon, metrics)), grads = grad_fn(state.params, state, inputs)
    state = state.apply_gradients(grads=grads)
    if args.log_gradients:
        metrics["encoder_gradients_std/"] = jax.tree.map(
            lambda x: x.std(), grads["params"]["encoder"]
        )
        metrics["vq_gradients_std/"] = jax.tree.map(
            lambda x: x.std(), grads["params"]["vq"]
        )
        metrics["decoder_gradients_std/"] = jax.tree.map(
            lambda x: x.std(), grads["params"]["decoder"]
        )
    return state, loss, recon, metrics


if __name__ == "__main__":
    rng = jax.random.PRNGKey(args.seed)
    if args.log:
        wandb.init(entity=args.entity, project=args.project, group="debug", config=args)

    # --- Initialize model ---
    tokenizer = TokenizerVQVAE(
        in_dim=args.image_channels,
        model_dim=args.model_dim,
        latent_dim=args.latent_dim,
        num_latents=args.num_latents,
        patch_size=args.patch_size,
        num_blocks=args.num_blocks,
        num_heads=args.num_heads,
        dropout=args.dropout,
        codebook_dropout=args.codebook_dropout,
    )
    rng, _rng = jax.random.split(rng)
    image_shape = (args.image_resolution, args.image_resolution, args.image_channels)
    inputs = dict(
        videos=jnp.zeros(
            (args.batch_size, args.seq_len, *image_shape), dtype=jnp.float32
        ),
    )
    init_params = tokenizer.init(_rng, inputs)

    # --- Load checkpoint ---
    step = 0
    if args.checkpoint:
        init_params["params"].update(
            PyTreeCheckpointer().restore(args.checkpoint)["model"]["params"]["params"]
        )
        # Assume checkpoint is of the form tokenizer_<timestamp>_<step>
        step += int(args.checkpoint.split("_")[-1])

    # --- Initialize optimizer ---
    lr_schedule = optax.warmup_cosine_decay_schedule(
        args.min_lr, args.max_lr, args.warmup_steps, args.num_steps
    )
    tx = optax.adamw(learning_rate=lr_schedule, b1=0.9, b2=0.9, weight_decay=1e-4)
    train_state = TrainState.create(apply_fn=tokenizer.apply, params=init_params, tx=tx)

    # --- TRAIN LOOP ---
    dataloader = get_dataloader(args.data_dir, args.seq_len, args.batch_size)
    while step < args.num_steps:
        for videos in dataloader:
            # --- Train step ---
            rng, _rng = jax.random.split(rng)
            inputs = dict(videos=videos, rng=_rng)
            train_state, loss, recon, metrics = train_step(train_state, inputs)
            print(f"Step {step}, loss: {loss}")
            step += 1

            # --- Logging ---
            if args.log:
                if step % args.log_interval == 0:
                    wandb.log({"loss": loss, "step": step, **metrics})
                if step % args.log_image_interval == 0:
                    gt_seq = inputs["videos"][0]
                    recon_seq = recon[0].clip(0, 1)
                    comparison_seq = jnp.concatenate((gt_seq, recon_seq), axis=1)
                    comparison_seq = einops.rearrange(
                        comparison_seq * 255, "t h w c -> h (t w) c"
                    )
                    log_images = dict(
                        image=wandb.Image(np.asarray(gt_seq[0])),
                        recon=wandb.Image(np.asarray(recon_seq[0])),
                        true_vs_recon=wandb.Image(
                            np.asarray(comparison_seq.astype(np.uint8))
                        ),
                    )
                    wandb.log(log_images)
                if step % args.log_checkpoint_interval == 0:
                    ckpt = {"model": train_state}
                    orbax_checkpointer = orbax.checkpoint.PyTreeCheckpointer()
                    save_args = orbax_utils.save_args_from_target(ckpt)
                    orbax_checkpointer.save(
                        os.path.join(
                            os.getcwd(), args.ckpt_dir, f"tokenizer_{ts}_{step}"
                        ),
                        ckpt,
                        save_args=save_args,
                    )
            if step >= args.num_steps:
                break

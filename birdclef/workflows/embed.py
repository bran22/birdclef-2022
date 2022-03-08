import click
import pandas as pd
import pytorch_lightning as pl
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger
from torchsummary import summary
from pathlib import Path
from datetime import timedelta

from birdclef.models.embedding import datasets, tilenet

# https://www.pytorchlightning.ai/blog/3-simple-tricks-that-will-change-the-way-you-debug-pytorch
# TODO: add this as a proper test
class CheckBatchGradient(pl.Callback):
    def on_train_start(self, trainer, model):
        n = 0
        example_input = model.example_input_array.to(model.device)
        example_input.requires_grad = True

        model.zero_grad()
        output = model(example_input)
        output[n].abs().sum().backward()

        zero_grad_inds = list(range(example_input.size(0)))
        zero_grad_inds.pop(n)

        if example_input.grad[zero_grad_inds].abs().sum().item() > 0:
            raise RuntimeError("Your model mixes data across the batch dimension!")


@click.group()
def embed():
    pass


@embed.command(name="summary")
@click.argument("metadata", type=click.Path(exists=True, dir_okay=False))
@click.argument("dataset-dir", type=click.Path(exists=True, file_okay=False))
@click.option("--dim", type=int, default=64)
@click.option("--n-mels", type=int, default=64)
def model_summary(metadata, dataset_dir, dim, n_mels):
    metadata_df = pd.read_parquet(metadata)
    data_module = datasets.TileTripletsDataModule(
        metadata_df,
        dataset_dir,
        batch_size=20,
        num_workers=4,
    )
    model = tilenet.TileNet(z_dim=dim, n_mels=n_mels)
    trainer = pl.Trainer(
        gpus=-1,
        # precision=16,
        fast_dev_run=True,
        # callbacks=[CheckBatchGradient()],
    )
    trainer.fit(model, data_module)
    summary(model, model.example_input_array)


@embed.command()
@click.argument("metadata", type=click.Path(exists=True, dir_okay=False))
@click.argument("dataset-dir", type=click.Path(exists=True, file_okay=False))
@click.option("--dim", type=int, default=64)
@click.option("--n-mels", type=int, default=64)
@click.option("--name", type=str, default="tile2vec-v2")
@click.option(
    "--root-dir",
    type=click.Path(file_okay=False),
    default=Path("data/intermediate/embedding"),
)
@click.option("--limit-train-batches", type=int, default=None)
@click.option("--limit-val-batches", type=int, default=None)
def fit(
    metadata,
    dataset_dir,
    dim,
    n_mels,
    name,
    root_dir,
    limit_train_batches,
    limit_val_batches,
):
    root_dir = Path(root_dir)
    metadata_df = pd.read_parquet(metadata)
    data_module = datasets.TileTripletsDataModule(
        metadata_df,
        dataset_dir,
        # With the 900k param model at 16 bits, apparently this can go up to
        # 449959. I don't trust this value though, and empirically 100 per batch
        # fills up gpu memory quite nicely.
        batch_size=100,
        num_workers=6,
    )
    model = tilenet.TileNet(z_dim=dim, n_mels=n_mels)

    trainer = pl.Trainer(
        gpus=-1,
        # using 16-bit precision causes issues with finding the learning rate,
        # and there are often anomalies: RuntimeError: Function 'SqrtBackward0'
        # returned nan values in its 0th output.
        # precision=16,
        # auto_scale_batch_size="binsearch",
        auto_lr_find=True,
        default_root_dir=root_dir / "root",
        logger=TensorBoardLogger(root_dir, name=name, log_graph=True),
        limit_train_batches=limit_train_batches or 1.0,
        limit_val_batches=limit_val_batches or 1.0,
        detect_anomaly=True,
        callbacks=[
            EarlyStopping(monitor="val_loss", mode="min"),
            # NOTE: need to figure out how to change the model so that it
            # actually passes this batch gradient condition.
            # CheckBatchGradient(),
            ModelCheckpoint(
                monitor="val_loss",
                auto_insert_metric_name=True,
                save_top_k=3,
                train_time_interval=timedelta(minutes=15),
            ),
        ],
        # profiler="simple",
    )
    trainer.tune(model, data_module)
    print(f"batch size: {data_module.batch_size}, lr: {model.lr}")
    summary(model, model.example_input_array)
    trainer.fit(model, data_module)


if __name__ == "__main__":
    embed()
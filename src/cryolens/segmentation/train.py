"""A bounded comparison on frozen train/validation data; no final test access."""

from __future__ import annotations

import gc
import json
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch import Tensor, nn
from torch.utils.data import DataLoader, TensorDataset

from cryolens.eval.cohort import digest, file_digest, load_manifest
from cryolens.eval.tracking import code_provenance, tracked_experiment
from cryolens.segmentation.data import development_records, normalize_db, write_json
from cryolens.segmentation.metrics import confusion, summarize
from cryolens.segmentation.models import (
    InferenceWrapper,
    LinearHead,
    UNet,
    frozen_encoder,
    state_digest,
)
from cryolens.segmentation.vendor.croma import get_2dalibi


def verify_cache(config: dict[str, Any], project: Path, dataset: Path) -> dict[str, Any]:
    receipt = json.loads((dataset / "dataset.json").read_text())
    manifest = load_manifest(project / config["manifest"])
    if receipt["test_access"] is not False or receipt["manifest_sha256"] != digest(manifest):
        raise ValueError("Dataset is not bound to the protected development manifest")
    if receipt["configuration_sha256"] != digest(config):
        raise ValueError("Dataset configuration changed")
    for partition in ["train", "validation"]:
        allowed = {r.scene_id: r for r in development_records(manifest, partition)}
        part = receipt["partitions"][partition]
        for pos in part["positions"]:
            record = allowed.get(pos["scene_id"])
            if (
                record is None
                or pos["group_id"] != record.group_id
                or pos["source_sha256"] != record.source_sha256
            ):
                raise ValueError("Chip is outside its permitted frozen partition")
        f = (dataset / part["file"]).resolve()
        if not f.is_relative_to(dataset.resolve()) or file_digest(f) != part["sha256"]:
            raise ValueError("Dataset cache checksum mismatch")
    return dict(receipt)


def evaluate(
    model: nn.Module,
    inputs: Tensor,
    labels: Tensor,
    positions: list[dict[str, Any]],
    batch: int,
    device: str,
) -> dict[str, Any]:
    model.eval()
    matrices: dict[str, Any] = defaultdict(lambda: np.zeros((2, 2), dtype=np.int64))
    start = time.perf_counter()
    with torch.no_grad():
        for first in range(0, len(inputs), batch):
            with torch.autocast(device_type=device, enabled=device == "cuda"):
                logits = model(inputs[first : first + batch].to(device=device, dtype=torch.float32))
            pred = logits.argmax(1).cpu().numpy()
            truth = labels[first : first + batch].numpy()
            for j in range(len(pred)):
                matrices[positions[first + j]["scene_id"]] += confusion(pred[j], truth[j])
    groups: dict[str, Any] = defaultdict(lambda: np.zeros((2, 2), dtype=np.int64))
    scene_groups = {p["scene_id"]: p["group_id"] for p in positions}
    for scene, matrix in matrices.items():
        groups[scene_groups[scene]] += matrix
    overall = summarize(sum(matrices.values(), np.zeros((2, 2), dtype=np.int64)))
    overall.update(
        {
            "scenes": {s: summarize(m) for s, m in matrices.items()},
            "groups": {s: summarize(m) for s, m in groups.items()},
            "scene_macro_mean_iou": float(
                np.mean([summarize(m)["mean_iou"] for m in matrices.values()])
            ),
            "group_macro_mean_iou": float(
                np.mean([summarize(m)["mean_iou"] for m in groups.values()])
            ),
            "elapsed_seconds": time.perf_counter() - start,
        }
    )
    return overall


def encode(
    inputs: Tensor, checkpoint: Path, batch: int, device: str
) -> tuple[Tensor, dict[str, Any]]:
    encoder = frozen_encoder(checkpoint).to(device)
    initial = state_digest(encoder)
    bias = get_2dalibi(16, 256).to(device)
    features = torch.empty((len(inputs), 768, 16, 16), dtype=torch.float16)
    start = time.perf_counter()
    with torch.no_grad():
        for first in range(0, len(inputs), batch):
            with torch.autocast(device_type=device, enabled=device == "cuda"):
                z = encoder(inputs[first : first + batch].to(device), bias)
            features[first : first + len(z)] = (
                z.transpose(1, 2).reshape(-1, 768, 16, 16).cpu().half()
            )
    if state_digest(encoder) != initial or any(
        p.requires_grad or p.grad is not None for p in encoder.parameters()
    ):
        raise ValueError("CROMA encoder did not remain frozen")
    receipt = {
        "encoder_state_sha256_before": initial,
        "encoder_state_sha256_after": initial,
        "frozen": True,
        "feature_shape": list(features.shape),
        "seconds": time.perf_counter() - start,
        "checkpoint_sha256": file_digest(checkpoint),
        "weights_loading": "weights_only=True; CPU map; strict state dict",
    }
    del encoder, bias
    gc.collect()
    if device == "cuda":
        torch.cuda.empty_cache()
    return features, receipt


def run(config: dict[str, Any], project: Path, dataset: Path, output: Path) -> dict[str, Any]:
    cache = verify_cache(config, project, dataset)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "configuration.json", config)
    write_json(output / "dataset.json", cache)
    provenance = code_provenance(project)
    write_json(output / "executed-code.json", provenance)
    torch.set_num_threads(4)
    random.seed(config["seed"])
    np.random.seed(config["seed"])
    torch.manual_seed(config["seed"])
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if config["require_cuda"] and device != "cuda":
        raise ValueError("CUDA required by the predeclared execution; no silent CPU substitution")
    if device == "cuda":
        torch.cuda.manual_seed_all(config["seed"])
    arrays = {}
    for partition in ["train", "validation"]:
        with np.load(dataset / cache["partitions"][partition]["file"], allow_pickle=False) as z:
            arrays[partition] = (
                torch.from_numpy(normalize_db(z["inputs_db"])),
                torch.from_numpy(z["labels"].astype(np.int64)),
            )
    train_x, train_y = arrays["train"]
    val_x, val_y = arrays["validation"]
    train_counts = np.asarray(cache["partitions"]["train"]["class_counts"])
    majority = int(train_counts.argmax())
    constant = summarize(confusion(np.full(val_y.shape, majority), val_y.numpy()))
    report: dict[str, Any] = {
        "schema_version": 1,
        "task": "sea_ice_segmentation",
        "development_only": True,
        "test_access": False,
        "manifest_sha256": cache["manifest_sha256"],
        "configuration_sha256": digest(config),
        "dataset_receipt_sha256": file_digest(dataset / "dataset.json"),
        "code": provenance,
        "hardware": {
            "device": device,
            "torch": torch.__version__,
            "cuda_runtime": torch.version.cuda,
            "bitwise_retraining_guaranteed": False,
            "gpu": torch.cuda.get_device_name() if device == "cuda" else None,
        },
        "normalization": "physical dB clipped [-60,10], HH offset -20, HV offset -30, both scale 10; same for both models, no test or per-scene fitting",
        "majority_train_baseline": constant,
        "models": {},
        "claim_boundary": "Preliminary sampled development chart agreement. One seed, fixed short training budget, validation selected checkpoints. No test performance, target precision/recall or iceberg identity accuracy.",
        "foundation_transfer_limits": [
            "VV/VH land pretraining transferred to HH/HV ocean/ice",
            "10m pretraining versus nominal80m data",
            "Declared input adapter is a research transfer choice",
            "Frozen token grid is 8 samples; interpolation does not restore sub-token information",
        ],
    }
    weights = torch.tensor(
        train_counts.sum() / (2 * train_counts), dtype=torch.float32, device=device
    )
    if not torch.isfinite(weights).all():
        raise ValueError("Both training classes are required")
    for name in ["unet", "croma_frozen"]:
        folder = output / name
        folder.mkdir()
        features = None
        input_train, input_val = train_x, val_x
        if name == "croma_frozen":
            features, train_encoding = encode(
                train_x, Path(config["croma_checkpoint"]), config["batch_size"], device
            )
            input_val, val_encoding = encode(
                val_x, Path(config["croma_checkpoint"]), config["batch_size"], device
            )
            input_train = features
            write_json(
                folder / "frozen-encoder.json",
                {"train": train_encoding, "validation": val_encoding},
            )
        torch.manual_seed(config["seed"])
        model = (UNet() if name == "unet" else LinearHead()).to(device)
        optimizer = torch.optim.AdamW(
            model.parameters(), lr=config["learning_rate"], weight_decay=config["weight_decay"]
        )
        loss_fn = nn.CrossEntropyLoss(weight=weights, ignore_index=255)
        generator = torch.Generator().manual_seed(config["seed"])
        loader = DataLoader(
            TensorDataset(input_train, train_y),
            batch_size=config["batch_size"],
            shuffle=True,
            generator=generator,
        )
        history = []
        best = -1.0
        started = time.perf_counter()
        with tracked_experiment(
            project / config["manifest"],
            project,
            Path(config["data_root"]),
            "sea_ice_segmentation",
            "train",
            {**config, "model_kind": name},
            config["tracking_uri"],
            experiment_name="cryolens-seaice-development-v1",
        ) as tracking:
            tracking.log_artifact(dataset / "dataset.json", "dataset")
            tracking.client.set_tag(tracking.run_id, "validation_partition", "development_only")
            tracking.client.set_tag(tracking.run_id, "training_seed_count", "1")
            if name == "croma_frozen":
                tracking.log_artifact(folder / "frozen-encoder.json", "provenance")
            scaler = torch.amp.GradScaler(device, enabled=device == "cuda")
            for epoch in range(1, config["epochs"] + 1):
                model.train()
                losses = []
                for x, y in loader:
                    optimizer.zero_grad(set_to_none=True)
                    with torch.autocast(device_type=device, enabled=device == "cuda"):
                        logits = model(x.to(device=device, dtype=torch.float32))
                        loss = loss_fn(logits, y.to(device))
                    if not torch.isfinite(loss):
                        raise ValueError("Nonfinite training loss")
                    scaler.scale(loss).backward()
                    scaler.step(optimizer)
                    scaler.update()
                    losses.append(float(loss.detach()))
                result = evaluate(
                    model,
                    input_val,
                    val_y,
                    cache["partitions"]["validation"]["positions"],
                    config["batch_size"],
                    device,
                )
                row = {
                    "epoch": epoch,
                    "train_loss": float(np.mean(losses)),
                    "validation_mean_iou": result["mean_iou"],
                    "validation": result,
                }
                history.append(row)
                tracking.log_metrics(
                    {
                        "train_loss": row["train_loss"],
                        "validation_mean_iou": result["mean_iou"],
                        "validation_group_macro_mean_iou": result["group_macro_mean_iou"],
                    },
                    step=epoch,
                )
                if epoch == 1:
                    torch.save(model.state_dict(), folder / "epoch1.pt")
                if result["mean_iou"] > best:
                    best = result["mean_iou"]
                    torch.save(model.state_dict(), folder / "best.pt")
                    write_json(folder / "best-validation.json", result)
                    best_epoch = epoch
                print(
                    name,
                    "epoch",
                    epoch,
                    "loss",
                    round(row["train_loss"], 4),
                    "validation mIoU",
                    round(result["mean_iou"], 4),
                    flush=True,
                )
            torch.save(model.state_dict(), folder / "final.pt")
            write_json(folder / "history.json", history)
            if name == "unet":
                for tag, weights_file in [
                    ("epoch1", "epoch1.pt"),
                    ("best", "best.pt"),
                    ("final", "final.pt"),
                ]:
                    fresh = UNet().eval()
                    fresh.load_state_dict(
                        torch.load(folder / weights_file, weights_only=True, map_location="cpu")
                    )
                    wrapped = InferenceWrapper(fresh).eval()
                    traced = torch.jit.trace(wrapped, torch.zeros(1, 2, 128, 128))
                    traced.save(str(folder / (tag + "-inference.pt")))
            model.load_state_dict(
                torch.load(folder / "best.pt", weights_only=True, map_location=device)
            )
            model.eval()
            predictions = []
            with torch.no_grad():
                for start in range(0, len(input_val), config["batch_size"]):
                    with torch.autocast(device_type=device, enabled=device == "cuda"):
                        logits = model(
                            input_val[start : start + config["batch_size"]].to(
                                device=device, dtype=torch.float32
                            )
                        )
                    predictions.append(logits.argmax(1).cpu().numpy().astype(np.uint8))
            np.savez_compressed(
                folder / "validation-predictions.npz", predictions=np.concatenate(predictions)
            )
            model_result = {
                "run_id": tracking.run_id,
                "trainable_parameters": sum(
                    p.numel() for p in model.parameters() if p.requires_grad
                ),
                "epochs": config["epochs"],
                "best_epoch": best_epoch,
                "best_validation": json.loads((folder / "best-validation.json").read_text()),
                "seconds": time.perf_counter() - started,
                "best_weights_sha256": file_digest(folder / "best.pt"),
                "inference_artifacts": {
                    f.name: file_digest(f) for f in folder.glob("*-inference.pt")
                },
            }
            write_json(folder / "model-result.json", model_result)
            for file in ["model-result.json", "history.json", "best-validation.json", "best.pt"]:
                tracking.log_artifact(folder / file)
            report["models"][name] = model_result
        del model, optimizer, features, input_train, input_val
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()
    report["peak_gpu_allocated_bytes"] = (
        torch.cuda.max_memory_allocated() if device == "cuda" else None
    )
    if code_provenance(project)["code_sha256"] != provenance["code_sha256"]:
        raise ValueError(
            "Executing source changed during training; retain output as failed evidence"
        )
    verify_cache(config, project, dataset)
    write_json(output / "comparison.json", report)
    return report

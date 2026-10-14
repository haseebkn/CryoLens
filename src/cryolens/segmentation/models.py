"""Small U-Net and a frozen official CROMA radar encoder with a linear pixel head."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import torch
from torch import Tensor, nn
from torch.nn import functional

from cryolens.eval.cohort import file_digest
from cryolens.segmentation.vendor.croma import ViT

CROMA_SHA256 = "0238d814b53108f3574bf1ea240e38a0a6edd46173816d9a6962070561893b63"


def block(cin: int, cout: int) -> nn.Sequential:
    return nn.Sequential(
        nn.Conv2d(cin, cout, 3, padding=1),
        nn.GroupNorm(4, cout),
        nn.ReLU(),
        nn.Conv2d(cout, cout, 3, padding=1),
        nn.GroupNorm(4, cout),
        nn.ReLU(),
    )


class UNet(nn.Module):
    """A reproducible small baseline, two SAR inputs and two chart-proxy logits."""

    def __init__(self) -> None:
        super().__init__()
        self.e1, self.e2, self.e3 = block(2, 16), block(16, 32), block(32, 64)
        self.middle = block(64, 128)
        self.d3, self.d2, self.d1 = block(192, 64), block(96, 32), block(48, 16)
        self.output = nn.Conv2d(16, 2, 1)

    def forward(self, x: Tensor) -> Tensor:
        a = self.e1(x)
        b = self.e2(functional.max_pool2d(a, 2))
        d = self.e3(functional.max_pool2d(b, 2))
        z = self.middle(functional.max_pool2d(d, 2))
        z = self.d3(
            torch.cat(
                [
                    functional.interpolate(
                        z, size=d.shape[-2:], mode="bilinear", align_corners=False
                    ),
                    d,
                ],
                1,
            )
        )
        z = self.d2(
            torch.cat(
                [
                    functional.interpolate(
                        z, size=b.shape[-2:], mode="bilinear", align_corners=False
                    ),
                    b,
                ],
                1,
            )
        )
        z = self.d1(
            torch.cat(
                [
                    functional.interpolate(
                        z, size=a.shape[-2:], mode="bilinear", align_corners=False
                    ),
                    a,
                ],
                1,
            )
        )
        return self.output(z)


class LinearHead(nn.Module):
    """Trainable 1x1 linear probe on the 8-sample CROMA token grid."""

    def __init__(self) -> None:
        super().__init__()
        self.classifier = nn.Conv2d(768, 2, 1)

    def forward(self, features: Tensor) -> Tensor:
        return functional.interpolate(
            self.classifier(features), size=(128, 128), mode="bilinear", align_corners=False
        )


def frozen_encoder(checkpoint: Path) -> Any:
    if file_digest(checkpoint) != CROMA_SHA256:
        raise ValueError("Official CROMA checkpoint checksum mismatch")
    weights = torch.load(checkpoint, weights_only=True, map_location="cpu")
    encoder = ViT(dim=768, depth=6, in_channels=2)
    encoder.load_state_dict(weights["s1_encoder"], strict=True)
    encoder.requires_grad_(False)
    encoder.eval()
    return encoder


def state_digest(model: nn.Module) -> str:
    h = hashlib.sha256()
    for name, value in model.state_dict().items():
        h.update(name.encode())
        h.update(value.detach().cpu().contiguous().numpy().tobytes())
    return h.hexdigest()


class InferenceWrapper(nn.Module):
    """Accept physical dB and apply the training adapter within the saved artifact."""

    def __init__(self, model: nn.Module) -> None:
        super().__init__()
        self.model = model
        self.register_buffer("means", torch.tensor([-20.0, -30.0]).view(1, 2, 1, 1))

    def forward(self, inputs_db: Tensor) -> Tensor:
        normalized = (torch.clamp(inputs_db, -60, 10) - self.means) / 10.0
        return torch.softmax(self.model(normalized), dim=1)

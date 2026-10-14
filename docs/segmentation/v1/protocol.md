# Development chart-proxy comparison v1

Declared before dataset preparation or training, 14 October 2026. This is an
initial, bounded experiment; final test access remains closed.

- Use only frozen `train` and `validation` records with available publisher
  chart proxies. Group IDs, input aliases, AOI and partitions remain unchanged.
  Reject `test` at the development-selection boundary, before opening a source.
- Keep HH/HV order. Restore physical dB with the existing publisher constants.
  Sample nonoverlapping 128 by 128 cores on an origin-aligned grid. Rank positions
  with seed 42 and source hash; take up to 32 eligible training chips and 16
  validation chips per scene. Selection uses geographic/valid-label support,
  never class prevalence, model outcomes or optical coverage.
- Eligible pixels must be inside the frozen NL marine AOI, outside GSHHG land
  and its 500 m coastal buffer, have finite HH/HV and measured incidence 15-50
  degrees, known offshore distance zones and known SIC. At least 75% of the chip
  must be scoreable. Record scenes with no eligible support rather than replacing
  them with favourable scenes. SIC 0-1 is open water; 2-10 is ice affected; all
  other values are ignored. Unknowns never become water. Both ice and water stay.
- Both models get identical dB inputs, clipped to [-60, 10], then fixed channel
  offsets [-20, -30] and scales [10, 10]. No test, scene-dependent percentile or
  fitted normalization is used. Ineligible SAR context is neutralized; unknown
  chart labels are excluded from loss and metrics.
- U-Net: small three-level model from scratch. CROMA: official base radar ViT,
  frozen weights, 1x1 linear segmentation head on its 8-sample token grid,
  bilinearly expanded to 128 samples. Only the head is trained. Encoder state
  hashes before/after encoding and absent gradients must agree.
- Six epochs, batch 8, AdamW learning rate 0.001, weight decay 0.0001, seed 42.
  Train-only inverse-frequency loss weights, same batch order and chips. Select
  best checkpoint by validation pixel-aggregated mean IoU; report all epochs.
  This development checkpoint selection is not an unbiased held-out estimate.
- Report per-class precision, recall, F1 and IoU; pixel confusion; scored pixels;
  per-scene and per-acquisition-group results and macro mean IoU; train-majority
  baseline; timings and actual GPU usage. Undefined metrics stay null. No pixel
  independence confidence interval is claimed. A single short run is preliminary.
- No protected test source, chart, reference review, detector or target metric is
  used. Chart agreement does not establish iceberg/ship detection or identity.

## Foundation-model limitations and attribution

CROMA was pretrained on VV/VH land imagery. Applying HH/HV ocean/ice at nominal
80 m is a polarization, resolution and domain transfer experiment. The declared
normalization is a transfer adapter, not a claim to reproduce its land benchmarks.
Frozen features can lose against the U-Net; report the measured direction.

Official source: https://github.com/antofuller/CROMA at
`59505a6bcadbf36ba20767270154bf9f3067c5e7`. The unmodified inference implementation
is vendored with its MIT license and excluded only from first-party formatting
and static-type policy. Maintained adapters and tests remain checked.
Checkpoint: https://huggingface.co/antofuller/CROMA at
`0dd28e3d633bd6715856ae9890e8c49360040598`, `CROMA_base.pt`, 777,563,846 bytes,
SHA256 `0238d814b53108f3574bf1ea240e38a0a6edd46173816d9a6962070561893b63`.
Load with PyTorch `weights_only=True`, CPU mapping and strict encoder state dict.

Citation: Fuller, Millard and Green, CROMA: Remote Sensing Representations with
Contrastive Radar-Optical Masked Autoencoders, NeurIPS 2023.
AI4Arctic: Buus-Hinkler et al., DTU, DOI 10.11583/DTU.21316608.v3, CC BY 4.0.

## Lifecycle and release boundary

Use actual trained U-Net artifacts for MLflow signatures/registry and explicitly
named research aliases. Demonstrate CPU container inference against saved local
predictions, then promotion and rollback by exact version/hash. This proves a
local research lifecycle; it does not authorize fresh-SAFE target detection,
automatic retraining or operational maritime deployment. Step 4 and full-pipeline
target metrics remain unfinished. Release materials must retain these limits.

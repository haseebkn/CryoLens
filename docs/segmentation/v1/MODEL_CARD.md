# U-Net and frozen CROMA chart-proxy model card

Date: 14 October 2026. Status: preliminary development research models.

**Purpose:** predict AI4Arctic chart-derived open water (SIC 0-1) versus
ice-affected areas (SIC 2-10) from HH/HV SAR. Unknown chart/invalid/land/coastal
pixels are excluded. These classes are chart proxies, not field truth.

**Data:** existing real 80 m AI4Arctic EW HH/HV observations inside the frozen
NL marine study polygon. 30 train scenes / 20 groups (960 nonoverlapping chips),
10 validation scenes / seven groups (160 chips). No project test source was used.
Prior analytical and AI-assisted exposure of development acquisitions remains
disclosed by the frozen evaluation protocol. Both models use the same pixels,
fixed adapter and masked support; chart labels are not model input channels.

**Models:** small U-Net, 488,578 trained parameters; official CROMA base radar
encoder, strictly frozen, with 1,538 trained linear-head parameters. Only radar
features are used; no optical encoder/fusion was trained. CROMA's VV/VH land
pretraining differs from HH/HV ocean/ice and nominal spatial spacing here.

**Training:** one seed (42), six epochs, class weights estimated on training
only, AdamW, mixed precision. Best epoch selected on development validation;
both best checkpoints occurred at epoch 4. Reported pooled validation mean IoU:
U-Net 0.9185, CROMA 0.9309. Group macro means: 0.8739 and 0.8733. No reliable
model superiority, regional accuracy or final test score is established.

**Important limitation:** 149/160 validation chips are single-class chart
interiors. On the exploratory 11 mixed-chart chips, mean IoU falls to 0.5371
and 0.6133. That subset is a post-training inspection, not another held-out set.
Nominal pixel counts are dependent within scenes/groups. Multi-seed variance,
boundary-sensitive performance, probability calibration, seasonal transfer,
absolute geolocation error and independent field truth remain unevaluated.

**Serving:** the U-Net TorchScript artifact accepts finite float32 physical
sigma0 HH/HV dB, [1,2,128,128]. It embeds the training clipping/normalization and
returns open-water/ice-affected class-score maps. Apply original validity masks
outside the model. The prototype has no spatial georeferencing in its API;
the caller must preserve the source grid. Scores are not calibrated confidence.

**Suitable use:** research comparison, reproducibility exercises and the
documented local MLflow/Docker lifecycle demonstration. Test only against
appropriate development data or a separately authorized, locked evaluation.

**Unsupported use:** automatic fresh-SAFE sea-ice masking, target identification,
ship/iceberg detection accuracy claims, operational maritime decisions,
navigation, live AIS/RF fusion or drift forecasting. The model does not resolve
step 4 or establish low target false positives. No third-party endorsement or
proprietary protocol certification is claimed.

**Provenance/licenses:** see protocol.md and comparison.json for exact checkpoint,
data, code and configuration hashes. Actual trained artifacts and source snapshots
remain separately bound; later software commits are not represented as having
executed earlier science. Project/U-Net and upstream CROMA software use MIT;
AI4Arctic uses CC BY 4.0. Cite Buus-Hinkler et al., DTU,
10.11583/DTU.21316608.v3 and Fuller, Millard and Green, NeurIPS 2023.

# Roadmap audit: PRs #6–#9

Later processing investigation: [v2](processing-validation/v2/README.md) corrects
the reference cropping, height/timing assumptions, resampling and short-block
noise handling. The geometric conclusions below are the historical audit
snapshot; the earlier radiometric rejections remain preserved. The current [v5 output validation](processing-validation/v5/README.md) closes representative complete EW/IW preprocessing; fresh target deployment still requires its separate release conditions.

See the [current processing report](processing-validation/v5/README.md) for
subsequent acquisition outcomes and remaining step-2 qualifications. The table
below retains the earlier audit snapshot rather than rewriting it.

Step 3 continuation: [fresh SAFE optical review](optical-review/fresh-v1/README.md) adds receipt-bound EW/IW review locations, real optical downloads and measured visibility. The table below remains the dated audit snapshot; human reference completion is still separate.

Steps 5–8 continuation: the [development comparison](segmentation/v1/README.md)
owns the later model experiment, local lifecycle and portable-candidate evidence,
including remaining release requirements. The table below retains the earlier
audit snapshot.

Audit date: 12 October 2026. Reviewed stack head:
`e29986877035588f50dff47ecbdfc39df86a02f4`.
This is an implementation and development-evidence audit, not a scientific
release, independent iceberg ground truth, or certification against external operational standards.
The existing review workspaces and human submissions were inspected read-only.

## What is actually complete

| Roadmap step | Verified delivery | Still required |
|---|---|---|
| 1. Scope and evaluation freeze | Versioned NL study polygon, acquisition partitions, task definitions, annotation/matching rules and actual MLflow provenance runs. The two newer PR #5 documentation/tracking commits are carried into this audit branch. | Preserve the frozen partitions. Footprint coverage is not eligible surveyed coverage. Disclose later reference-only and AI-assisted exposure. |
| 2. Fresh Sentinel-1 processing (#6) | Authentic EW and HH/HV IW inputs; calibration/noise/geolocation comparison machinery; documented SNAP execution; quality rejection gates; NERSC experiment. | **Not complete: deployment gate closed.** None of the three products passes combined usability and comparison requirements. Resolve the failures, then demonstrate representative passing products. IW ingestion support is not validated operational southern-NL coverage. |
| 3. Sentinel-2 paired review (#7–#8) | Planetary Computer and CDSE ingestion, native optical/quality layers, acquisition provenance, paired chips, visibility assessment and analyst evidence API/UI. Real CDSE archives and prepared artifacts are checked below. | Demonstrate useful optical evidence at a meaningful sampling scale. Current download availability is not reliable identity corroboration. Missing/clouded imagery must never make a SAR target false. |
| 4. Reference set (#9) | Whole-area survey preparation, retained/rejected queues, stratified sampling probabilities, human submission ledger, concealed-answer consistency packets, adjudication and freeze tooling. | **Not complete:** finish real human dispositions, consistency review and adjudication; then freeze exact scored masks and evidence. AI observations and nonblind human feedback are recorded separately. |
| 5. U-Net versus CROMA | No implemented experiment or result found in this stack; PyTorch is absent from the checked project environment. | Train/evaluate development baselines with leakage controls, then conduct the locked evaluation. |
| 6. Full-pipeline measurement | Candidate generation/suppression accounting and metric helpers exist. | No valid end-to-end precision, recall, per-stage recall or empirical false-alarm report exists. Requires completed reference work and explicit uncertainty/sampling treatment. |
| 7. ML lifecycle | Dataset/provenance tracking exists. | Demonstrated model training runs, registry/aliases, model-serving container and witnessed promotion/rollback remain absent. |
| 8. Package/release | Buildable package, API/dashboard, documentation and CI checks exist. | Finish the scientific limitations/model cards and clean-install release demonstration; no repository release tag existed at audit. |

The study polygon is an offshore research AOI, not Newfoundland and Labrador's
legal provincial boundary. Acceptance thresholds are project engineering rules,
not external maritime operational standards. Do not advertise this system as a
navigation product or an iceberg-identification service.

## Issues fixed

1. **Reference cache integrity (#6):** SNAP receipts omitted ENVI headers. Those
   headers control raster decoding even when image bytes are unchanged. Receipts
   now require the complete output inventory, headers and sidecars; missing,
   modified or incomplete inventories fail closed. Historical references were
   rerun, not silently re-signed.
2. **Empty scientific comparison (#6):** an empty sampling plan could reach an
   all-passed reduction without any actual comparisons. The CLI now rejects empty,
   duplicate or malformed products/windows before launching the reference.
3. **Optical evidence provenance (#7–#8):** evidence eligibility read database
   metadata while artifact integrity checked a separate manifest. Those could
   disagree. Artifact/evidence requests now reject metadata that differs from the
   hash-verified manifest, including pair/detection identity.
4. **Review ordering (#9):** importing survey responses first did not prove that
   the candidate inspection occurred afterward. Earlier candidate timestamps now
   fail. Responses cannot predate their issued consistency packet; adjudication
   must follow every comparison review, including reviews whose answer was not
   selected for the final reference.
5. **Consistency sample (#9):** a caller could reduce the repeat below the frozen
   policy's 20%. The frozen minimum is now enforced.
6. **AI-assistance disclosure (#9):** progress and frozen references now expose
   separately recorded AI observations/nonblind feedback. Freezing requires an
   explicit assistance disclosure when such events exist. Concealing previous
   answers does not erase earlier exposure or establish independent accuracy.

These changes do not relabel any pilot image or turn a possible iceberg into a
confirmed iceberg. The existing viewer and submission ledgers are unchanged.

## Real-data evidence

The machine-readable [audit receipt](audits/pr6-9/report.json) distinguishes fresh
execution from historical measurements. Reproduce against an evidence checkout:

```powershell
python validation/audit_roadmap.py --evidence-root E:\CryoLens --output data/processed/new-audit
```

Use a new output directory; SNAP and the documented local reference image are
required. No download credentials are printed or included in the receipt.

The three original SAFE manifests, annotations and measurement bytes are
rehashed. Full-scene non-positive-HV figures are **historical measurements** bound
to those unchanged inputs and the original reader/quality code; this audit does
not claim a fresh full-scene scan. They remain approximately 45.7% (legacy EW),
26.1% (fresh EW) and 54.3% (IW), above the 5% project rejection threshold. These
full-footprint percentages are not NL eligible-water false-alarm rates.

All **27 windows were freshly executed with SNAP** and their complete output
inventories rehashed. Window comparisons passed in 3/9 legacy EW, 9/9 fresh EW,
and 0/9 IW windows. The fresh EW comparison success does not override its
separate failed whole-product quality check. All three deployment decisions
remain rejection. See the [27 new receipts](audits/pr6-9/reference-receipts/).

Both real CDSE Sentinel-2 ZIPs pass catalogue MD5, ZIP CRC and all 86 referenced
SAFE manifest checks each. Advertised BLAKE3 remains explicitly unchecked.
All **100 completed local pair manifests** pass their recorded artifact hashes:
98 Planetary Computer contexts and two CDSE contexts. Six have optical items,
and zero are marked useful for review. A missing optical item represents a
radar-only/unavailable-evidence context, not a downloaded optical image.

At **2026-10-12 06:11 UTC**, both sealed reference workspaces and append-only
ledger chains passed integrity checks. The held-out pilot had **46/64 formal
human submissions**, leaving 18; development had **0/289 formal human submissions**.
There were zero issued consistency packets and zero freezes. Separately recorded
AI review events cover the assisted work already discussed with the user; they
are not additional formal human submissions. These are dated ledger counts,
not browser-draft counts, and may increase as the user finishes reviewing.

The audit executed from uncommitted audit fixes. Its receipt preserves the
source hashes captured when execution began; subsequent formatting of
`validation.py` and the audit driver means those two hashes do not describe the
final committed bytes. The matched-pixel comparison functions were unchanged;
the driver subsequently gained a guard against running with assertions disabled.
The receipt is retained as observed, not rewritten to pretend a future commit
executed. New SNAP receipts bind actual runtime, input, graph and output bytes.

The optical pilot is also deliberately limited: catalogue overlap was found for
15 of 25 SAR acquisitions within a 12-hour search window, before candidate
visibility assessment. The original two-scene, 34-candidate preparation found
one candidate with downloaded optical context and zero useful optical visibility.
Multiple contexts for one candidate are not independent confirmations. Frozen
identity rules remain stricter than catalogue search. CDSE and Planetary Computer
products from different processing baselines are not exact-product equivalents.

## Verification and next handoff

Regression tests exercise altered headers, incomplete receipts, invalid plans,
metadata disagreement and review chronology/policy failures. Database checks use
real PostGIS and roll back their fixtures. Package checks run outside the checkout.
Generated artifacts and compact provenance are committed; raw rasters remain local.

Local checks: **335 passed** in the portable suite (14 archive-dependent tests
skipped in the isolated checkout), then **13 passed and one skipped** when the
real archive integration suite ran against the original data with the audited
source. That remaining skip was the target-property check because no candidate
survived on its selected development scene. Ruff and formatting pass, mypy passes
118 files, real PostGIS checks pass with rollback, and the built wheel works
outside the repository. The final review-chronology change was also checked by
rerunning all 16 reference-set tests.

After the primary human pilot is complete, issue the frozen random 20% repeat
at least seven days after the latest primary review, resolve disagreements and
freeze the reference. A repeat measures consistency, not correctness. An
independent reviewer and suitable optical evidence remain useful additional
evidence. Do not compute claimed operational accuracy from AI-assisted guesses.

PRs #6–#9 are stacked, not four independent changes. This audit is additive and
does not merge them or authorize fresh-SAFE deployment. The scientific failures
remain visible rather than weakening thresholds to turn the status green.

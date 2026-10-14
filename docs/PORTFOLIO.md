# Portfolio walkthrough

## Current presentation package - 14 October 2026

Use [v0.1.0-research.2](https://github.com/haseebkn/CryoLens/releases/tag/v0.1.0-research.2)
for the final five-page evidence report, offline HTML walkthrough, timed speaker
script and portable installed-wheel CPU demonstration. The HTML presents
recorded results; actual inference is a separate executable check documented
inside the ZIP. Dependency installation requires internet. The release's
Windows CPU check does not qualify fresh-SAFE model deployment or other platforms.

The intended NL maritime domain awareness scope remains unchanged. This is an
intermediate research delivery: available public imagery and sea-ice labels
support the demonstrated work, but sufficient verified acquisition-matched
target truth has not been assembled. Public-data limitations, unfinished human
review and remaining engineering/evaluation work are separate constraints.
Do not claim that lack of public data explains every gap or that suitable
public target truth cannot exist.

See the [current roadmap and remaining work](../README.md#roadmap-demonstrated-progress-and-remaining-work).
The pilot's 64 primary dispositions and five amendments count; consistency,
adjudication and reference freezing remain. Target precision, recall and
empirical false alarms are still unmeasured. Earlier scientific records retain
their actual dates and source identities.

## One-minute introduction

"CryoLens is a Newfoundland and Labrador SAR screening project. I built a
traceable path from satellite measurements to unverified radar candidates and
analyst review. The main engineering work is preserving physical units,
excluding unreliable pixels, measuring coverage and exposing uncertainty. I
cannot currently claim iceberg precision or recall: the public evaluation data
contains sea-ice charts, not acquisition-matched iceberg truth."

## Optional local dashboard demonstration

This requires local data and services. For the prepared five-minute presentation,
use the release package above; empty dashboards must not be filled with mock results.

1. Open the dashboard and show the NL study area and the actual observation
   dates. Explain the distinction between historical imagery and live coverage.
2. Inspect a real candidate, if imported, and show its source, radiometry,
   unclassified state and detector score semantics. Empty data stays empty.
3. Walk through one real-data report's analyzed coverage and per-stage removal
   ledger. Explain which masks reduced coverage and why fewer candidates do
   not prove better detection.
4. Show the analyst review interface and explain that writes require a locally
   configured identity and API key. Do not display the key during the demo.
5. Show the audit regressions and real PostGIS CI check. Finish with the next
   scientific requirement: independent regional target labels and AIS context.

## Claims to make

- "I implemented and tested a statistical candidate-screening baseline."
- "I constrained analysis and serving to a documented NL shelf research area."
- "I removed fabricated fallback results and corrected evaluation leakage."
- "I can explain the failure modes and how to measure the next improvement."

## Claims to avoid

- C-CORE certified/compliant, a clone of Coresight, or endorsed by C-CORE.
- A proven false-positive reduction, precision, recall or operational Pfa.
- A trained iceberg classifier, live AIS integration or validated drift forecast.
- Radar component lengths as measured iceberg dimensions.
- Old 39-scene headline numbers as current performance after the audit.

Read [MDA alignment](MDA_ALIGNMENT.md), [audit](AUDIT.md),
[benchmark evidence](BENCHMARK.md) and [limitations](LIMITATIONS.md) before
presenting. Source data and records used in a demo should have provenance and
should be clearly distinguished from the automated test fixtures.

## Model development evidence (14 October 2026)

[U-Net versus frozen CROMA](segmentation/v1/README.md) is now an executed,
tracked development experiment. Its model card discloses chart proxies,
homogeneous-chip bias, mixed-chip performance, polarization/domain transfer
and untouched final-test evaluation. The local registry/signature/container
promotion and rollback have actual receipts. These improve the foundation-model
evaluation and ML lifecycle evidence; they do not establish operational MLOps
or resolve independent target reference/accuracy requirements.

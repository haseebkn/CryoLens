# Portfolio walkthrough

## One-minute introduction

"CryoLens is a Newfoundland and Labrador SAR screening project. I built a
traceable path from satellite measurements to unverified radar candidates and
analyst review. The main engineering work is preserving physical units,
excluding unreliable pixels, measuring coverage and exposing uncertainty. I
cannot currently claim iceberg precision or recall: the public evaluation data
contains sea-ice charts, not acquisition-matched iceberg truth."

## Five-minute demonstration

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

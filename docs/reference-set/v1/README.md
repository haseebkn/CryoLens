# Independent reference pilot v1

Prepared from verified real AI4Arctic files on 5 October 2026. This is a
preparation receipt, **not a completed human reference set or an accuracy report**.
The user will review the first whole-scene pilot personally. No responses have
been accepted, no review-time estimate exists, no consistency sample has been
reviewed, and no independent second reviewer has been obtained.

| Item | Development scene | Held-out scene |
|---|---:|---:|
| Acquisition | 28 April 2018, 09:39:37 UTC | 19 January 2019, 10:12:18 UTC |
| Frozen partition | Validation (development) | Test, reference-only access |
| Nonoverlapping survey cores prepared | 193 | 64 |
| Available survey pixels | 24,731,337 | 7,193,789 |
| Approximate available offshore NL area, km² | 158,280.5568 | 46,040.2496 |
| Raw connected components | 311 | Not computed |
| Retained candidate census | 28 | Not computed |
| Rejected components | 283, all first rejected by `min_size` | Not computed |
| Sampled rejected reviews | 68 | Not computed |
| Candidate review tasks | 96 | 0 |
| Accepted human responses / scored survey pixels | 0 / 0 | 0 / 0 |

Available area is native mask pixel count × nominal 80 m spacing squared.
It is approximate acquisition exposure, not completed surveyed coverage, a
provincial boundary, or a claim of regional sampling representativeness. Survey
cores cover all declared available offshore pixels once, with display context
outside each core. Partial, unusable, excluded and unreviewed areas are unscored.

The development detector ran with the existing Gamma configuration, without
tuning. It has 20,869,250 eligible pixels (approximately 133,563.2 km²); this
mask is separate from the larger independent-survey domain. A raw component
frame and first-rejection trace precede suppression. Random sampling is within
scene × first failed stage × size × peak-HV strata. The largest rejection
stratum has 234 components and a 20-item sample (π = 20/234). Small strata are
censused; no nonempty rejection stratum has zero selection probability.
Retained components have π = 1. See [the actual design](development-sampling.json).

One selected development component has six existing hash-verified radar/optical
contexts. These are the already documented limited-visibility Planetary Computer
and CDSE pairs, not six independent targets or new usable optical confirmations.
Missing or obscured counterparts never establish a non-target or iceberg label.

The held-out source was decoded only for survey imagery, domain masks and
geolocation, with chart context disabled and **no detector execution**. SAR
arrays are now prepared and must no longer be described as wholly unopened.
Its sea-ice chart labels and model evaluation remain sealed. The original
evaluation manifest and acquisition groups are unchanged. The source-bound
[access release](reference-access-release.json) records the user's authority
for this additive annotation-only preparation. It is not a model release.

## Start the pilot

The local held-out review server is available at
[http://127.0.0.1:8012/](http://127.0.0.1:8012/). If it has stopped, restart it:

```text
uv run --frozen python -m cryolens.reference serve --workspace data/processed/reference/pilot-v1/heldout --queue survey --port 8012
```

Enter your own reviewer ID. Inspect HH and HV; search every valid pixel inside
the blue core boundary, including small returns. Record targets as `unknown`
identity unless independently corroborated. Keep uncertainties explicit and
exclude ambiguous regions. Mark a core complete only after personally searching
it. Save each response before navigation, then export the saved JSON.
The browser saves local drafts; export/import is required to accept annotations.

```text
uv run --frozen python -m cryolens.reference ingest --workspace data/processed/reference/pilot-v1/heldout --input <exported-responses.json>
uv run --frozen python -m cryolens.reference report --workspace data/processed/reference/pilot-v1/heldout --output data/processed/reference/heldout-progress.json
```

The development pilot also requires its whole-area survey before its mixed
candidate queue opens. This ordering preserves an independent search instead
of teaching the reviewer where the detector looked. Finish the pilot to measure
annotation time, uncertain coverage and sampling uncertainty before deciding
on broader scene coverage.

After every primary task has a disposition, the workflow supports a random
20% concealed repeat, at least seven days after the latest review. A separate
packet can be issued to a distinct reviewer when one is available. Record
disagreements and adjudicate before freezing the annotations and exact masks.
See the [complete review protocol](../../REFERENCE_SET.md) for commands and
the [adjudication template](../../../configs/reference/adjudication-template.json).

## Reproducibility and claim boundary

- [Development preparation](development-preparation.json) and
  [held-out preparation](heldout-preparation.json) bind source bytes, group,
  policy, workspace and private artifact hashes.
- [Development MLflow receipt](development-tracking.json) and
  [held-out MLflow receipt](heldout-tracking.json) are real provenance runs.
  No model-performance metrics were logged.
- Native imagery, grids, sealed workspaces, code snapshots, task/status mappings
  and response ledgers are stored locally under `data/processed/reference/pilot-v1/`.
  Private mappings and earlier responses are outside the served reviewer folders.
  Git contains preparation summaries, not test imagery or invented annotations.
- The code snapshots capture the generating source, including a dirty working
  tree, before processing. Source hashes are checked again before sealing.
  They preserve what actually executed rather than claiming a future commit ran.

Software and real packet preparation are complete. **Actual annotations,
consistency checks and adjudicated reference freezing remain pending.**
Precision, recall, per-stage recall and empirical false alarms per 1,000 km²
remain unmeasured. SAR inspection may support observable-target metrics after
locked evaluation; iceberg-identification accuracy needs independent evidence.
These two acquisitions cannot establish regional performance or operational
readiness. Fresh SAFE processing remains blocked separately.

Verification: 340 tests passed and one was skipped; lint, formatting and types
passed. The real PostGIS check passed with fixtures rolled back. The built wheel
loads the reference policy and includes the viewer outside the repository. Both
sealed packet file inventories were independently rehashed with no differences.
The development viewer's channel, zoom and navigation controls were inspected
without saving any human responses. Local lint/format exclusions cover two
pre-existing unrelated working-tree files, which are not part of this change;
CI verifies the committed tree without those exclusions.

The final audit also fixes chip centres for non-convex components whose bounding
rectangles contain separate components. A synthetic nested-component regression
test covers that case. The [real development reproduction](development-frame-reproduction.json)
matches all 311 recorded components, including centres and first rejection,
and the full detection summary exactly after the fix. The existing pilot was
not affected; packets and any local human drafts are preserved. No held-out
detector run was needed. Reproduce this check with:

```text
uv run --frozen python scripts/check_reference_frame.py --workspace data/processed/reference/pilot-v1/development --output data/processed/reference/frame-reproduction.json
```

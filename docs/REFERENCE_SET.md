# Independent NL reference set

Protocol `nl-independent-reference-v1` extends the frozen `nl-mda-v1` evaluation
design. It implements preparation, human annotation, sampling, consistency and
reference freezing. **Preparing review packets is not a completed reference
set.** Real annotations and re-review remain necessary; no automatic target or
identity labels are supplied.

The user chose to personally review the first pilot. A second reviewer has not
been obtained. The [pilot receipts](reference-set/v1/README.md) distinguish real
imagery preparation from actual reviewed coverage and measured performance.

## Separate observations and identities

An observable SAR target is a compact localized return judged to represent a
discrete object at the available sensor resolution. A non-target is diffuse
clutter or an imaging/processing artifact. Ambiguity remains `uncertain`.
Do not impose the detector's four-pixel minimum when searching independently.
An observable target can have `unknown` identity.

Ship, iceberg and other identities need positive independently corroborating
optical, AIS or field evidence. Record its source/hash, acquisition time,
observed position, matching radius, position uncertainty and rationale.
The existing six-hour evidence window applies. Optical identity also needs
an inspected clear view, valid optical pixels and stated resolution. Matching
allowances must account for movement and geolocation; they are declarations,
not validated drift forecasts. Missing counterparts, IIP context, SAR appearance
and AIS absence never establish identity. Source/evidence hashes are recorded;
external observations still need analyst provenance verification and adjudication.
Use the [supporting-observation template](../configs/reference/supporting-observation-template.json)
only after supplying actual inspected evidence; its placeholders are intentionally invalid.

The machine-readable policy is
[`configs/reference/protocol-v1.json`](../configs/reference/protocol-v1.json).
Normalized human observations reuse the frozen `Annotation` contract, while
the full supporting-observation declarations are preserved in the reference
ledger. These are analyst-derived observable-target references, not field truth
or a C-CORE protocol.

## Frame and sampling

Existing aggregate suppression statistics cannot reconstruct discarded returns.
The reference producer saves **every raw eight-connected component**, including
single-pixel components, before post-detection suppression. Each record stores
source-bound identity, native pixel bounds/location, size, radiometry, passed
stages and first failed stage. This last reason is conditional on passing prior
stages; a component is not duplicated across rejection populations.

CFAR uses the existing Gamma detector and operating configuration, with 512-pixel
cores and a halo covering its full training window. Core results assemble into
global detection/support arrays **before** connected components are extracted,
so tile boundaries do not fragment targets or create duplicate samples. Regression
fixtures compare CA and Gamma detection/support masks and clutter values against
untiled execution. Large CFAR temporaries are bounded; SAR, geolocation and
assembled output arrays remain scene-sized. Out-of-core ingestion is not claimed.

Retained components are a **census**. Rejected sampling strata are:

```text
scene × first failed stage × component size × peak-HV brightness
size: 1, 2, 3, 4–15, 16+ pixels
brightness: below −30, −30 to below −24, at least −24 dB
```

Seeded simple random sampling without replacement selects up to **20 per
`min_size` stratum** and **8 per other rejection stratum**. Small strata are
censused. Full frame counts, selected counts, seed and **n/N inclusion
probabilities** are preserved privately and in the sampling-design report.
Every nonempty rejection stratum has positive probability; all retained
components have probability one. This prioritizes `min_size` without assigning
zero probability to other failures.

After a stratum has all its selected candidate dispositions complete, reporting
provides inverse-probability weighted target totals and an explicit upper bound
treating uncertain labels as targets. Exact conditional 95% count intervals invert
the hypergeometric sampling tails, using the official
[SciPy hypergeometric implementation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.hypergeom.html).
Zero observed targets in a sample does not imply zero targets in its population.
These per-stratum intervals address finite sampling only: they do not account
for reviewer errors, are not simultaneous across strata, and are not regional
scene/group uncertainty intervals. Nonresponse prevents a stratum estimate;
its planned inclusion probabilities are never replaced by the response fraction.

Weighted sampled-component results concern the raw CFAR frame. They cannot
measure objects that candidate generation or upstream masking missed.

## Independent area search and exact coverage

The first queue contains **all available offshore NL SAR pixels in the selected
scene**, partitioned into nonoverlapping 384-pixel cores with 24 pixels of display
context. Empty cores are omitted. No candidate locations, detector outcomes,
size threshold, chart labels or prior answers appear in this queue. A second
queue mixes retained and sampled rejected components without displaying their
status, sampling stratum or weight. The same reviewer's whole-area dispositions
must be imported before candidate review is served or accepted.

The survey domain requires finite HH/HV, valid incidence and geolocation,
frozen AOI pixel centres, and publisher land-distance zones **greater than 2**.
It uses **no sea-ice, border, seam, CFAR-support or post-detection gate**. Land
and unknown radiometry remain excluded explicitly. This surveys the complete
declared available domain, not every ocean pixel in a metadata footprint.
The exact native masks and interpolated geolocation grids are saved. Nominal
80 m samples are not 80 m physical object resolution; tie-point geolocation is
not independently surveyed positional accuracy.

Inspect both polarizations and search the whole valid core. Mark all supported
observable targets and uncertainties, including isolated small returns. Use
the context to decide ownership of objects near core boundaries and mark each
object once. Shift-drag ambiguous regions to exclude them from scored coverage.
Uncertain points additionally exclude a declared three-pixel neighbourhood.
`partial` and `unusable` dispositions exclude the entire core from scoring;
they are never interpreted as target-free. Unreviewed cores remain unreviewed.

Development detector-eligible masks are stored separately. A frozen snapshot
intersects them with independently completed unambiguous survey coverage.
Held-out survey-only packets do **not** compute detection eligibility: that field
is explicitly unavailable and must be derived by the subsequent locked model
evaluation. An all-false placeholder is not evidence of zero eligible water.
Area is saved pixel count × nominal spacing squared, explicitly approximate
acquisition exposure. Repeat reviews and duplicate derivatives do not add area.

## Held-out reference-only access amendment

The original sealed evaluation manifest and its grouping are unchanged. This
additive protocol separates annotation preparation from locked model execution:
test SAR preparation requires a release binding the parent manifest, reference
policy and exact scene IDs, with `independent_reference_only`, no detector
execution and no chart-label access. The user explicitly requested independent
held-out references; this is the recorded authority for pilot preparation.

An access receipt is written **before** SAR decoding. The January 19, 2019
test source is now decoded solely to prepare human survey images, masks and
geolocation, using `load_context=False`. Its SAR arrays therefore must no longer
be described as wholly unopened. Its SIC/SOD/FLOE labels and model evaluation
remain sealed. No automated target judgments, test CFAR run, final model
evaluation or tuning on this packet are performed. Preparation is not a human
survey and supplies no scored coverage until responses exist.

The modeller must keep test review imagery/annotations out of training and
tuning. The controller should provide the dedicated survey folder to the
reviewer, keeping private mappings and labels separate. A single workstation
and a self-attested role separation are not enforced organizational independence.
If test imagery or labels inform tuning, record that exposure and replace the
test cohort rather than retaining an untouched/generalization claim. The usual
model/configuration-bound test release remains mandatory for model execution.
Fresh SAFE processing remains blocked; no rejected SAFE imagery is released
through this reference producer.

## Human workflow

Prepare the real pilots and real MLflow provenance runs:

```text
uv run --frozen python scripts/prepare_reference_pilot.py
```

Outputs are in `data/processed/reference/pilot-v1/`. Existing workspaces are not
overwritten. `--reuse-verified` reuses verified packets without rewriting human
responses; a new policy/selection needs a new output version. Raw imagery,
private grids, response ledger and test imagery remain outside Git.

Start the held-out survey viewer:

```text
uv run --frozen python -m cryolens.reference serve --workspace data/processed/reference/pilot-v1/heldout --queue survey --port 8012
```

Open `http://127.0.0.1:8012/`. Enter your own reviewer ID, inspect HH and HV,
zoom without inferring finer measurements, mark targets/uncertainties and record
inspection notes. A complete response requires explicit personal-inspection
attestation and both viewed channels. Save each response before changing tasks;
export saved responses before closing. No browser response is sent to a server.
Local drafts remain separate from validated accepted annotations.

Import the exported JSON and measure progress/effort:

```text
uv run --frozen python -m cryolens.reference ingest --workspace data/processed/reference/pilot-v1/heldout --input <exported-responses.json>
uv run --frozen python -m cryolens.reference report --workspace data/processed/reference/pilot-v1/heldout --output data/processed/reference/heldout-progress.json
```

The importer validates source/task association, exact core/mask membership,
UTC times, reviewer attestation, separate task labels, evidence and identity
matching. Hash-verified artifacts are required. Imports are atomic and
idempotent; conflicting replacements fail. The SQLite event history rejects
updates/deletes and is hash-chained. It records local self-attested reviewer
identity, **not authenticated professional field truth**. A local administrator
can change files or replace the database; it is an audit trail, not a security
boundary. The existing API's authenticated analyst history is unchanged.

For the development pilot, first serve/import its `survey` queue. Then:

```text
uv run --frozen python -m cryolens.reference serve --workspace data/processed/reference/pilot-v1/development --queue candidates --reviewer <your-reviewer-id> --port 8013
```

Relevant hash-verified existing Sentinel-2 pairs appear as supporting context,
showing source/version, time separation, visibility and movement-envelope
limitations. Inspect suitability; no optical counterpart absence becomes a
non-target or an identity label. Current optical coverage is limited.

The viewer serves only its dedicated reviewer folder on loopback. Private
sampling/status mappings and prior answers are outside that directory. It has
no public write API. To obtain another reviewer, issue a dedicated packet and
give that reviewer its folder; do not send the primary responses with it.

## Blinded consistency and adjudication

After all primary pilot tasks have a disposition, wait at least **seven days
after the latest recorded review**. Issue a random **20%** repeat sample:

```text
uv run --frozen python -m cryolens.reference issue --workspace <workspace> --original-reviewer <your-id> --reviewer <your-id> --mode repeat
```

The command refuses early repeats. Fresh opaque IDs, renamed assets and shuffled
order conceal earlier answers and candidate status; recognizing an image remains
possible. Private mappings retain base IDs and repeat n/N. Serve the returned
phase ID as `--queue`, using the assigned reviewer, then import its export.
Review timing and repeated coverage do not enter the original area denominator.

When a colleague or qualified independent reviewer is available:

```text
uv run --frozen python -m cryolens.reference issue --workspace <workspace> --original-reviewer <your-id> --reviewer <independent-id> --mode independent --fraction 1
```

This uses a distinct reviewer and a packet without prior answers. Report
within-reviewer and between-reviewer agreement separately. Candidate reporting
includes the target/non-target/uncertain confusion matrix, exact agreement and
Cohen's kappa (undefined cases remain null). Survey comparisons report status
and one-to-one target correspondence at the frozen 200 m tolerance. None is
an accuracy score against physical truth. Without another reviewer, explicitly
report that inter-rater agreement is unmeasured.

Resolve differences explicitly before freezing. The adjudication JSON requires
adjudicator ID/time, uncertainty notes, reviewed object ownership across core
boundaries, actual independent-reviewer status and a retained response key with
rationale for every differing task. It never silently averages labels. See
[the template](../configs/reference/adjudication-template.json).

```text
uv run --frozen python -m cryolens.reference freeze --workspace <workspace> --reviewer <primary-id> --adjudication <completed-adjudication.json> --output <workspace>/frozen-v1
```

Freezing refuses missing primary dispositions, candidate nonresponse, missing
consistency responses, unresolved conflicts or zero completed unambiguous
survey coverage. It saves the selected annotations, full evidence declarations,
adjudication, exact coverage masks and a sealed content manifest. This completes
one declared reference pilot, not regional validation or iceberg truth.
New corrections belong in a newly documented review/adjudication version;
accepted history is never overwritten.

## Expansion and remaining evidence

Finish the initial whole-scene pilot to measure active viewer time, unresolved
target fraction, unusable/uncertain area and the width of sampled rejection
intervals. The timer excludes hidden-tab time and work in other applications;
it is a self-recorded planning measure, not independently verified labour.
Use the observed task mix and mean/range to estimate additional annotation
effort; never invent timing before reviews exist. Freeze broader scene choices
and sampling budgets in a new design version before examining their outcomes.
Do not handpick additional rejects and retain old inclusion probabilities.

Expand acquisition groups, seasons and NL shelf subregions, especially where
current coverage is thin. A wider independent reviewer effort and usable paired
optical observations remain valuable. One winter held-out acquisition and one
previously explored development acquisition cannot establish a regional miss
rate, empirical false-alarm budget or operational readiness.

Precision, recall, per-stage recall and false detections per 1,000 km² await
actual frozen references and locked full-chain evaluation. SAR review alone may
support observable-target detection metrics while **iceberg-identification
accuracy remains unestablished**.

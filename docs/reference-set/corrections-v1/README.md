# Pilot correction integration

This change connects validated correction proposals to progress, concealed repeat
selection, reviewer agreement and adjudicated reference freezing. It is not a
completed reference set or a model-accuracy evaluation.

The real source package is SHA256
`d7fc3ea8875edcc4e522649fa36701d4e3a60f7ebd72049697d32677cdb05a80`.
A dry run validated all five corrections against the existing sealed held-out
workspace without changing its 64 initial responses. Native imagery, human
exports and the detailed correction package remain local and outside Git.

| Tile | Corrected observation |
|---|---|
| 3 | Observable SAR target, identity unknown; native point 368, 295 |
| 24 | Uncertain object, identity unknown; position estimated from the marked screenshot |
| 25 | User reports waves and no object |
| 27 | User reports waves and no object |
| 53 | User reports waves and no object |

The user explicitly confirmed inspecting HH/HV and the whole valid core for
24, 25, 27 and 53. Those recheck durations are unmeasured, rather than fabricated
or converted to zero. The manual confirmation's registration time is retained
with that time-basis limitation. Tile 24 retains a two-pixel screenshot
localization allowance in addition to the three-pixel protocol uncertainty
buffer. Its 11 by 11 exclusion removes 121 otherwise scored pixels.

The proposed corrected pilot has 6,990,611 unambiguous pixels, approximately
44,739.9104 square kilometres using nominal 80 m sample spacing. This is
acquisition exposure, not independent physical truth, regional performance or
an iceberg-identification claim. Tiles 1 (unusable) and 4 (partial) stay unscored.

Use the [documented correction command](../../REFERENCE_SET.md#additive-corrections-before-consistency-review)
with a receipt-bound package. Successful adoption appends five amendments;
progress still contains 64 effective primary tasks, not 69 independent reviews.
Repeat selection then waits seven days after the latest corrected confirmation
under the linked protocol. The current conservative
not-before time from the package is 20 October 2026 at 22:50:06 Newfoundland
time. It must be recomputed if genuine further reviews occur.

No actual held-out repeat, final freeze, CFAR run or model evaluation was performed
by this implementation. Final adoption and its dated verification receipt are a
separate execution after software validation; the initial dry run must not be
represented as an already applied correction.

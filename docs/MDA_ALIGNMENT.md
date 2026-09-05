# Maritime domain awareness alignment

Reviewed 2026-09-05. This is a requirements traceability note, not a compliance
certificate. No C-CORE internal operating procedure, acceptance threshold or
contractual interface specification was provided. The implementation is an
independent research demonstration.

## Public basis

[C-CORE maritime surveillance](https://c-core.ca/solutions/maritime-surveillance/)
describes satellite target detection, ship/iceberg discrimination, analyst
quality control, visualization and reporting. CryoLens follows the separation
between detecting a radar return and deciding its identity. It does not claim
to implement Coresight or C-CORE's proprietary algorithms.

[Transport Canada's MDA description](https://tc.canada.ca/en/marine-transportation/marine-security/maritime-domain-awareness)
emphasizes timely, accurate information and shared situational awareness.
Historical acquisitions are therefore presented with their observation times;
processing a historical image today does not turn it into current surveillance.

[MANICE chapter 4](https://www.canada.ca/en/environment-climate-change/services/weather-manuals-documentation/manice-manual-of-ice/chapter-4.html)
separates source/method, observation time, position, sea-ice context and
measured versus estimated dimensions. CryoLens uses UTC observation metadata
and preserves unknowns. Its radar component dimensions are image extents,
not measured iceberg dimensions. It does not generate MANICE messages or map
an uncalibrated CFAR score onto MANICE satellite confidence codes.

The [2025 International Ice Patrol report](https://www.navcen.uscg.gov/sites/default/files/pdf/iip/Report%20of%20the%20International%20Ice%20Patrol%20in%20the%20North%20Atlantic%202025.pdf)
describes AIS deconfliction of potential iceberg targets. This motivates an
explicit `not_checked` state when AIS is unavailable. Lack of an AIS match is
not evidence that a radar target is an iceberg or an illicit vessel.

## Traceability

| Concern | Implemented response | Remaining evidence or integration |
|---|---|---|
| Geographic scope | Shared NL study polygon; pixel and API containment | Official customer AOI if required; this is a study boundary |
| Radiometric integrity | Real SAFE measurements; validated metadata; no invented noise or successful mock products | Real SAFE calibration/geolocation cross-check against trusted processing |
| Target identity | CFAR candidates unclassified; score semantics explicit | Independent ship/iceberg labels, calibrated classifier, AIS and structure deconfliction |
| False alarms | Masks, contextual gating, component filtering, per-stage removal ledger | Representative confirmed positives/negatives and missed-target survey |
| Observation context | Source scene/time, processing settings, unknown context exposed | Source latency and coverage service-level requirements |
| Human quality control | Protected, attributed analyst review history | Operational multi-user authentication and independent adjudication |
| Interchange | WGS84 GeoJSON; source metadata preserved | Customer schema conformance tests; no MANICE/STAC compliance claim |
| Forecast integrity | Disabled without verified forcing/model validation | Real forcing, bathymetry, physical parameters and trajectory error assessment |
| Reproducibility | Locked dependencies, executable checks, versioned evidence | Independent reruns on broader seasons and Grand Banks coverage |

## Acceptance before any operational use

A qualified domain partner would need to agree on target definitions, size
classes, required area/time coverage, geolocation tolerance and an explicit
false-alarm budget alongside minimum recall. Evaluation must be separated by
scene/acquisition, season, sea-ice context and geography. Record uncertain
labels rather than forcing them into positive/negative classes. Count candidate
false alarms only after adjudication, and report uncertainty and missed-target
risk alongside the count. Those acceptance conditions are not yet satisfied.

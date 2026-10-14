# Pinned public EGM96 auxiliary grid

`ww15mgh_b.zip` was downloaded from the ESA SNAP auxiliary-data service on
12 October 2026:
[original archive](https://step.esa.int/auxdata/dem/egm96/ww15mgh_b.zip).

Measured archive SHA256:
`b5db7a14c3062decb8daaaf60f257ff374084be79aeda9ce333dd5b9c815751c`.
This is a recorded retrieval checksum, not a separately signed publisher checksum.
The loader verifies it before decoding the 721 × 1441, 15-minute global grid.
No extraction, credentials or runtime download is required. The built package
contains the same auxiliary archive.

The grid supplies geoid undulation for the marine mean-sea-surface geometry
experiment. It does not supply a terrain DEM, tidal correction, precise orbit,
or independent ground-control measurement. Interpolation and all height/timing
assumptions are recorded in [the processing report](../../../../docs/processing-validation/v2/README.md).

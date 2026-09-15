"""Pin the AI4Arctic standardisation constants to the publisher's own file.

Every sigma-nought value CryoLens produces from the AI4Arctic distribution is
``stored * std + mean`` using six numbers transcribed from the publisher's
``misc/global_meanstd.npy``. Those numbers were previously recorded only as a
source comment and a provenance string, so a transcription error or a
well-meaning edit would have silently rescaled the entire physical basis of the
project with nothing to catch it.

That failure mode is not hypothetical here: an earlier revision inferred the
standardisation from each scene's observed extrema instead, which put the
cross-pol scale 33 percent out and the open-water median 4.2 dB wrong, and the
resulting benchmark had to be withdrawn.

The publisher file is vendored (2 KB) so this check is hermetic and runs in CI
without network access. Its pickle is **never executed** — the constants are
recovered by disassembling the opcode stream, because loading it would require
``allow_pickle=True`` on a third-party binary.

Source: https://github.com/astokholm/AI4ArcticSeaIceChallenge/blob/main/misc/global_meanstd.npy
"""

from __future__ import annotations

import ast
import hashlib
import io
import pickletools
import re
import struct
from pathlib import Path

import pytest

from cryolens.data.ai4arctic import PUBLISHER_MEAN_STD

CONSTANTS_FILE = Path(__file__).resolve().parents[1] / "data" / "global_meanstd.npy"

EXPECTED_SHA256 = "2f3f9188617a98c9e89158302efad8ffbc22ecc9d66d76019b54cdb5cbcfdeca"

_NAME_RE = re.compile(r"BINUNICODE\s+('[^']*')")
_BYTES_RE = re.compile(r"SHORT_BINBYTES\s+(b'.*')")

# Structural tokens emitted by numpy's dtype and scalar machinery rather than
# variable names. The byte-order marker appears only once, for the first
# variable, because numpy memoises the dtype afterwards; omitting it from this
# set silently drops ``nersc_sar_primary``.
_STRUCTURAL_TOKENS = {"O8", "|", "<", ">", "=", "mean", "std", "f8", "b"}


def _extract_constants(path: Path) -> dict[str, tuple[float, float]]:
    """Recover ``{variable: (mean, std)}`` without executing the pickle."""
    raw = path.read_bytes()
    listing = io.StringIO()
    pickletools.dis(raw[raw.index(b"\x80") :], out=listing)

    stream: list[tuple[str, object]] = []
    for line in listing.getvalue().splitlines():
        name_match = _NAME_RE.search(line)
        if name_match:
            stream.append(("name", ast.literal_eval(name_match.group(1))))
            continue
        bytes_match = _BYTES_RE.search(line)
        if bytes_match:
            payload = ast.literal_eval(bytes_match.group(1))
            if len(payload) == 8:
                stream.append(("value", struct.unpack("<d", payload)[0]))

    constants: dict[str, tuple[float, float]] = {}
    current: str | None = None
    pending: list[float] = []
    for kind, item in stream:
        if kind == "name":
            if item in _STRUCTURAL_TOKENS:
                continue
            if current is not None and len(pending) == 2:
                constants[current] = (pending[0], pending[1])
            current = str(item)
            pending = []
        elif current is not None:
            pending.append(float(item))  # type: ignore[arg-type]
    if current is not None and len(pending) == 2:
        constants[current] = (pending[0], pending[1])
    return constants


@pytest.fixture(scope="module")
def published_constants() -> dict[str, tuple[float, float]]:
    """Constants parsed from the vendored publisher file."""
    return _extract_constants(CONSTANTS_FILE)


class TestPublisherFile:
    """The vendored artefact must be the one the provenance string names."""

    def test_file_is_present(self) -> None:
        assert CONSTANTS_FILE.is_file(), f"vendored constants missing at {CONSTANTS_FILE}"

    def test_checksum_matches_recorded_provenance(self) -> None:
        digest = hashlib.sha256(CONSTANTS_FILE.read_bytes()).hexdigest()
        assert digest == EXPECTED_SHA256

    def test_provenance_string_in_reader_matches(self) -> None:
        """The digest quoted in the reader must be the one actually vendored."""
        source = Path("src/cryolens/data/ai4arctic.py").read_text(encoding="utf-8")
        assert EXPECTED_SHA256 in source

    def test_parser_recovers_every_variable(
        self, published_constants: dict[str, tuple[float, float]]
    ) -> None:
        """A silent parser regression must not masquerade as agreement."""
        assert len(published_constants) == 24
        assert "nersc_sar_primary" in published_constants


class TestConstantsAgreeWithPublisher:
    """Each hardcoded constant must equal the publisher's value exactly."""

    @pytest.mark.parametrize("variable", sorted(PUBLISHER_MEAN_STD))
    def test_constant_matches(
        self, variable: str, published_constants: dict[str, tuple[float, float]]
    ) -> None:
        assert variable in published_constants, f"{variable} absent from publisher file"
        expected_mean, expected_std = published_constants[variable]
        mean, std = PUBLISHER_MEAN_STD[variable]
        # Exact equality, not approx. Both sides originate from the same float64
        # bit pattern, so any tolerance at all would admit a transcription error:
        # an approx(abs=1e-12) version of this test passed a deliberately
        # corrupted constant, which is precisely the drift it exists to catch.
        assert mean == expected_mean, f"{variable} mean drifted from the publisher value"
        assert std == expected_std, f"{variable} std drifted from the publisher value"

    def test_no_extra_variables_claimed(
        self, published_constants: dict[str, tuple[float, float]]
    ) -> None:
        """The reader must not invent constants the publisher never supplied."""
        unknown = set(PUBLISHER_MEAN_STD) - set(published_constants)
        assert not unknown, f"constants not present in publisher file: {sorted(unknown)}"

    def test_standard_deviations_are_positive(self) -> None:
        """A non-positive scale would invert or collapse the decibel axis."""
        for variable, (_, std) in PUBLISHER_MEAN_STD.items():
            assert std > 0.0, f"{variable} has a non-positive standard deviation"


class TestRestoredPhysicalRange:
    """Restored values must land in the decibel regime real S1 EW data occupies."""

    def test_cross_pol_zero_maps_to_plausible_backscatter(self) -> None:
        """A standardised value of 0 is the global mean cross-pol backscatter.

        Sentinel-1 EW HV over this region spans roughly -35 to -10 dB across
        open water and sea ice, so the global mean must sit inside that band.
        """
        mean, _ = PUBLISHER_MEAN_STD["nersc_sar_secondary"]
        assert -35.0 < mean < -10.0

    def test_co_pol_mean_exceeds_cross_pol_mean(self) -> None:
        """Co-polarised backscatter is stronger than cross-polarised."""
        hh_mean, _ = PUBLISHER_MEAN_STD["nersc_sar_primary"]
        hv_mean, _ = PUBLISHER_MEAN_STD["nersc_sar_secondary"]
        assert hh_mean > hv_mean

    def test_incidence_angle_mean_within_ew_swath(self) -> None:
        """The EW swath spans about 19 to 47 degrees; its mean must lie inside."""
        mean, _ = PUBLISHER_MEAN_STD["sar_incidenceangle"]
        assert 19.0 < mean < 47.0

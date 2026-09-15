"""Ambiguity must not turn one sighting into many confirmations."""

import pytest

from cryolens.eval.correlate import SpatiotemporalMatcher, unambiguous_pairs


def test_many_candidates_near_one_sighting_are_ambiguous() -> None:
    assert unambiguous_pairs([("a", "iip1"), ("b", "iip1")]) == set()


def test_many_sightings_near_one_candidate_are_ambiguous() -> None:
    assert unambiguous_pairs([("a", "iip1"), ("a", "iip2")]) == set()


def test_independent_unique_associations_are_retained() -> None:
    assert unambiguous_pairs([("a", "iip1"), ("b", "iip2")]) == {("a", "iip1"), ("b", "iip2")}


@pytest.mark.parametrize("speed", [-0.1, float("nan"), float("inf")])
def test_invalid_drift_speed_rejected(speed: float) -> None:
    with pytest.raises(ValueError):
        SpatiotemporalMatcher(speed)

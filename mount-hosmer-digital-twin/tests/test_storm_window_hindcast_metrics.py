from __future__ import annotations

import numpy as np
import pytest

from avycore.validation import (
    mountain_block_bootstrap_interval,
    storm_window_positive_metrics,
)


def test_storm_window_metrics_keep_incomplete_events_as_failures() -> None:
    eligible = np.ones((4, 5), dtype=bool)
    eligible[2, 2] = False
    predicted = np.zeros_like(eligible)
    predicted[0, :3] = True
    predicted[2, 2] = True

    event_a = np.zeros_like(eligible)
    event_a[0, :4] = True
    event_b = np.zeros_like(eligible)
    event_b[2, 1:4] = True
    event_c = np.zeros_like(eligible)
    event_c[3, :2] = True

    metrics = storm_window_positive_metrics(
        predicted,
        eligible=eligible,
        event_masks=(event_a, event_b, event_c),
        event_ids=("complete-captured", "missing-input", "boundary-crossing"),
        geometry_complete=(True, True, False),
        capture_minimum_overlap_fraction=0.5,
        cell_area_m2=900.0,
    )

    assert metrics.negative_evidence_used is False
    assert metrics.unmapped_cells_treated_as_negative is False
    assert metrics.event_count == 3
    assert metrics.captured_event_count == 1
    assert metrics.incomplete_input_event_count == 1
    assert metrics.incomplete_geometry_event_count == 1
    assert metrics.event_capture_fraction == pytest.approx(1 / 3)
    assert metrics.predicted_outside_eligible_cell_count == 1
    assert metrics.event_scores[0].captured is True
    assert metrics.event_scores[1].captured is False
    assert metrics.event_scores[2].captured is False
    assert not hasattr(metrics, "precision")
    assert not hasattr(metrics, "intersection_over_union")


def test_storm_window_metrics_reject_empty_or_misaligned_evidence() -> None:
    mask = np.ones((2, 2), dtype=bool)
    with pytest.raises(ValueError, match="at least one"):
        storm_window_positive_metrics(
            mask,
            eligible=mask,
            event_masks=(),
            event_ids=(),
            geometry_complete=(),
            capture_minimum_overlap_fraction=0.1,
            cell_area_m2=1.0,
        )
    with pytest.raises(ValueError, match="match the prediction"):
        storm_window_positive_metrics(
            mask,
            eligible=mask,
            event_masks=(np.ones((3, 3), dtype=bool),),
            event_ids=("event",),
            geometry_complete=(True,),
            capture_minimum_overlap_fraction=0.1,
            cell_area_m2=1.0,
        )


def test_mountain_block_bootstrap_is_seeded_and_resamples_blocks() -> None:
    first = mountain_block_bootstrap_interval(
        [7, 8, 3, 9],
        [10, 10, 10, 10],
        replicate_count=2_000,
        random_seed=41,
    )
    second = mountain_block_bootstrap_interval(
        [7, 8, 3, 9],
        [10, 10, 10, 10],
        replicate_count=2_000,
        random_seed=41,
    )

    assert first == second
    assert first.estimate == pytest.approx(27 / 40)
    assert first.lower <= first.estimate <= first.upper
    assert first.resampling_unit == "mountain_block"


def test_mountain_block_bootstrap_rejects_invalid_ratios() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        mountain_block_bootstrap_interval([2], [1])

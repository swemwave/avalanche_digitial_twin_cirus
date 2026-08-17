from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "build_blinded_observation_packets.py"
)
SPEC = importlib.util.spec_from_file_location("build_blinded_observation_packets", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_blind_audit_rejects_evaluated_content() -> None:
    base = {"schema": MODULE.PACKET_SCHEMA, "packet_id": "blind-test"}
    MODULE._assert_blind(base)
    for value in (
        "model layer",
        "prediction",
        "simulation",
        "alpha line",
        "parameter result",
        "hazard score",
        "runout result",
    ):
        with pytest.raises(ValueError, match="forbidden content"):
            MODULE._assert_blind({**base, "leak": value})


def test_blind_audit_rejects_wrong_packet_schema() -> None:
    with pytest.raises(ValueError, match="schema"):
        MODULE._assert_blind({"schema": "wrong", "packet_id": "blind-test"})


def test_immutable_packet_never_overwrites_different_bytes(tmp_path: Path) -> None:
    path = tmp_path / "packet.json"
    MODULE._write_immutable(path, b"one")
    MODULE._write_immutable(path, b"one")
    with pytest.raises(ValueError, match="Immutable blinded-packet conflict"):
        MODULE._write_immutable(path, b"two")


def test_blank_forms_require_every_explicit_mask_and_cannot_claim_human_work() -> None:
    form = MODULE._blank_review_form("blind-123", "a" * 64, "A")
    assert form["ai_generated_only"] is None
    assert form["human_completed"] is None
    assert form["peer_submission_accessed"] is None
    assert form["release_density_transferability"]["transfer_uncertainty_kg_m3"] is None
    for component in form["components"]:
        assert set(component["observation_masks"]) == {
            "missing_data",
            "scene_edge",
            "detection_exclusion",
            "survey_coverage",
            "cloud",
            "cloud_shadow",
            "shadow",
            "forest",
            "water",
            "layover",
            "radar_shadow",
            "prior_deposit",
        }
        assert all(
            record["status"] is None
            for record in component["observation_masks"].values()
        )


def test_deterministic_zip_has_fixed_bytes() -> None:
    entries = [("b.txt", b"two"), ("a.txt", b"one")]
    assert MODULE._zip_bytes(entries) == MODULE._zip_bytes(reversed(entries))

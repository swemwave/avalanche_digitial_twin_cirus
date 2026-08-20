"""Offline terrain-bake, condition replay, baseline, and validation CLI.

Everything else the app does at runtime is served from the bake, so the only
offline step is the bake itself. See ``app/bake.py``.

    python -m app.cli bake            # build runtime/baked/ (tiles + .npy + meta)
    python -m app.cli bake --force    # rebuild even if a bake already exists
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


def bake_command(args: argparse.Namespace) -> int:
    from app.bake import bake
    from app.core.settings import get_settings

    settings = get_settings()
    updates: dict[str, Path] = {}
    if args.pack is not None:
        updates["mountain_pack_path"] = args.pack.resolve()
    if args.data_root is not None:
        updates["data_root"] = args.data_root.resolve()
    if args.runtime_root is not None:
        updates["runtime_root"] = args.runtime_root.resolve()
    if updates:
        settings = settings.model_copy(update=updates)
    settings.validate(require_data_root=True)
    bake(settings, force=args.force)
    return 0


def compare_baseline_command(args: argparse.Namespace) -> int:
    from app.baseline import compare_engines

    expectations = None if args.no_expectations else Path(args.expectations)
    report = compare_engines(
        args.baseline_engine,
        args.candidate_engine,
        seed=args.seed,
        expectations_path=expectations,
    )
    rendered = json.dumps(report, indent=2)
    if args.output == "-":
        print(rendered)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(f"[baseline] wrote comparison report to {output}")
    return 0 if report["all_checked_results_match"] else 1


def validate_condition_pack_command(args: argparse.Namespace) -> int:
    from app.processing.conditions.storage import (
        ConditionPackStorageError,
        load_condition_pack,
    )

    try:
        pack = load_condition_pack(args.path)
    except ConditionPackStorageError as exc:
        print(f"[condition-pack] invalid: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "valid": True,
                "schema_version": pack.schema_version,
                "condition_id": pack.condition_id,
                "normalized_output_sha256": pack.normalized_output_sha256,
                "valid_start_utc": pack.times.valid_start_utc.isoformat(),
                "valid_end_utc": pack.times.valid_end_utc.isoformat(),
            },
            sort_keys=True,
        )
    )
    return 0


def prepare_era5_land_request_command(args: argparse.Namespace) -> int:
    """Freeze a credential-free full-product request before any CDS retrieval."""

    from datetime import datetime, timedelta

    from app.processing.conditions.era5_land import (
        audit_cds_access,
        build_monthly_request_manifest,
        canonical_request_manifest_bytes,
        write_request_manifest,
    )

    def utc_hour(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() != timedelta(0)
            or parsed.minute
            or parsed.second
            or parsed.microsecond
        ):
            raise ValueError("ERA5 request bounds must be exact UTC hours.")
        return parsed

    manifest = build_monthly_request_manifest(
        utc_hour(args.start), utc_hour(args.end), area=tuple(args.area)
    )
    target = write_request_manifest(manifest, args.runtime_root)
    audit = audit_cds_access()
    print(
        json.dumps(
            {
                "request_id": target.name,
                "request_path": str(target),
                "request_manifest_sha256": hashlib.sha256(
                    canonical_request_manifest_bytes(manifest)
                ).hexdigest(),
                "cds_config_present": audit.locally_configured,
                "cdsapi_installed": audit.cdsapi_installed,
                "retrieval_performed": False,
            },
            sort_keys=True,
        )
    )
    return 0


def verify_snowpack_example_command(args: argparse.Namespace) -> int:
    from app.processing.snow.official_example import verify_official_example

    target = verify_official_example(
        args.executable,
        args.source_root,
        args.runtime_root,
        timeout_seconds=args.timeout_seconds,
    )
    report = json.loads((target / "report.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "verification_id": report["verification_id"],
                "verification_path": str(target),
                "result": report["result"],
                "executable_sha256": report["executable"]["sha256"],
                "output_smet_sha256": report["parsed_smet"]["raw_sha256"],
                "normalized_smet_sha256": report["parsed_smet"]["normalized_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


def _reject_condition_path_in_data(path: Path, data_root: Path, role: str) -> None:
    resolved = path.resolve()
    source_root = data_root.resolve()
    if resolved == source_root or resolved.is_relative_to(source_root):
        raise ValueError(
            f"{role} ({resolved}) must not be inside the read-only DATA root ({source_root})."
        )


def replay_eccc_conditions_command(args: argparse.Namespace) -> int:
    """Import, normalize, replay-check, and persist one offline ECCC snapshot."""

    from datetime import datetime, timezone

    from avycore.conditions import canonical_condition_pack_bytes

    from app.processing.conditions.eccc import (
        ECCCHistoricalProvider,
        import_eccc_snapshot,
        load_eccc_snapshot,
        mountain_grid_from_pack,
        write_quality_report,
    )
    from app.processing.conditions.protocol import ConditionRequest, replay_provider
    from app.processing.conditions.storage import write_condition_pack
    from app.core.settings import get_settings

    def utc_hour(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError(f"Expected UTC timestamp, got {value!r}")
        parsed = parsed.astimezone(timezone.utc)
        if parsed.minute or parsed.second or parsed.microsecond:
            raise ValueError(f"Expected exact UTC hour, got {value!r}")
        return parsed

    runtime_root = Path(args.runtime_root).resolve()
    data_root = get_settings().data_root
    _reject_condition_path_in_data(runtime_root, data_root, "Condition output root")
    uses_snapshot = args.snapshot is not None
    uses_source_files = args.stations is not None or args.hourly is not None
    if uses_snapshot and uses_source_files:
        raise ValueError("Use --snapshot or --stations/--hourly, not both.")
    if not uses_snapshot and (args.stations is None or args.hourly is None):
        raise ValueError("Provide --snapshot or both --stations and --hourly.")
    if uses_snapshot:
        source_root = Path(args.snapshot).resolve()
        _reject_condition_path_in_data(source_root, data_root, "ECCC source snapshot")
        snapshot = load_eccc_snapshot(source_root)
    else:
        stations_path = Path(args.stations).resolve()
        hourly_path = Path(args.hourly).resolve()
        _reject_condition_path_in_data(stations_path, data_root, "ECCC station source")
        _reject_condition_path_in_data(hourly_path, data_root, "ECCC hourly source")
        source_root = import_eccc_snapshot(stations_path, hourly_path, runtime_root)
        snapshot = load_eccc_snapshot(source_root)
    mountain_pack_path = Path(args.mountain_pack).resolve()
    mountain_pack = json.loads(mountain_pack_path.read_text(encoding="utf-8"))
    target_lon, target_lat = (float(value) for value in mountain_pack["center_wgs84"])
    request = ConditionRequest(
        mountain_grid=mountain_grid_from_pack(mountain_pack_path),
        valid_start_utc=utc_hour(args.start),
        valid_end_utc=utc_hour(args.end),
    )
    provider = ECCCHistoricalProvider(
        snapshot,
        target_longitude_deg=target_lon,
        target_latitude_deg=target_lat,
        target_elevation_m=args.target_elevation_m,
        selected_station_id=args.station_id,
    )
    quality = provider.quality_report(request)
    report_stem = (
        f"{snapshot.snapshot_id}-{request.valid_start_utc:%Y%m%d%H}-"
        f"{request.valid_end_utc:%Y%m%d%H}"
    )
    quality_sha256 = hashlib.sha256(
        (
            json.dumps(
                quality,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    report_name = f"{report_stem}-{quality_sha256}.json"
    quality_path = write_quality_report(
        quality, runtime_root / "reports" / "conditions" / "eccc" / report_name
    )
    first = replay_provider(provider, request)
    replay = replay_provider(provider, request)
    replay_identical = canonical_condition_pack_bytes(first) == canonical_condition_pack_bytes(
        replay
    )
    if not replay_identical:
        raise RuntimeError("ECCC normalization failed byte-for-byte deterministic replay.")
    pack_path = write_condition_pack(first, runtime_root)
    print(
        json.dumps(
            {
                "condition_id": first.condition_id,
                "normalized_output_sha256": first.normalized_output_sha256,
                "condition_pack_path": str(pack_path),
                "source_snapshot_id": snapshot.snapshot_id,
                "source_snapshot_path": str(source_root),
                "source_files": snapshot.manifest["files"],
                "quality_report_path": str(quality_path),
                "selected_station_id": quality["selected_station_id"],
                "recommended_station_id": quality["recommended_station_id"],
                "withheld_station_id": quality["withheld_station_id"],
                "deterministic_replay_identical": replay_identical,
            },
            sort_keys=True,
        )
    )
    return 0


def replay_pcic_conditions_command(args: argparse.Namespace) -> int:
    """Acquire or replay the selected immutable PCIC snapshot offline."""

    from datetime import datetime, timezone

    from avycore.conditions import canonical_condition_pack_bytes

    from app.core.settings import get_settings
    from app.processing.conditions.eccc import mountain_grid_from_pack
    from app.processing.conditions.pcic import (
        PCICStationProvider,
        acquire_pcic_snapshot,
        compare_pcic_to_eccc,
        load_pcic_snapshot,
        write_json_report,
    )
    from app.processing.conditions.protocol import ConditionRequest, replay_provider
    from app.processing.conditions.storage import load_condition_pack, write_condition_pack

    def utc_hour(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError(f"Expected timezone-aware UTC timestamp, got {value!r}")
        parsed = parsed.astimezone(timezone.utc)
        if parsed.minute or parsed.second or parsed.microsecond:
            raise ValueError(f"Expected exact UTC hour, got {value!r}")
        return parsed

    runtime_root = Path(args.runtime_root).resolve()
    data_root = get_settings().data_root
    _reject_condition_path_in_data(runtime_root, data_root, "Condition output root")
    if args.acquire and args.snapshot is not None:
        raise ValueError("Use --acquire or --snapshot, not both.")
    if not args.acquire and args.snapshot is None:
        raise ValueError("Provide --snapshot for cache-native replay or use --acquire once.")
    if args.acquire:
        source_root = acquire_pcic_snapshot(runtime_root)
    else:
        source_root = Path(args.snapshot).resolve()
        _reject_condition_path_in_data(source_root, data_root, "PCIC source snapshot")
    snapshot = load_pcic_snapshot(source_root)

    mountain_pack_path = Path(args.mountain_pack).resolve()
    mountain_pack = json.loads(mountain_pack_path.read_text(encoding="utf-8"))
    target_lon, target_lat = (float(value) for value in mountain_pack["center_wgs84"])
    request = ConditionRequest(
        mountain_grid=mountain_grid_from_pack(mountain_pack_path),
        valid_start_utc=utc_hour(args.start),
        valid_end_utc=utc_hour(args.end),
    )
    provider = PCICStationProvider(
        snapshot,
        target_longitude_deg=target_lon,
        target_latitude_deg=target_lat,
        target_elevation_m=args.target_elevation_m,
    )
    first = replay_provider(provider, request)
    replay = replay_provider(provider, request)
    replay_identical = canonical_condition_pack_bytes(first) == canonical_condition_pack_bytes(
        replay
    )
    if not replay_identical:
        raise RuntimeError("PCIC normalization failed byte-for-byte deterministic replay.")
    pack_path = write_condition_pack(first, runtime_root)

    report_stem = (
        f"{snapshot.snapshot_id}-{request.valid_start_utc:%Y%m%d%H}-"
        f"{request.valid_end_utc:%Y%m%d%H}"
    )
    quality = provider.quality_report(request)
    quality_sha = hashlib.sha256(
        (json.dumps(quality, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()
    quality_path = write_json_report(
        quality,
        runtime_root
        / "reports"
        / "conditions"
        / "pcic"
        / f"{report_stem}-quality-{quality_sha}.json",
    )
    comparison_path = None
    comparison = None
    if args.eccc_condition_pack is not None:
        eccc_pack = load_condition_pack(args.eccc_condition_pack)
        comparison = compare_pcic_to_eccc(
            first,
            eccc_pack,
            target_longitude_deg=target_lon,
            target_latitude_deg=target_lat,
            target_elevation_m=args.target_elevation_m,
            eccc_original_organization=args.eccc_original_organization,
        )
        comparison_sha = hashlib.sha256(
            (
                json.dumps(
                    comparison,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        comparison_path = write_json_report(
            comparison,
            runtime_root
            / "reports"
            / "conditions"
            / "pcic"
            / f"{report_stem}-eccc-disagreement-{comparison_sha}.json",
        )
    print(
        json.dumps(
            {
                "condition_id": first.condition_id,
                "normalized_output_sha256": first.normalized_output_sha256,
                "condition_pack_path": str(pack_path),
                "source_snapshot_id": snapshot.snapshot_id,
                "source_snapshot_path": str(source_root),
                "source_files": snapshot.manifest["files"],
                "quality_report_path": str(quality_path),
                "comparison_report_path": (
                    str(comparison_path) if comparison_path is not None else None
                ),
                "comparison_overlap_counts": (
                    {
                        name: metrics["overlap_count"]
                        for name, metrics in comparison["variables"].items()
                    }
                    if comparison is not None
                    else None
                ),
                "deterministic_replay_identical": replay_identical,
            },
            sort_keys=True,
        )
    )
    return 0


def characterize_m2_forcing_command(args: argparse.Namespace) -> int:
    """Build and atomically publish a deterministic, non-activating M2 report."""

    from datetime import datetime, timezone

    from app.core.settings import get_settings
    from app.processing.conditions.characterization import (
        build_m2_forcing_characterization,
        write_characterization_report,
    )
    from app.processing.conditions.eccc import load_eccc_snapshot, mountain_grid_from_pack
    from app.processing.conditions.protocol import ConditionRequest
    from app.processing.conditions.storage import load_condition_pack

    def utc_hour(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError(f"Expected timezone-aware UTC timestamp, got {value!r}")
        parsed = parsed.astimezone(timezone.utc)
        if parsed.minute or parsed.second or parsed.microsecond:
            raise ValueError(f"Expected exact UTC hour, got {value!r}")
        return parsed

    data_root = get_settings().data_root
    runtime_root = Path(args.runtime_root).resolve()
    paths = {
        "M2 output root": runtime_root,
        "terrain bake": Path(args.bake_root).resolve(),
        "ECCC source snapshot": Path(args.eccc_snapshot).resolve(),
        "ECCC ConditionPack": Path(args.eccc_condition_pack).resolve(),
        "PCIC ConditionPack": Path(args.pcic_condition_pack).resolve(),
    }
    for role, path in paths.items():
        _reject_condition_path_in_data(path, data_root, role)
    request = ConditionRequest(
        mountain_grid=mountain_grid_from_pack(Path(args.mountain_pack).resolve()),
        valid_start_utc=utc_hour(args.start),
        valid_end_utc=utc_hour(args.end),
    )
    inputs = {
        "project_root": Path(args.project_root).resolve(),
        "bake_root": paths["terrain bake"],
        "eccc_snapshot": load_eccc_snapshot(paths["ECCC source snapshot"]),
        "eccc_pack": load_condition_pack(paths["ECCC ConditionPack"]),
        "pcic_pack": load_condition_pack(paths["PCIC ConditionPack"]),
        "request": request,
        "target_longitude_deg": args.target_longitude_deg,
        "target_latitude_deg": args.target_latitude_deg,
        "existing_reference_elevation_m": args.target_elevation_m,
        "selected_eccc_station_id": args.eccc_station_id,
        "lapse_rate_k_per_m": args.lapse_rate_k_per_m,
    }
    first = build_m2_forcing_characterization(**inputs)
    replay = build_m2_forcing_characterization(**inputs)
    first_bytes = json.dumps(
        first, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    replay_bytes = json.dumps(
        replay, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    if first_bytes != replay_bytes:
        raise RuntimeError("M2 forcing characterization failed deterministic replay.")
    target = write_characterization_report(first, runtime_root)
    if write_characterization_report(replay, runtime_root) != target:
        raise RuntimeError("M2 forcing characterization publication was not idempotent.")
    print(
        json.dumps(
            {
                "report_id": first["report_id"],
                "report_path": str(target),
                "deterministic_replay_identical": True,
                "reference_elevation_activation": first["bake_reference_elevation"][
                    "activation"
                ]["status"],
                "snow_swe_status": first["snow_depth_swe"]["status"],
                "radiation_activation": first["radiation"]["activation"],
            },
            sort_keys=True,
        )
    )
    return 0


def derive_reference_elevation_command(args: argparse.Namespace) -> int:
    """Derive and atomically publish a non-activating bake-bound contract."""

    from app.bake_identity import processing_manifest
    from app.core.settings import get_settings
    from app.processing.mountain_pack import load_mountain_pack
    from app.processing.terrain.reference_elevation import (
        canonical_reference_elevation_bytes,
        derive_reference_elevation_from_bake,
        write_reference_elevation,
    )

    settings = get_settings()
    runtime_root = Path(args.runtime_root).resolve()
    bake_root = Path(args.bake_root).resolve()
    mountain_pack_path = Path(args.mountain_pack).resolve()
    for role, path in {
        "Reference-elevation output root": runtime_root,
        "Terrain bake": bake_root,
        "Mountain Pack": mountain_pack_path,
    }.items():
        _reject_condition_path_in_data(path, settings.data_root, role)
    selected_settings = settings.model_copy(
        update={"mountain_pack_path": mountain_pack_path}
    )
    _, pack_identity = load_mountain_pack(selected_settings)
    processing_sha = processing_manifest(settings.project_root)["sha256"]
    inputs = {
        "bake_root": bake_root,
        "expected_processing_sha256": processing_sha,
        "expected_mountain_pack_sha256": pack_identity["sha256"],
        "target_longitude_deg": args.target_longitude_deg,
        "target_latitude_deg": args.target_latitude_deg,
        "legacy_elevation_m": args.legacy_elevation_m,
    }
    first = derive_reference_elevation_from_bake(**inputs)
    replay = derive_reference_elevation_from_bake(**inputs)
    if canonical_reference_elevation_bytes(first) != canonical_reference_elevation_bytes(
        replay
    ):
        raise RuntimeError("Reference-elevation derivation failed deterministic replay.")
    target = write_reference_elevation(first, runtime_root)
    if write_reference_elevation(replay, runtime_root) != target:
        raise RuntimeError("Reference-elevation publication was not idempotent.")
    print(
        json.dumps(
            {
                "reference_elevation_id": first.reference_elevation_id,
                "reference_elevation_path": str(target),
                "proposed_reference_elevation_m": first.selection.proposed_reference_elevation_m,
                "legacy_reference_elevation_m": first.legacy_reference.elevation_m,
                "difference_from_legacy_m": first.selection.difference_from_legacy_m,
                "activation_status": first.selection.activation_status,
                "vertical_datum_status": first.input_bake.vertical_datum_status,
                "deterministic_replay_identical": True,
            },
            sort_keys=True,
        )
    )
    return 0


def preserve_bake_command(args: argparse.Namespace) -> int:
    """Inventory, copy, and re-hash the complete active served surface."""

    from app.bake_preservation import preserve_bake, validate_preservation
    from app.core.settings import get_settings

    settings = get_settings()
    runtime_root = Path(args.runtime_root).resolve()
    bake_root = Path(args.bake_root).resolve()
    _reject_condition_path_in_data(runtime_root, settings.data_root, "Preservation output root")
    _reject_condition_path_in_data(bake_root, settings.data_root, "Bake preservation source")
    target = preserve_bake(bake_root, runtime_root)
    inventory = validate_preservation(target)
    print(
        json.dumps(
            {
                "preservation_path": str(target),
                "preservation_id": inventory["preservation_id"],
                "source_bake_sha256": inventory["source_bake_sha256"],
                "file_count": inventory["file_count"],
                "total_bytes": inventory["total_bytes"],
                "inventory_sha256": inventory["inventory_sha256"],
                "copy_verified_byte_for_byte": True,
            },
            sort_keys=True,
        )
    )
    return 0


def compare_bakes_command(args: argparse.Namespace) -> int:
    """Publish a deterministic scientific old/new terrain comparison."""

    from app.bake_comparison import compare_bakes, write_bake_comparison
    from app.core.settings import get_settings

    settings = get_settings()
    old_bake = Path(args.old_bake).resolve()
    new_bake = Path(args.new_bake).resolve()
    runtime_root = Path(args.runtime_root).resolve()
    for role, path in {"Old bake": old_bake, "New bake": new_bake, "Output": runtime_root}.items():
        _reject_condition_path_in_data(path, settings.data_root, role)
    first = compare_bakes(old_bake, new_bake)
    replay = compare_bakes(old_bake, new_bake)
    if first != replay:
        raise RuntimeError("Bake comparison failed deterministic replay.")
    target = write_bake_comparison(first, runtime_root)
    if write_bake_comparison(replay, runtime_root) != target:
        raise RuntimeError("Bake-comparison publication was not idempotent.")
    print(json.dumps({
        "comparison_id": first["comparison_id"],
        "comparison_path": str(target),
        "terrain_arrays_numerically_identical": first["scientific_summary"]["terrain_arrays_numerically_identical"],
        "total_mask_changed_count": first["scientific_summary"]["total_mask_changed_count"],
        "total_value_changed_count": first["scientific_summary"]["total_value_changed_count"],
        "deterministic_replay_identical": True,
    }, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bake = subparsers.add_parser("bake", help="Bake the terrain artifacts (offline, one-time)")
    bake.add_argument("--force", action="store_true", help="Rebuild even if a bake already exists")
    bake.add_argument("--pack", type=Path, help="Mountain Pack JSON to bake")
    bake.add_argument("--data-root", type=Path, help="Read-only source root for the selected pack")
    bake.add_argument(
        "--runtime-root",
        type=Path,
        help="Generated runtime root; use a separate root per mountain",
    )
    bake.set_defaults(func=bake_command)

    from app.baseline import DEFAULT_SEED

    project_root = Path(__file__).resolve().parents[2]
    comparison = subparsers.add_parser(
        "compare-baseline",
        help="Compare two runout engines on the frozen synthetic M0 cases",
    )
    comparison.add_argument("--baseline-engine", default="fast")
    comparison.add_argument("--candidate-engine", default="advanced")
    comparison.add_argument("--seed", type=int, default=DEFAULT_SEED)
    comparison.add_argument(
        "--expectations",
        default=str(project_root / "backend" / "config" / "m0-baseline.json"),
        help="Frozen deterministic expectation manifest",
    )
    comparison.add_argument(
        "--no-expectations",
        action="store_true",
        help="Run the comparison without checking the frozen M0 manifest",
    )
    comparison.add_argument(
        "--output",
        default="-",
        help="Report path, or '-' for stdout (default)",
    )
    comparison.set_defaults(func=compare_baseline_command)

    condition_pack = subparsers.add_parser(
        "validate-condition-pack",
        help="Validate a Condition Pack file or atomically-written pack directory",
    )
    condition_pack.add_argument("path", help="Path to condition-pack.json or its directory")
    condition_pack.set_defaults(func=validate_condition_pack_command)

    era5_request = subparsers.add_parser(
        "prepare-era5-land-request",
        help="Freeze a credential-free ERA5-Land monthly request manifest",
    )
    era5_request.add_argument("--start", required=True, help="Inclusive exact UTC hour")
    era5_request.add_argument("--end", required=True, help="Inclusive exact UTC hour")
    era5_request.add_argument(
        "--area",
        nargs=4,
        required=True,
        type=float,
        metavar=("NORTH", "WEST", "SOUTH", "EAST"),
    )
    era5_request.add_argument(
        "--runtime-root", default=str(project_root / "runtime")
    )
    era5_request.set_defaults(func=prepare_era5_land_request_command)

    snowpack_example = subparsers.add_parser(
        "verify-snowpack-example",
        help="Run and preserve the unchanged pinned SNOWPACK 3.7.0 res1exp example",
    )
    snowpack_example.add_argument("--executable", required=True)
    snowpack_example.add_argument("--source-root", required=True)
    snowpack_example.add_argument(
        "--runtime-root", default=str(project_root / "runtime")
    )
    snowpack_example.add_argument("--timeout-seconds", type=float, default=120.0)
    snowpack_example.set_defaults(func=verify_snowpack_example_command)

    eccc = subparsers.add_parser(
        "replay-eccc-conditions",
        help="Offline import and deterministic replay of an ECCC historical snapshot",
    )
    eccc.add_argument(
        "--snapshot",
        default=None,
        help="Existing immutable ECCC source-cache snapshot directory",
    )
    eccc.add_argument(
        "--stations",
        default=None,
        help="ECCC climate-stations CSV; requires --hourly and excludes --snapshot",
    )
    eccc.add_argument(
        "--hourly",
        default=None,
        help="ECCC climate-hourly CSV; requires --stations and excludes --snapshot",
    )
    eccc.add_argument("--start", required=True, help="Inclusive UTC hour")
    eccc.add_argument("--end", required=True, help="Inclusive UTC hour")
    eccc.add_argument(
        "--target-elevation-m",
        required=True,
        type=float,
        help="Explicit temperature-transfer elevation; never inferred from a stale bake",
    )
    eccc.add_argument(
        "--station-id",
        default=None,
        help="Optional explicit station override; default uses the reported ranking",
    )
    eccc.add_argument(
        "--runtime-root",
        default=str(project_root / "runtime"),
        help="Generated cache/output root (must not be DATA/)",
    )
    eccc.add_argument(
        "--mountain-pack",
        default=str(project_root / "backend" / "config" / "mount_hosmer.pack.json"),
        help="Current mountain-pack contract used for grid identity and center",
    )
    eccc.set_defaults(func=replay_eccc_conditions_command)

    pcic = subparsers.add_parser(
        "replay-pcic-conditions",
        help="Offline acquisition or cache-native replay of the selected PCIC snapshot",
    )
    pcic.add_argument(
        "--snapshot",
        default=None,
        help="Existing immutable PCIC source-cache snapshot directory",
    )
    pcic.add_argument(
        "--acquire",
        action="store_true",
        help="Acquire the fixed selected clipped series and official metadata once",
    )
    pcic.add_argument("--start", required=True, help="Inclusive UTC hour")
    pcic.add_argument("--end", required=True, help="Inclusive UTC hour")
    pcic.add_argument(
        "--target-elevation-m",
        required=True,
        type=float,
        help="Mount Hosmer comparison elevation; no PCIC elevation correction is applied",
    )
    pcic.add_argument(
        "--eccc-condition-pack",
        default=None,
        help="Validated ECCC ConditionPack directory/file for independent disagreement metrics",
    )
    pcic.add_argument(
        "--eccc-original-organization",
        default="Environment and Climate Change Canada - Meteorological Service of Canada",
        help="Original organization from the selected ECCC station metadata snapshot",
    )
    pcic.add_argument(
        "--runtime-root",
        default=str(project_root / "runtime"),
        help="Generated cache/output root (must not be DATA/)",
    )
    pcic.add_argument(
        "--mountain-pack",
        default=str(project_root / "backend" / "config" / "mount_hosmer.pack.json"),
        help="Current mountain-pack contract used for grid identity and center",
    )
    pcic.set_defaults(func=replay_pcic_conditions_command)

    characterization = subparsers.add_parser(
        "characterize-m2-forcing",
        help="Offline deterministic elevation, correction, and representativeness report",
    )
    characterization.add_argument("--eccc-snapshot", required=True)
    characterization.add_argument("--eccc-condition-pack", required=True)
    characterization.add_argument("--pcic-condition-pack", required=True)
    characterization.add_argument("--start", required=True, help="Inclusive UTC hour")
    characterization.add_argument("--end", required=True, help="Inclusive UTC hour")
    characterization.add_argument("--target-longitude-deg", required=True, type=float)
    characterization.add_argument("--target-latitude-deg", required=True, type=float)
    characterization.add_argument("--target-elevation-m", required=True, type=float)
    characterization.add_argument("--eccc-station-id", default=None)
    characterization.add_argument("--lapse-rate-k-per-m", type=float, default=0.0065)
    characterization.add_argument(
        "--runtime-root", default=str(project_root / "runtime")
    )
    characterization.add_argument(
        "--bake-root", default=str(project_root / "runtime" / "baked")
    )
    characterization.add_argument(
        "--mountain-pack",
        default=str(project_root / "backend" / "config" / "mount_hosmer.pack.json"),
    )
    characterization.add_argument("--project-root", default=str(project_root))
    characterization.set_defaults(func=characterize_m2_forcing_command)

    reference = subparsers.add_parser(
        "derive-reference-elevation",
        help="Derive a versioned bake-bound reference elevation without activating it",
    )
    reference.add_argument("--target-longitude-deg", required=True, type=float)
    reference.add_argument("--target-latitude-deg", required=True, type=float)
    reference.add_argument("--legacy-elevation-m", required=True, type=float)
    reference.add_argument(
        "--runtime-root", default=str(project_root / "runtime")
    )
    reference.add_argument(
        "--bake-root", default=str(project_root / "runtime" / "baked")
    )
    reference.add_argument(
        "--mountain-pack",
        default=str(project_root / "backend" / "config" / "mount_hosmer.pack.json"),
    )
    reference.set_defaults(func=derive_reference_elevation_command)

    preservation = subparsers.add_parser(
        "preserve-bake",
        help="Inventory and preserve the complete active bake before replacement",
    )
    preservation.add_argument(
        "--runtime-root", default=str(project_root / "runtime")
    )
    preservation.add_argument(
        "--bake-root", default=str(project_root / "runtime" / "baked")
    )
    preservation.set_defaults(func=preserve_bake_command)

    bake_comparison = subparsers.add_parser(
        "compare-bakes", help="Compare a preserved bake with a rebuilt bake without activation"
    )
    bake_comparison.add_argument("--old-bake", required=True)
    bake_comparison.add_argument(
        "--new-bake", default=str(project_root / "runtime" / "baked")
    )
    bake_comparison.add_argument(
        "--runtime-root", default=str(project_root / "runtime")
    )
    bake_comparison.set_defaults(func=compare_bakes_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()

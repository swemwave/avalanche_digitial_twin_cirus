from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.settings import get_settings
from app.services.catalog import generate_catalog, inspect_point_cloud
from app.services.conditions import process_avalanche_forecast, process_dynamic, process_snow, process_weather
from app.services.events import process_all_events, process_event
from app.services.susceptibility import process_all_event_susceptibility, process_event_susceptibility
from app.services.terrain import generate_terrain_layers


def _progress(percent: int, message: str) -> None:
    print(f"  [{percent:3d}%] {message}", flush=True)


def run_analysis_command(args: argparse.Namespace) -> int:
    """Produce release zones and a hazard picture for one set of conditions."""
    from datetime import datetime

    from app.processing.weather.presets import PRESETS, preset
    from app.services.analysis import run_analysis
    from app.storage import repository

    settings = get_settings()

    scenario = None
    if args.preset:
        if args.preset not in PRESETS:
            print(f"Unknown preset {args.preset!r}. Available: {', '.join(PRESETS)}")
            return 2
        scenario = preset(args.preset)

    at = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else None
    if args.mode == "historical" and at is None:
        print("Historical replay needs --at. Without a datetime there is nothing to replay.")
        return 2
    if args.mode == "scenario" and scenario is None:
        print(f"Scenario mode needs --preset. Available: {', '.join(PRESETS)}")
        return 2

    payload = run_analysis(
        settings,
        mode=args.mode,
        at=at,
        scenario=scenario,
        event_id=args.event_id,
        progress=_progress,
    )
    repository.save_analysis(settings, payload, correlation_id="cli")

    print()
    print(f"Analysis        {payload['analysis_id']}")
    print(f"  hazard        {payload['hazard_score']} / 100   (a relative index, NOT a probability)")
    print(f"  confidence    {payload['confidence_score']} / 100")
    print(f"  release zones {payload['release_zones']['zone_count']}")
    print(f"  model         {payload['model']['model_version']} ({payload['model']['config_sha256'][:12]})")
    for warning in payload.get("warnings", []):
        print(f"  ! {warning}")
    print()
    print(payload["disclaimer"])
    return 0


def run_simulation_command(args: argparse.Namespace) -> int:
    from app.services.analysis import run_simulation
    from app.storage import repository

    settings = get_settings()
    payload = run_simulation(
        settings,
        analysis_id=args.analysis_id,
        zone_ids=args.zone_id or None,
        simulation_mode=args.mode,
        release_size=args.release_size,
        seed=args.seed,
        progress=_progress,
    )
    repository.save_simulation(settings, payload, correlation_id="cli")

    assessment = payload["assessment"]
    runout = payload["runout"]
    print()
    print(f"Simulation      {payload['simulation_id']}  ({payload['engine']})")
    print(f"  risk          {assessment['combinedRiskScore']} / 100  {assessment['riskLevel']}")
    print(f"  hazard        {assessment['hazardScore']} / 100")
    print(f"  consequence   {assessment['consequenceScore']} / 100  {assessment['consequenceClass']}")
    print(f"  confidence    {assessment['confidenceScore']} / 100")
    print(f"  runout        {runout['core_area_m2'] / 10_000:.0f} ha core, "
          f"{runout['uncertainty_area_m2'] / 10_000:.0f} ha envelope")
    exposure = payload.get("exposure", {}) or {}
    assets = exposure.get("assets", []) or []
    print(f"  exposed       {len(assets)} assets (a LOWER BOUND -- OSM has 1 building for this AOI)")
    for category in (exposure.get("summary", {}) or {}).get("by_category", {}).values():
        print(f"                {category['exposed_count']:>3} {category['title'].lower()}")
    print()
    for reason in assessment.get("mainReasons", [])[:5]:
        print(f"  - {reason}")
    print()
    for warning in payload.get("warnings", []):
        print(f"  ! {warning}")
    print()
    print(payload["disclaimer"])
    return 0


def data_health_command(args: argparse.Namespace) -> int:
    from app.services.data_catalog import health_report

    settings = get_settings()
    report = health_report(settings)

    print(f"Data health: {report['overall_status'].upper()}")
    summary = report["summary"]
    print(
        f"  {summary['datasets_usable_by_model']}/{summary['datasets_checked']} datasets usable, "
        f"{summary['critical_issues']} critical, {summary['warnings']} warnings"
    )
    print()
    for dataset in report["datasets"]:
        marker = {"ok": " ", "warning": "~", "degraded": "~", "critical": "!"}.get(dataset["status"], "?")
        print(f" {marker} {dataset['status']:<9} {dataset['title']}")
        for issue in dataset.get("issues", []):
            if issue["severity"] in {"critical", "warning"}:
                print(f"      - {issue['message']}")
    print()
    print(report["empty_file_policy"])
    # A degraded data state is not a failed command: the model is built to run with
    # missing inputs and to say so. Exiting non-zero would break any scheduler.
    return 0


def migrate_command(args: argparse.Namespace) -> int:
    from app.storage.database import get_database

    settings = get_settings()
    database = get_database(settings)
    database.create_all()
    applied = database.migrate()

    print(f"Database: {settings.database_url}")
    if applied:
        print(f"  applied migrations: {', '.join(str(item) for item in applied)}")
    else:
        print("  already up to date")
    return 0


def scan_data(args: argparse.Namespace) -> int:
    settings = get_settings()
    catalog = generate_catalog(settings, verify_checksums=not args.skip_checksum)
    output = settings.runtime_root / "catalog" / "data_catalog.json"
    print(f"Catalog written: {output}")
    print(json.dumps(catalog["summary"], indent=2))
    return 0


def inspect_point_clouds(args: argparse.Namespace) -> int:
    settings = get_settings()
    point_cloud_root = settings.data_root / "static" / "lidar_bc" / "downloads" / "LiDAR_Point_Cloud_Index"
    output_path = settings.runtime_root / "catalog" / "point_cloud_inventory.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = []
    if not point_cloud_root.exists():
        output_path.write_text(json.dumps({"records": [], "warning": "point-cloud folder missing"}, indent=2), encoding="utf-8")
        print(f"Point-cloud folder missing: {point_cloud_root}")
        return 0
    for path in sorted(point_cloud_root.glob("*.la?")):
        metadata, warnings = inspect_point_cloud(Path(path))
        records.append(
            {
                "name": path.name,
                "relative_path": path.relative_to(settings.data_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "metadata": metadata,
                "warnings": warnings,
            }
        )
    output_path.write_text(json.dumps({"records": records}, indent=2), encoding="utf-8")
    print(f"Point-cloud inventory written: {output_path}")
    return 0


def process_terrain(args: argparse.Namespace) -> int:
    settings = get_settings()
    result = generate_terrain_layers(settings, force=args.force)
    print(f"Terrain layers written: {settings.runtime_root / 'processed' / 'static' / 'terrain_layers.json'}")
    print(json.dumps({"layer_count": len(result["layers"]), "generated_at_utc": result["generated_at_utc"]}, indent=2))
    return 0


def process_events(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.all:
        result = process_all_events(settings, force=args.force)
        print(f"Event summaries written under: {settings.runtime_root / 'processed' / 'events'}")
        print(json.dumps(result, indent=2))
        return 1 if result.get("errors") else 0
    if not args.event_id:
        raise SystemExit("Provide --all or --event-id")
    result = process_event(settings, args.event_id, force=args.force)
    print(f"Event summary written: {settings.runtime_root / 'processed' / 'events' / args.event_id / 'event_summary.json'}")
    print(json.dumps({"event_id": result["event_id"], "layer_count": len(result["layers"]), "warning_count": len(result["warnings"])}, indent=2))
    return 0


def process_conditions(args: argparse.Namespace) -> int:
    settings = get_settings()
    result = process_dynamic(settings, force=args.force)
    print(f"Dynamic summaries written: {settings.runtime_root / 'processed' / 'dynamic'}")
    print(json.dumps(result, indent=2))
    return 0


def process_weather_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    result = process_weather(settings, force=args.force)
    print(f"Weather normalized: {settings.runtime_root / 'processed' / 'dynamic' / 'weather_normalized.parquet'}")
    print(json.dumps({"record_count": result.get("record_count"), "station_count": result.get("station_count"), "warnings": result.get("warnings", [])}, indent=2))
    return 0


def process_snow_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    result = process_snow(settings, force=args.force)
    print(f"Snow normalized: {settings.runtime_root / 'processed' / 'dynamic' / 'snow_stations_normalized.parquet'}")
    print(json.dumps({"record_count": result.get("record_count"), "station_count": len(result.get("stations", [])), "warnings": result.get("warnings", [])}, indent=2))
    return 0


def process_forecast_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    result = process_avalanche_forecast(settings, force=args.force)
    print(f"Avalanche forecast summary written: {settings.runtime_root / 'processed' / 'dynamic' / 'avalanche_forecast.json'}")
    print(json.dumps({"applicable_region": result.get("applicable_region"), "valid_until_utc": result.get("valid_until_utc"), "warnings": result.get("warnings", [])}, indent=2))
    return 0


def process_susceptibility_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.all:
        result = process_all_event_susceptibility(settings, force=args.force)
        print(f"Susceptibility summaries written under: {settings.runtime_root / 'processed' / 'events'}")
        print(json.dumps(result, indent=2))
        return 1 if result.get("errors") else 0
    if not args.event_id:
        raise SystemExit("Provide --all or --event-id")
    result = process_event_susceptibility(settings, args.event_id, force=args.force)
    print(f"Susceptibility summary written: {settings.runtime_root / 'processed' / 'events' / args.event_id / 'susceptibility_summary.json'}")
    print(
        json.dumps(
            {
                "event_id": result["event_id"],
                "dynamic_score": result["dynamic_condition_index"]["score"],
                "combined_available": result["combined_index"]["available"],
                "warning_count": len(result["warnings"]),
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan-data", help="Catalog the configured Mount Hosmer source data")
    scan.add_argument("--skip-checksum", action="store_true", help="Catalog metadata without verifying SHA-256 checksums")
    scan.set_defaults(func=scan_data)

    point_clouds = subparsers.add_parser("inspect-point-clouds", help="Inspect LAS/LAZ headers when laspy/lazrs are installed")
    point_clouds.set_defaults(func=inspect_point_clouds)

    terrain = subparsers.add_parser("process-terrain", help="Generate terrain preview layers and prototype susceptibility")
    terrain.add_argument("--force", action="store_true", help="Regenerate cached terrain previews")
    terrain.set_defaults(func=process_terrain)

    events = subparsers.add_parser("process-events", help="Generate satellite event previews and summaries")
    events.add_argument("--all", action="store_true", help="Process all discovered event folders")
    events.add_argument("--event-id", help="Process one event id, for example MH_20260116T183016Z")
    events.add_argument("--force", action="store_true", help="Regenerate cached event previews")
    events.set_defaults(func=process_events)

    dynamic = subparsers.add_parser("process-dynamic", help="Normalize weather, snow, and Avalanche Canada forecast data")
    dynamic.add_argument("--force", action="store_true", help="Regenerate cached dynamic summaries")
    dynamic.set_defaults(func=process_conditions)

    weather = subparsers.add_parser("process-weather", help="Normalize ECCC weather files")
    weather.add_argument("--force", action="store_true", help="Regenerate cached weather summary")
    weather.set_defaults(func=process_weather_command)

    snow = subparsers.add_parser("process-snow", help="Normalize BC snow-station files")
    snow.add_argument("--force", action="store_true", help="Regenerate cached snow summary")
    snow.set_defaults(func=process_snow_command)

    forecast = subparsers.add_parser("process-forecast", help="Parse current Avalanche Canada forecast context")
    forecast.add_argument("--force", action="store_true", help="Regenerate cached forecast summary")
    forecast.set_defaults(func=process_forecast_command)

    susceptibility = subparsers.add_parser("process-susceptibility", help="Generate dynamic and combined prototype susceptibility")
    susceptibility.add_argument("--all", action="store_true", help="Process susceptibility for all discovered event folders")
    susceptibility.add_argument("--event-id", help="Process one event id, for example MH_20260430T182949Z")
    susceptibility.add_argument("--force", action="store_true", help="Regenerate cached susceptibility summaries and previews")
    susceptibility.set_defaults(func=process_susceptibility_command)

    analysis = subparsers.add_parser(
        "run-analysis",
        help="Score release zones for one set of conditions (current, a past datetime, or a scenario)",
    )
    analysis.add_argument(
        "--mode", choices=("current", "historical", "scenario"), default="current"
    )
    analysis.add_argument("--at", help="ISO-8601 datetime, for --mode historical")
    analysis.add_argument("--preset", help="Scenario preset name, for --mode scenario")
    analysis.add_argument("--event-id", help="Use this event's satellite snow observation")
    analysis.set_defaults(func=run_analysis_command)

    simulation = subparsers.add_parser(
        "run-simulation", help="Run snow down the hill from a stored analysis's release zones"
    )
    simulation.add_argument("analysis_id", help="The analysis to simulate, e.g. AN_20260714T044404Z_bd3ea5")
    simulation.add_argument("--mode", choices=("fast", "advanced"), default="fast")
    simulation.add_argument(
        "--release-size", choices=("small", "medium", "large", "very_large"), default=None
    )
    simulation.add_argument(
        "--zone-id", action="append", help="Simulate only this zone. Repeatable. Omit for all zones."
    )
    simulation.add_argument("--seed", type=int, help="Fix the seed to reproduce a run exactly")
    simulation.set_defaults(func=run_simulation_command)

    health = subparsers.add_parser(
        "data-health", help="Report which datasets the model can actually use, and why not"
    )
    health.set_defaults(func=data_health_command)

    migrate = subparsers.add_parser(
        "migrate", help="Create and migrate the database (also runs automatically at startup)"
    )
    migrate.set_defaults(func=migrate_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()

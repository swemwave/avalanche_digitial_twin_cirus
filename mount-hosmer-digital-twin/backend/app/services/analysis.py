"""The orchestrator: terrain + conditions -> release zones -> simulation -> risk.

One analysis is one timestamped, reproducible answer to "what is this mountain
doing under these conditions?". It records the model version, the configuration
hash, the terrain checksums and the exact inputs, so the same analysis can be
recomputed later and produce the same numbers.

An analysis does not simulate. It finds release zones. A *simulation* takes one or
more of those zones and runs snow down the hill from them. That separation is
deliberate: finding the zones is expensive and shared, while simulating a
particular zone at a particular size is cheap and is what a user iterates on.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.core import provenance as prov
from app.core.model_config import DISCLAIMER, ModelConfig, load_model_config
from app.core.paths import analyses_dir, ensure_runtime_dirs, simulations_dir
from app.core.settings import Settings
from app.models import instability, release_zones, risk, snow, wind
from app.processing.harmonization.grids import AnalysisGrid, grid_set
from app.processing.harmonization.raster_io import Semantics, read_aligned, write_raster
from app.processing.render import render_preview
from app.processing.terrain import engine as terrain_engine
from app.processing.weather import features as weather
from app.processing.weather.features import ConditionSet, Mode, ScenarioInput
from app.simulation import exposure, runout

#: Terrain layers the models need in memory. Read once per analysis.
REQUIRED_TERRAIN = (
    "elevation",
    "slope",
    "aspect",
    "plan_curvature",
    "general_curvature",
    "forest_mask",
    "canopy_height",
    "distance_to_ridge",
    "landcover",
    "terrain_susceptibility",
    "starting_zone_terrain",
    "terrain_source",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TerrainBundle:
    grid: AnalysisGrid
    layers: dict[str, np.ma.MaskedArray]
    index: dict[str, Any]

    def __getitem__(self, key: str) -> np.ma.MaskedArray:
        return self.layers[key]

    @property
    def lidar_fraction(self) -> float:
        return float(self.index.get("terrain", {}).get("lidar_fraction", 0.0))


def load_terrain(settings: Settings) -> TerrainBundle:
    index = terrain_engine.generate(settings, force=False)
    layers = {name: terrain_engine.load_layer(settings, name) for name in REQUIRED_TERRAIN}
    return TerrainBundle(grid=grid_set(settings).terrain, layers=layers, index=index)


def load_event_ndsi(
    settings: Settings, event_id: str, grid: AnalysisGrid
) -> tuple[np.ma.MaskedArray | None, np.ma.MaskedArray | None, float | None, list[str]]:
    """Read a satellite event's NDSI and cloud mask onto the terrain grid.

    Sentinel-2 (10 m) is preferred over Landsat (30 m). Cloud comes from the
    Sentinel SCL band -- clouds are NEVER allowed to read as snow.
    """
    warnings: list[str] = []
    event_dir = settings.data_root / "events" / event_id
    if not event_dir.is_dir():
        return None, None, None, [f"Event {event_id} not found in the source data."]

    ndsi_files = sorted((event_dir / "sentinel2").glob("*NDSI*.tif")) if (event_dir / "sentinel2").is_dir() else []
    scl_files = sorted((event_dir / "sentinel2").glob("*SCL*.tif")) if (event_dir / "sentinel2").is_dir() else []

    if not ndsi_files:
        ndsi_files = sorted((event_dir / "landsat").glob("*NDSI*.tif")) if (event_dir / "landsat").is_dir() else []
        if ndsi_files:
            warnings.append(
                "No Sentinel-2 NDSI for this event; falling back to the 30 m Landsat NDSI, which "
                "resolves snow far more coarsely than the 5 m terrain grid."
            )

    if not ndsi_files:
        return None, None, None, [f"No NDSI raster found for event {event_id}."]

    ndsi = read_aligned(ndsi_files[0], grid, Semantics.INDEX)

    cloud = None
    cloud_percent = None
    if scl_files:
        scl = read_aligned(scl_files[0], grid, Semantics.CATEGORICAL)
        codes = np.asarray(scl.filled(0)).astype(int)
        # Sentinel-2 scene classification: 3 cloud shadow, 8 medium-probability
        # cloud, 9 high-probability cloud, 10 thin cirrus.
        cloudy = np.isin(codes, [3, 8, 9, 10])
        cloud = np.ma.array(cloudy.astype("float32"), mask=np.ma.getmaskarray(scl))
        valid = ~np.ma.getmaskarray(scl)
        cloud_percent = round(float(cloudy[valid].mean() * 100.0), 2) if valid.any() else None
        if cloud_percent is not None and cloud_percent > 60:
            warnings.append(
                f"The satellite scene for {event_id} is {cloud_percent:.0f}% cloud covered. Snow "
                f"observations from it are unreliable; the snow model falls back to modelling."
            )
    else:
        warnings.append(
            "No Sentinel-2 scene-classification band; cloud could not be masked. NDSI-based snow "
            "cover may include cloud, so it is used cautiously."
        )

    return ndsi, cloud, cloud_percent, warnings


def build_conditions(
    settings: Settings,
    config: ModelConfig,
    *,
    mode: str,
    at: datetime | None = None,
    scenario: ScenarioInput | None = None,
) -> ConditionSet:
    source = weather.load_weather(settings)
    if mode == "historical":
        if at is None:
            raise ValueError("Historical replay requires a datetime.")
        return weather.replay_conditions(settings, config, at, source)
    if mode == "current":
        return weather.current_conditions(settings, config, source)
    if mode == "scenario":
        if scenario is None:
            raise ValueError("Scenario mode requires scenario inputs.")
        return weather.scenario_conditions(settings, config, scenario, at, source)
    raise ValueError(f"Unknown analysis mode {mode!r}. Expected historical, current, or scenario.")


def load_forecast_context(settings: Settings) -> dict[str, Any] | None:
    """Avalanche Canada current products. Context only -- never scored, never a label."""
    path = settings.runtime_root / "processed" / "dynamic" / "avalanche_forecast.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def run_analysis(
    settings: Settings,
    *,
    mode: str = "current",
    at: datetime | None = None,
    scenario: ScenarioInput | None = None,
    event_id: str | None = None,
    analysis_id: str | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Produce release zones and a hazard picture for one set of conditions."""
    started = time.perf_counter()
    ensure_runtime_dirs(settings.runtime_root)
    config = load_model_config(settings)

    def report(percent: int, message: str) -> None:
        if progress is not None:
            progress(percent, message)

    analysis_id = analysis_id or f"AN_{utc_now().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    output_dir = analyses_dir(settings) / analysis_id
    output_dir.mkdir(parents=True, exist_ok=True)

    report(5, "Loading terrain")
    terrain = load_terrain(settings)
    grid = terrain.grid

    report(20, "Assembling weather conditions")
    conditions = build_conditions(settings, config, mode=mode, at=at, scenario=scenario)

    report(30, "Reading satellite snow observations")
    ndsi = cloud = None
    cloud_percent = None
    satellite_warnings: list[str] = []
    if event_id:
        ndsi, cloud, cloud_percent, satellite_warnings = load_event_ndsi(settings, event_id, grid)

    report(40, "Modelling snow conditions")
    snow_fields = snow.compute(
        config=config,
        grid=grid,
        conditions=conditions,
        elevation=terrain["elevation"],
        slope=terrain["slope"],
        aspect=terrain["aspect"],
        forest_mask=terrain["forest_mask"],
        canopy_height=terrain["canopy_height"],
        observed_ndsi=ndsi,
        observed_cloud_mask=cloud,
    )

    report(55, "Modelling wind loading")
    wind_speed = conditions.number("wind_speed_max_72h_kmh")
    wind_direction = conditions.number("wind_direction_72h_deg")
    wind_consistency = conditions.number("wind_consistency_72h")
    wind_fields = wind.compute(
        config=config,
        grid=grid,
        elevation=terrain["elevation"],
        slope=terrain["slope"],
        aspect=terrain["aspect"],
        plan_curvature=terrain["plan_curvature"],
        distance_to_ridge=terrain["distance_to_ridge"],
        forest_mask=terrain["forest_mask"],
        snow_availability=snow_fields.transportable_snow if snow_fields.available else None,
        wind_speed_kmh=wind_speed,
        wind_direction_deg=wind_direction,
        wind_consistency=wind_consistency,
    )

    report(70, "Scoring dynamic instability")
    forecast = load_forecast_context(settings)
    instability_fields = instability.compute(
        config=config,
        grid=grid,
        conditions=conditions,
        snow=snow_fields,
        wind=wind_fields,
        terrain_susceptibility=terrain["terrain_susceptibility"],
        forecast_context=forecast,
    )

    report(80, "Extracting release zones")
    zone_set = release_zones.ReleaseZoneSet(zones=[], warnings=[], explanation={"zone_count": 0})
    if instability_fields.estimated_release_score is not None:
        zone_set = release_zones.extract(
            config=config,
            grid=grid,
            release_score=instability_fields.estimated_release_score,
            terrain_susceptibility=terrain["terrain_susceptibility"],
            dynamic_instability=instability_fields.dynamic_instability,
            slope=terrain["slope"],
            aspect=terrain["aspect"],
            elevation=terrain["elevation"],
            plan_curvature=terrain["plan_curvature"],
            landcover=terrain["landcover"],
            starting_zone_terrain=terrain["starting_zone_terrain"],
            forest_mask=terrain["forest_mask"],
            wind_loading=wind_fields.wind_loading if wind_fields.available else None,
            snow_depth_index=snow_fields.snow_depth_index,
            terrain_source=terrain["terrain_source"],
        )
    else:
        zone_set.warnings.append(
            "The estimated release score was withheld for lack of condition data, so no release "
            "zones could be extracted. This is NOT a statement that no release zones exist."
        )

    report(88, "Writing model rasters")
    layer_records = _persist_layers(
        settings, output_dir, grid, snow_fields, wind_fields, instability_fields
    )

    report(94, "Scoring hazard and confidence")
    hazard, hazard_detail = _hazard_score(instability_fields, zone_set, terrain)

    inputs = list(conditions.values.values()) + snow_fields.inputs
    confidence, confidence_breakdown = risk.score_confidence(
        config=config,
        inputs=inputs,
        observation_age_hours=conditions.observation_age_hours,
        station_distance_km=conditions.station_distance_km,
        lidar_fraction=terrain.lidar_fraction,
        cloud_cover_percent=cloud_percent,
        infrastructure_complete=False,
        instability_weight_fraction=instability_fields.available_weight_fraction,
        simulation_uncertainty_ratio=None,
        is_scenario=conditions.mode is Mode.SCENARIO,
    )

    warnings = sorted(
        set(
            conditions.warnings
            + snow_fields.warnings
            + wind_fields.warnings
            + instability_fields.warnings
            + zone_set.warnings
            + satellite_warnings
        )
    )

    payload = {
        "schema_version": 1,
        "analysis_id": analysis_id,
        "generated_at_utc": utc_now().isoformat(),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "mode": conditions.mode.value,
        "valid_time_utc": conditions.valid_time_utc.isoformat(),
        "event_id": event_id,
        "model": config.provenance(),
        "software_version": settings.app_version,
        "grid": grid.describe(),
        "terrain": {
            "lidar_fraction": terrain.lidar_fraction,
            "effective_source_resolution_m": terrain.index.get("terrain", {}).get(
                "effective_source_resolution_m"
            ),
            "source": terrain.index.get("terrain", {}).get("coverage_by_source_label"),
        },
        "conditions": conditions.to_dict(),
        "snow": snow_fields.explanation,
        "wind": wind_fields.explanation,
        "instability": instability_fields.explanation,
        "release_zones": zone_set.to_geojson(),
        "release_zone_explanation": zone_set.explanation,
        "hazard": hazard_detail,
        "hazard_score": round(hazard, 1),
        "confidence_score": round(confidence, 1),
        "confidence_breakdown": confidence_breakdown,
        "satellite": {
            "event_id": event_id,
            "cloud_cover_percent": cloud_percent,
            "used_for_snow_presence": ndsi is not None,
        },
        "layers": layer_records,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
        "is_probability": False,
        "is_official_forecast": False,
    }

    (output_dir / "analysis.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "release_zones.geojson").write_text(
        json.dumps(zone_set.to_geojson(), indent=2), encoding="utf-8"
    )
    report(100, "Analysis complete")
    return payload


def _hazard_score(
    instability_fields: instability.InstabilityFields,
    zone_set: release_zones.ReleaseZoneSet,
    terrain: TerrainBundle,
) -> tuple[float, dict[str, Any]]:
    """One number for "how loaded is this mountain right now".

    Taken as the area-weighted mean release score across the release zones, not
    the maximum -- a single hot pixel should not set the hazard for the whole
    mountain. Where no zones exist, the high percentile of the release score on
    avalanche terrain is used, so "no zones crossed the threshold" does not silently
    become "hazard zero".
    """
    if zone_set.zones:
        areas = np.array([zone.properties["area_m2"] for zone in zone_set.zones], dtype="float64")
        scores = np.array(
            [zone.properties["estimated_release_score"] for zone in zone_set.zones], dtype="float64"
        )
        hazard = float((areas * scores).sum() / areas.sum())
        return hazard, {
            "method": "Area-weighted mean estimated release score across all release zones.",
            "zone_count": len(zone_set.zones),
            "total_release_area_km2": round(float(areas.sum()) / 1_000_000.0, 4),
            "max_zone_score": round(float(scores.max()), 1),
            "min_zone_score": round(float(scores.min()), 1),
            "is_probability": False,
        }

    release = instability_fields.estimated_release_score
    if release is None:
        return 0.0, {
            "method": "Not computed.",
            "withheld": True,
            "reason": (
                "The dynamic instability score was withheld for lack of input data, so no hazard "
                "score could be produced. This is NOT a hazard of zero -- it is an absence of "
                "information."
            ),
            "is_probability": False,
        }

    avalanche_terrain = np.asarray(terrain["starting_zone_terrain"].filled(0.0)) > 0.5
    values = np.asarray(release.filled(np.nan))[avalanche_terrain]
    values = values[np.isfinite(values)]
    if values.size == 0:
        return 0.0, {"method": "No avalanche terrain in the AOI.", "is_probability": False}

    hazard = float(np.percentile(values, 95))
    return hazard, {
        "method": (
            "No release zone crossed the threshold. Hazard is the 95th percentile of the estimated "
            "release score across avalanche terrain, so a below-threshold day is not reported as "
            "zero hazard."
        ),
        "zone_count": 0,
        "is_probability": False,
    }


def _persist_layers(
    settings: Settings,
    output_dir: Path,
    grid: AnalysisGrid,
    snow_fields: snow.SnowFields,
    wind_fields: wind.WindFields,
    instability_fields: instability.InstabilityFields,
) -> list[dict[str, Any]]:
    """Write every model raster and its preview. Returns the layer index."""
    specs: dict[str, tuple[str, str, str, dict[str, Any]]] = {
        "snow_presence": ("Snow presence", "Snow", "0-1", {"ramp": "snow", "low": 0, "high": 1}),
        "snow_depth_index": ("Snow depth index", "Snow", "0-1 index", {"ramp": "snow", "low": 0, "high": 1}),
        "swe_index": ("SWE index", "Snow", "0-1 index", {"ramp": "snow", "low": 0, "high": 1}),
        "snow_new_loading": ("New-snow loading", "Snow", "0-1 index", {"ramp": "hazard", "low": 0, "high": 1}),
        "snow_wet_index": ("Wet-snow index", "Snow", "0-1 index", {"ramp": "thermal", "low": 0, "high": 1}),
        "snow_melt_index": ("Melt index", "Snow", "0-1 index", {"ramp": "thermal", "low": 0, "high": 1}),
        "snow_freeze_thaw_index": ("Freeze-thaw index", "Snow", "0-1 index", {"ramp": "thermal", "low": 0, "high": 1}),
        "snow_persistence": ("Snow persistence", "Snow", "0-1 index", {"ramp": "snow", "low": 0, "high": 1}),
        "wind_terrain_exposure": ("Terrain exposure (Sx)", "Wind", "degrees", {"ramp": "diverging", "symmetric": True}),
        "wind_windward_erosion": ("Windward erosion", "Wind", "0-100", {"ramp": "water", "low": 0, "high": 100}),
        "wind_lee_deposition": ("Lee deposition", "Wind", "0-100", {"ramp": "hazard", "low": 0, "high": 100}),
        "wind_cross_loading": ("Cross-loading", "Wind", "0-100", {"ramp": "hazard", "low": 0, "high": 100}),
        "wind_ridge_exposure": ("Ridge exposure", "Wind", "0-100", {"ramp": "water", "low": 0, "high": 100}),
        "wind_cornice_potential": ("Cornice potential", "Wind", "0-100", {"ramp": "snow", "low": 0, "high": 100}),
        "wind_loading": ("Combined wind loading", "Wind", "0-100", {"ramp": "hazard", "low": 0, "high": 100}),
        "dynamic_instability": ("Dynamic instability", "Hazard", "0-100 index", {"ramp": "hazard", "low": 0, "high": 100}),
        "estimated_release_score": ("Estimated release score", "Hazard", "0-100 index", {"ramp": "hazard", "low": 0, "high": 100}),
    }

    available: dict[str, np.ma.MaskedArray] = {}
    available.update(snow_fields.layers())
    if wind_fields.available:
        available.update(wind_fields.layers())
    available.update(instability_fields.layers())

    records: list[dict[str, Any]] = []
    for layer_id, array in available.items():
        if layer_id not in specs or array.count() == 0:
            continue
        title, group, units, render = specs[layer_id]
        raster = output_dir / f"{layer_id}.tif"
        write_raster(raster, array, grid, build_overviews=False)
        preview = output_dir / "previews" / f"{layer_id}.png"
        render_preview(
            preview,
            array,
            render.get("ramp", "hazard"),
            low=render.get("low"),
            high=render.get("high"),
            symmetric=bool(render.get("symmetric")),
        )
        from app.processing.harmonization.raster_io import masked_stats

        records.append(
            {
                "id": layer_id,
                "title": title,
                "group": group,
                "units": units,
                "kind": "raster_overlay",
                "stats": masked_stats(array),
                "opacity": 0.75,
                "is_probability": False,
            }
        )
    return records


# --- Simulation ---------------------------------------------------------------


def run_simulation(
    settings: Settings,
    *,
    analysis_id: str,
    zone_ids: list[str] | None = None,
    simulation_mode: str = "fast",
    release_size: str | None = None,
    seed: int | None = None,
    simulation_id: str | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Run snow down the hill from one or more release zones of a stored analysis."""
    started = time.perf_counter()
    config = load_model_config(settings)

    def report(percent: int, message: str) -> None:
        if progress is not None:
            progress(percent, message)

    analysis_path = analyses_dir(settings) / analysis_id / "analysis.json"
    if not analysis_path.exists():
        raise FileNotFoundError(f"Analysis {analysis_id} was not found.")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))

    report(5, "Loading terrain")
    terrain = load_terrain(settings)
    grid = terrain.grid

    report(15, "Rebuilding release zones")
    # Rebuild the zone pixel masks from the stored analysis. Rasterizing the
    # stored polygons is what makes a simulation reproducible from the record
    # alone, rather than requiring the analysis to still be in memory.
    zones = _rehydrate_zones(analysis, grid)
    if zone_ids:
        zones = [zone for zone in zones if zone.zone_id in set(zone_ids)]
        if not zones:
            raise KeyError(
                f"None of the requested zones {zone_ids} exist in analysis {analysis_id}."
            )
    if not zones:
        raise ValueError(
            f"Analysis {analysis_id} produced no release zones, so there is nothing to simulate."
        )

    size = release_size or analysis.get("conditions", {}).get("release_size") or "medium"
    if size not in runout.RELEASE_SIZES:
        raise ValueError(
            f"Unknown release size {size!r}. Expected one of {', '.join(runout.RELEASE_SIZES)}."
        )

    engine = runout.get_engine(simulation_mode)
    simulation_id = simulation_id or f"SIM_{utc_now().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    output_dir = simulations_dir(settings) / simulation_id
    output_dir.mkdir(parents=True, exist_ok=True)

    results: list[runout.RunoutResult] = []
    for index, zone in enumerate(zones):
        report(
            20 + int(50 * index / max(len(zones), 1)),
            f"Simulating {zone.zone_id} ({index + 1}/{len(zones)})",
        )
        results.append(
            engine.simulate(
                zone=zone,
                grid=grid,
                elevation=terrain["elevation"],
                slope=terrain["slope"],
                forest_mask=terrain["forest_mask"],
                plan_curvature=terrain["plan_curvature"],
                config=config,
                release_size=size,
                seed=seed,
            )
        )

    report(75, "Merging simulated flow")
    reached = np.zeros(grid.shape, dtype=bool)
    envelope = np.zeros(grid.shape, dtype=bool)
    intensity = np.zeros(grid.shape, dtype="float32")
    velocity = np.zeros(grid.shape, dtype="float32")
    release_pixels = np.zeros(grid.shape, dtype=bool)

    for zone, result in zip(zones, results):
        reached |= result.reached
        envelope |= result.uncertainty
        intensity = np.maximum(intensity, result.intensity)
        velocity = np.maximum(velocity, result.velocity)
        release_pixels |= zone.pixels

    report(85, "Analysing exposure")
    exposure_result = exposure.analyze(
        settings=settings,
        config=config,
        grid=grid,
        reached=reached,
        intensity=intensity,
        velocity=velocity if velocity.any() else None,
        release_pixels=release_pixels,
    )

    report(92, "Scoring risk and confidence")
    hazard = float(
        np.mean([zone.properties["estimated_release_score"] for zone in zones])
    )

    core_area = float(reached.sum()) * grid.resolution_m**2
    envelope_area = float(envelope.sum()) * grid.resolution_m**2
    uncertainty_ratio = envelope_area / core_area if core_area > 0 else None

    stored_confidence = analysis.get("confidence_breakdown", {})
    conditions_payload = analysis.get("conditions", {})
    inputs = _rehydrate_inputs(conditions_payload)

    confidence, confidence_breakdown = risk.score_confidence(
        config=config,
        inputs=inputs,
        observation_age_hours=conditions_payload.get("observation_age_hours"),
        station_distance_km=conditions_payload.get("station_distance_km"),
        lidar_fraction=terrain.lidar_fraction,
        cloud_cover_percent=(analysis.get("satellite") or {}).get("cloud_cover_percent"),
        infrastructure_complete=False,
        instability_weight_fraction=float(
            (analysis.get("instability") or {}).get("available_weight_fraction", 0.0)
        ),
        simulation_uncertainty_ratio=uncertainty_ratio,
        is_scenario=analysis.get("mode") == "scenario",
    )

    reasons = _simulation_reasons(zones, exposure_result, analysis)
    limitations = _simulation_limitations(analysis, results, exposure_result, terrain)

    assessment = risk.assess(
        config=config,
        hazard_score=hazard,
        consequence_score=exposure_result.consequence_score,
        consequence_class=exposure_result.consequence_class,
        confidence_score=confidence,
        confidence_breakdown=confidence_breakdown,
        reasons=reasons,
        limitations=limitations,
        warnings=exposure_result.warnings,
    )

    report(96, "Writing simulation outputs")
    write_raster(output_dir / "runout_intensity.tif", np.ma.array(intensity, mask=~envelope), grid, build_overviews=False)
    render_preview(output_dir / "previews" / "runout_intensity.png", np.ma.array(intensity, mask=~envelope), "hazard", low=0, high=1)
    if velocity.any():
        write_raster(output_dir / "runout_velocity.tif", np.ma.array(velocity, mask=~envelope), grid, build_overviews=False)
        render_preview(output_dir / "previews" / "runout_velocity.png", np.ma.array(velocity, mask=~envelope), "thermal", low=0, high=float(velocity.max()))

    payload = {
        "schema_version": 1,
        "simulation_id": simulation_id,
        "analysis_id": analysis_id,
        "generated_at_utc": utc_now().isoformat(),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "simulation_mode": simulation_mode,
        "engine": engine.name,
        "release_size": size,
        "random_seed": seed if seed is not None else int(config.require("runout.advanced_mode.random_seed")),
        "zone_ids": [zone.zone_id for zone in zones],
        "model": config.provenance(),
        "software_version": settings.app_version,
        "grid": grid.describe(),
        "reproducibility": {
            "note": (
                "Re-running this simulation with the same analysis_id, zone_ids, mode, release "
                "size, seed and model version reproduces these results exactly."
            ),
            "analysis_id": analysis_id,
            "model_version": config.version,
            "config_sha256": config.sha256,
            "seed": seed if seed is not None else int(config.require("runout.advanced_mode.random_seed")),
        },
        "runout": {
            "core_area_m2": round(core_area, 1),
            "uncertainty_area_m2": round(envelope_area, 1),
            "uncertainty_ratio": round(uncertainty_ratio, 3) if uncertainty_ratio else None,
            "max_velocity_ms": round(float(velocity.max()), 2) if velocity.any() else None,
            "runout_polygons": [
                result.runout_polygon for result in results if result.runout_polygon
            ],
            "uncertainty_polygons": [
                result.uncertainty_polygon for result in results if result.uncertainty_polygon
            ],
            "main_paths": [result.main_path for result in results if result.main_path],
            "alternate_paths": [
                path for result in results for path in result.alternate_paths
            ],
            "stopping_points": [
                {"zone_id": result.zone_id, **point}
                for result in results
                for point in result.stopping_points[:5]
            ],
            "per_zone": [
                {"zone_id": result.zone_id, **result.metadata} for result in results
            ],
        },
        "exposure": exposure_result.to_dict(),
        "assessment": assessment.to_dict(),
        "zones": [zone.properties for zone in zones],
        "warnings": sorted(
            set(
                exposure_result.warnings
                + [warning for result in results for warning in result.warnings]
            )
        ),
        "disclaimer": DISCLAIMER,
        "is_probability": False,
        "is_official_forecast": False,
    }

    (output_dir / "simulation.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    report(100, "Simulation complete")
    return payload


def _rehydrate_zones(analysis: dict[str, Any], grid: AnalysisGrid) -> list[release_zones.ReleaseZone]:
    """Rebuild zone pixel masks by rasterizing the stored polygons."""
    from rasterio import features as rio_features
    from rasterio.warp import transform_geom

    zones: list[release_zones.ReleaseZone] = []
    collection = analysis.get("release_zones", {}) or {}
    for feature in collection.get("features", []):
        geometry = feature.get("geometry")
        if not geometry:
            continue
        utm = transform_geom("EPSG:4326", grid.crs_string, geometry)
        pixels = rio_features.rasterize(
            [(utm, 1)],
            out_shape=grid.shape,
            transform=grid.transform,
            fill=0,
            dtype="uint8",
        ).astype(bool)
        if not pixels.any():
            continue
        properties = feature.get("properties", {})
        zones.append(
            release_zones.ReleaseZone(
                zone_id=str(properties.get("zone_id") or feature.get("id")),
                geometry=geometry,
                geometry_utm=utm,
                properties=properties,
                pixels=pixels,
            )
        )
    return zones


def _rehydrate_inputs(conditions_payload: dict[str, Any]) -> list[prov.Value]:
    values: list[prov.Value] = []
    for name, item in (conditions_payload.get("values") or {}).items():
        try:
            values.append(
                prov.Value(
                    name=name,
                    value=item.get("value"),
                    units=item.get("units", ""),
                    provenance=item.get("provenance", "unavailable"),
                    source=item.get("source", ""),
                    timestamp_utc=item.get("timestamp_utc"),
                    detail=item.get("detail"),
                )
            )
        except prov.ProvenanceError:
            continue
    return values


def _simulation_reasons(
    zones: list[release_zones.ReleaseZone],
    exposure_result: exposure.ExposureResult,
    analysis: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    for zone in zones[:2]:
        reasons.extend(zone.properties.get("main_reasons", [])[:3])

    hit = exposure_result.summary.get("by_category", {})
    if hit:
        described = ", ".join(
            f"{item['exposed_count']} {item['title'].lower()}" for item in hit.values()
        )
        reasons.append(f"The simulated runout intersects {described}.")
    else:
        reasons.append(
            "The simulated runout does not intersect any asset in the (incomplete) infrastructure "
            "inventory."
        )

    wind_info = analysis.get("wind", {}) or {}
    if wind_info.get("available") and wind_info.get("transport_capacity", 0) > 0.3:
        reasons.append(
            f"Wind from {wind_info.get('wind_direction_from_deg', 0):.0f} degrees at "
            f"{wind_info.get('wind_speed_kmh', 0):.0f} km/h is loading the lee slopes."
        )
    return reasons


def _simulation_limitations(
    analysis: dict[str, Any],
    results: list[runout.RunoutResult],
    exposure_result: exposure.ExposureResult,
    terrain: TerrainBundle,
) -> list[str]:
    limitations = [
        "The runout model has not been calibrated against any observed Mount Hosmer avalanche. "
        "No historical avalanche record exists for this mountain.",
        "The model has no snow-profile data, so it cannot represent buried persistent weak layers "
        "-- the mechanism behind most avalanche fatalities.",
        "SWE and snow depth are modelled indices, not measurements. The nearest snow station is "
        "roughly 19 km away at a different elevation.",
    ]

    snow_info = analysis.get("snow", {}) or {}
    if snow_info.get("depth_provenance") == "modelled":
        limitations.append("Snow depth was modelled rather than measured.")
    if snow_info.get("swe_provenance") == "modelled":
        limitations.append("SWE was modelled rather than directly measured.")

    if analysis.get("mode") == "scenario":
        limitations.append(
            "This is a hypothetical scenario. The weather inputs were supplied by the user and were "
            "not observed."
        )

    if exposure_result.completeness.get("building_features_in_aoi", 0) <= 1:
        limitations.append(
            "Building coverage in OpenStreetMap for this area is incomplete, so the consequence "
            "score may understate what is actually exposed."
        )

    if terrain.lidar_fraction < 0.99:
        limitations.append(
            f"{1 - terrain.lidar_fraction:.1%} of the terrain is interpolated from a 30 m DEM "
            f"rather than 1 m LiDAR."
        )

    for result in results:
        if result.metadata.get("particles_still_moving_at_cutoff", 0) > 0:
            limitations.append(
                "Some simulated particles were still moving when the step limit was reached; the "
                "runout distance may be truncated."
            )
            break

    return limitations

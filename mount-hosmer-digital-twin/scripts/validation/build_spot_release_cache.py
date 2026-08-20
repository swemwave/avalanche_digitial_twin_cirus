"""Cache the SPOT 2018/2019 blocks for hourly-forcing release scoring.

Why this exists
---------------
``spot-blind-swiss-v1`` handed the release model a **scalar** wind:
``run_spot_blind_hindcast.py:417`` reduces the storm to
``mean(72 hours x 9 sample points)``. That is defect 1 in
``docs/release-engine-repair-plan.md``, and it is the mechanism behind the
frozen 0/40/0/0 zero-zone pattern. The configuration search that followed the
repair ran on ``regime-hindcast-v1``'s CERRA forcing, which never scalarizes
wind, so **the zero-zone condition was never re-tested on the forcing that
produced it**. This script caches what a re-test needs: the hourly
``(sample, hour)`` ERA5 field per block, alongside the same terrain, eligibility
and event rasterization the frozen SPOT scorer used.

Two things it does not do:

* It does not re-run, re-score or re-write ``spot-blind-swiss-v1``. That
  experiment's spec, artifacts and digests are frozen and are read only.
* It does not make a holdout number. The four SPOT 2019 blocks were scored and
  viewed in the frozen experiment, so every number computed from this cache is
  a **development** number and every consumer must label it one. The block ids
  are preserved verbatim so that provenance stays visible.

What the ERA5 payload does and does not carry
---------------------------------------------
The frozen request is five variables: ``temperature_2m``, ``precipitation``,
``snowfall``, ``wind_speed_10m``, ``wind_direction_10m``. There is no
``snow_depth`` and no ``shortwave_radiation``, both of which the CERRA payload
has. So a consumer of this cache must pass ``snow_depth_m=None`` and
``insolation=None`` rather than substitute a zero: an absent input stays absent.

``snowfall`` is cached as a **diagnostic only**. ``avycore.snowpack.state``
applies its rain/snow classification exactly once, to total precipitation, and
explicitly refuses to also consume a provider's pre-classified series. The
frozen v1 scalar ``new_snow_cm`` came from ``snowfall``; caching it keeps that
number auditable beside the hourly path without mixing the two.

Sample geometry
---------------
The frozen spec says "nine fixed quarter/mid/three-quarter points per core".
That is not :func:`avycore.snowpack.sample_lattice`'s sixth/mid/five-sixths
lattice, and the ordering is south-to-north, west-to-east. Both facts were
recovered by matching the payload's returned per-point elevations against the
Copernicus DEM (RMS 34-69 m, consistent with Open-Meteo's own 90 m DEM; the
alternatives are off by 286-805 m), and the reconstruction is re-checked here
against every payload so a silent geometry change cannot pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for _source in (
    REPOSITORY_ROOT / "packages" / "avycore" / "src",
    REPOSITORY_ROOT / "backend",
    Path(__file__).resolve().parent,
):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

import run_regime_hindcast as regime  # noqa: E402
import run_spot_blind_hindcast as legacy  # noqa: E402

SPEC_PATH = REPOSITORY_ROOT / "validation-data/experiments/spot-blind-swiss-v1.json"

#: Fractions of the fixed core at which the frozen nine ERA5 points sit.
CORE_SAMPLE_FRACTIONS = (0.25, 0.5, 0.75)

TERRAIN_LAYERS = (
    "elevation",
    "slope",
    "aspect",
    "general_curvature",
    "plan_curvature",
    "forest_mask",
)


def core_sample_points(block: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """The nine frozen sample coordinates, in the payload's own order."""
    west, south, east, north = (float(value) for value in block["core_grid"]["bounds"])
    eastings = [west + fraction * (east - west) for fraction in CORE_SAMPLE_FRACTIONS]
    northings = [south + fraction * (north - south) for fraction in CORE_SAMPLE_FRACTIONS]
    points = [(easting, northing) for northing in northings for easting in eastings]
    return (
        np.asarray([point[0] for point in points], dtype="float64"),
        np.asarray([point[1] for point in points], dtype="float64"),
    )


#: ERA5 cells are 0.25 degrees. A reconstructed point this close to a cell
#: boundary may be assigned to either side, because Open-Meteo's own LV95 ->
#: WGS84 transform is not bit-identical to pyproj's. 0.002 degrees is about
#: 220 m, an order below the 30 m grid's own 20.1 km block and far below the
#: 27.8 km cell, so it cannot absorb a genuinely wrong lattice.
CELL_BOUNDARY_TOLERANCE_DEG = 0.002

#: Agreement required between Open-Meteo's returned per-point elevation and the
#: warped Copernicus DEM at the reconstructed coordinate. The correct lattice
#: sits at 34-69 m RMS -- Open-Meteo serves a 90 m DEM, so exact agreement is
#: not expected -- while every rejected alternative lattice is 286-824 m out.
#: These bounds separate those two populations with room to spare and are not
#: tuned to any block.
ELEVATION_AGREEMENT_RMS_MAX_M = 150.0
ELEVATION_AGREEMENT_ABSOLUTE_MAX_M = 400.0


def _snap_disagreement_is_a_boundary_tie(
    reconstructed: float, returned: float
) -> bool:
    """True when the point sits on the boundary between the two candidate cells."""
    boundary = (round(reconstructed * 4.0) / 4.0 + returned) / 2.0
    return abs(reconstructed - boundary) <= CELL_BOUNDARY_TOLERANCE_DEG


def _check_sample_geometry(
    block: dict[str, Any],
    points: list[dict[str, Any]],
    east_m: np.ndarray,
    north_m: np.ndarray,
    *,
    elevation: np.ma.MaskedArray,
    grid: "legacy.ExperimentGrid",
) -> dict[str, Any]:
    """Confirm the reconstructed lattice is the one the frozen payload came from.

    Two independent checks, because neither alone is sufficient. The
    0.25-degree snap only proves the point landed in the right ERA5 cell, which
    a wrong lattice on a 20 km block usually also does. The elevation
    comparison is the discriminating one: Open-Meteo returns the elevation of
    the *requested* coordinate, so a mis-ordered or differently spaced lattice
    shows up as hundreds of metres of disagreement against the warped DEM.
    """
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
    longitude, latitude = transformer.transform(east_m, north_m)
    # Cell containing each sample, from the grid's own affine transform.
    columns = np.floor((east_m - grid.west) / grid.resolution_m).astype(int)
    rows = np.floor((grid.north - north_m) / grid.resolution_m).astype(int)
    described: list[dict[str, Any]] = []
    boundary_ties: list[int] = []
    differences: list[float] = []
    for index, point in enumerate(points):
        returned_latitude = float(point["latitude"])
        returned_longitude = float(point["longitude"])
        for axis, value, returned in (
            ("latitude", float(latitude[index]), returned_latitude),
            ("longitude", float(longitude[index]), returned_longitude),
        ):
            if round(value * 4.0) / 4.0 == returned:
                continue
            if _snap_disagreement_is_a_boundary_tie(value, returned):
                boundary_ties.append(index)
                continue
            raise ValueError(
                f"{block['block_id']} sample {index} reconstructs to {axis} "
                f"{value:.5f}, which snaps away from the frozen payload's "
                f"{returned}. The lattice reconstruction is wrong; do not cache "
                "a guessed geometry."
            )
        row, column = int(rows[index]), int(columns[index])
        if np.ma.getmaskarray(elevation)[row, column]:
            raise ValueError(
                f"{block['block_id']} sample {index} falls on a masked DEM cell."
            )
        warped_elevation = float(elevation[row, column])
        difference = warped_elevation - float(point["elevation"])
        differences.append(difference)
        described.append(
            {
                "east_m": float(east_m[index]),
                "north_m": float(north_m[index]),
                "latitude": float(latitude[index]),
                "longitude": float(longitude[index]),
                "returned_cell_latitude": returned_latitude,
                "returned_cell_longitude": returned_longitude,
                "returned_point_elevation_m": float(point["elevation"]),
                "warped_dem_elevation_m": warped_elevation,
                "elevation_difference_m": difference,
            }
        )
    error = np.asarray(differences, dtype="float64")
    rms = float(np.sqrt((error**2).mean()))
    absolute_maximum = float(np.abs(error).max())
    if rms > ELEVATION_AGREEMENT_RMS_MAX_M or absolute_maximum > (
        ELEVATION_AGREEMENT_ABSOLUTE_MAX_M
    ):
        raise ValueError(
            f"{block['block_id']} sample elevations disagree with the warped DEM "
            f"by {rms:.1f} m RMS / {absolute_maximum:.1f} m maximum. That is the "
            "signature of a wrong sample lattice, not of a DEM resolution "
            "difference."
        )
    return {
        "lattice": "core quarter/mid/three-quarter, south to north, west to east",
        "verified_by": [
            "returned 0.25-degree ERA5 cell of every payload point",
            "returned per-point elevation against the warped Copernicus DEM",
        ],
        "elevation_agreement_rms_m": rms,
        "elevation_agreement_absolute_maximum_m": absolute_maximum,
        "elevation_agreement_rms_bound_m": ELEVATION_AGREEMENT_RMS_MAX_M,
        "elevation_agreement_absolute_bound_m": ELEVATION_AGREEMENT_ABSOLUTE_MAX_M,
        "cell_boundary_tie_sample_indices": sorted(set(boundary_ties)),
        "cell_boundary_tolerance_deg": CELL_BOUNDARY_TOLERANCE_DEG,
        "points": described,
    }


def _frozen_scalars(storm: dict[str, np.ndarray]) -> dict[str, float]:
    """The two scalars ``spot-blind-swiss-v1`` actually fed the model.

    Recomputed from the same payload so the hourly path can be reported beside
    the number it replaces, without reopening the frozen artifact.
    """
    snowfall = storm["snowfall"]
    speeds = storm["wind_speed_10m"]
    return {
        "new_snow_cm_frozen_scalar": float(np.mean(snowfall.sum(axis=1))),
        "wind_speed_kmh_frozen_scalar_mean": float(speeds.mean()),
        "wind_speed_kmh_maximum_hour": float(speeds.max()),
        "wind_speed_kmh_p95": float(np.quantile(speeds, 0.95)),
        "transporting_hour_fraction_at_dry_threshold": float(
            np.mean(speeds / 3.6 > 7.7)
        ),
    }


def _forcing_payload(
    block: dict[str, Any],
    path: Path,
    *,
    elevation: np.ma.MaskedArray,
    grid: "legacy.ExperimentGrid",
) -> dict[str, Any]:
    """Stack the nine ERA5 series into ``(sample, hour)`` matrices."""
    payload = json.loads(path.read_bytes())
    points = payload if isinstance(payload, list) else [payload]
    east_m, north_m = core_sample_points(block)
    if len(points) != east_m.size:
        raise ValueError(f"{block['block_id']} forcing point count changed.")
    geometry = _check_sample_geometry(
        block, points, east_m, north_m, elevation=elevation, grid=grid
    )

    times = tuple(points[0]["hourly"]["time"])
    for point in points:
        if tuple(point["hourly"]["time"]) != times:
            raise ValueError("Forcing sample points do not share one timeline.")

    def stack(name: str) -> np.ndarray:
        values = np.asarray([point["hourly"][name] for point in points], dtype="float64")
        if not np.isfinite(values).all():
            raise ValueError(f"{block['block_id']} {name} contains a missing hour.")
        return values

    variables = {
        name: stack(name)
        for name in (
            "temperature_2m",
            "precipitation",
            "snowfall",
            "wind_speed_10m",
            "wind_direction_10m",
        )
    }

    window = block["storm_window"]
    start, end = str(window["start_utc"]), str(window["end_utc"])
    storm_hours = [index for index, stamp in enumerate(times) if start < stamp <= end]
    if len(storm_hours) != int(window["hour_count"]):
        raise ValueError(
            f"{block['block_id']} has {len(storm_hours)} storm hours, not the frozen count."
        )
    # Everything the payload holds up to the frozen window end. Hours at or
    # before start_utc are antecedent; hours after end_utc lie outside the
    # frozen window and are dropped rather than quietly extending it.
    selected = [index for index, stamp in enumerate(times) if stamp <= end]

    return {
        "times": tuple(times[index] for index in selected),
        "antecedent_hour_count": len(selected) - len(storm_hours),
        "storm_hour_count": len(storm_hours),
        "sample_east_m": east_m,
        "sample_north_m": north_m,
        "sample_geometry": geometry,
        "sample_elevation_m": np.asarray(
            [float(point["elevation"]) for point in points], dtype="float64"
        ),
        "latitude_deg": float(np.mean([float(p["latitude"]) for p in points])),
        "longitude_deg": float(np.mean([float(p["longitude"]) for p in points])),
        "air_temperature_c": variables["temperature_2m"][:, selected],
        "precipitation_mm": variables["precipitation"][:, selected],
        "wind_speed_10m_kmh": variables["wind_speed_10m"][:, selected],
        "wind_from_direction_deg": variables["wind_direction_10m"][:, selected],
        "diagnostic_snowfall_cm": variables["snowfall"][:, selected],
        "frozen_scalar_diagnostics": _frozen_scalars(
            {name: values[:, storm_hours] for name, values in variables.items()}
        ),
    }


def _nearest_sample_index(
    grid: legacy.ExperimentGrid, east_m: np.ndarray, north_m: np.ndarray
) -> np.ndarray:
    """Same nearest-sample assignment ``ForcingSampleGrid`` performs."""
    east, north = regime._cell_centres(grid)
    distance = (east[..., None] - east_m[None, None, :]) ** 2 + (
        north[..., None] - north_m[None, None, :]
    ) ** 2
    return np.argmin(distance, axis=-1).astype("int16")


def build_block(
    block: dict[str, Any],
    spec: dict[str, Any],
    sources: dict[str, Path],
    output_dir: Path,
    *,
    outline_shapefile_source_id: str,
) -> Path:
    terrain, terrain_meta, core = legacy._terrain(block, spec, sources)
    # SPOT's own eligibility: complete required inputs inside the fixed core.
    # It has no acquisition-coverage layer -- the archives ship no
    # machine-readable footprint polygon -- so this is exactly the mask the
    # frozen scorer used, not a stricter or looser one.
    eligible = np.logical_and.reduce(
        [~np.ma.getmaskarray(terrain.layer(name)) for name in TERRAIN_LAYERS]
    )[core]

    forcing = _forcing_payload(
        block,
        sources[block["meteorology_source_id"]],
        elevation=terrain.layer("elevation"),
        grid=terrain.grid,
    )
    sample_index = _nearest_sample_index(
        terrain.grid, forcing["sample_east_m"], forcing["sample_north_m"]
    )[core]

    layers: dict[str, np.ndarray] = {}
    for name in TERRAIN_LAYERS:
        layer = terrain.layer(name)
        layers[f"layer_{name}"] = np.asarray(layer.filled(0.0), dtype="float32")[core]
        layers[f"mask_{name}"] = np.ma.getmaskarray(layer)[core]

    # Targets. These outlines were already opened and scored by the frozen
    # experiment, which is precisely why everything downstream is development.
    shapefile = sources[outline_shapefile_source_id]
    event_masks, event_ids, geometry_complete, attributes, target_meta = (
        legacy._target_events(shapefile, block)
    )
    offsets = [0]
    flat_indices: list[np.ndarray] = []
    for mask in event_masks:
        indices = np.flatnonzero(mask.reshape(-1)).astype("int32")
        flat_indices.append(indices)
        offsets.append(offsets[-1] + indices.size)

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{block['block_id']}.npz"
    np.savez_compressed(
        path,
        eligible=eligible,
        sample_index=sample_index,
        event_flat_indices=(
            np.concatenate(flat_indices) if flat_indices else np.zeros(0, "int32")
        ),
        event_offsets=np.asarray(offsets, dtype="int64"),
        event_geometry_complete=np.asarray(geometry_complete, dtype=bool),
        forcing_air_temperature_c=forcing["air_temperature_c"],
        forcing_precipitation_mm=forcing["precipitation_mm"],
        forcing_wind_speed_10m_kmh=forcing["wind_speed_10m_kmh"],
        forcing_wind_from_direction_deg=forcing["wind_from_direction_deg"],
        forcing_sample_elevation_m=forcing["sample_elevation_m"],
        diagnostic_snowfall_cm=forcing["diagnostic_snowfall_cm"],
        **layers,
        metadata_json=np.asarray(
            json.dumps(
                {
                    "block_id": block["block_id"],
                    "source_experiment_id": spec["experiment_id"],
                    "source_partition": block["partition"],
                    "partition_for_this_cache": "development_burned",
                    "burned_reason": (
                        "Every SPOT block was scored and viewed in the frozen "
                        "spot-blind-swiss-v1 experiment. Nothing computed from "
                        "this cache is a holdout number."
                    ),
                    "campaign_year": block["campaign_year"],
                    "mountain_group": block["mountain_group"],
                    "core_grid": block["core_grid"],
                    "core_window_rowcol": list(block["core_window_rowcol"]),
                    "resolution_m": float(block["core_grid"]["resolution_m"]),
                    "storm_window": block["storm_window"],
                    "forcing_times_utc": list(forcing["times"]),
                    "antecedent_hour_count": forcing["antecedent_hour_count"],
                    "storm_hour_count": forcing["storm_hour_count"],
                    "sample_geometry": forcing["sample_geometry"],
                    "frozen_scalar_diagnostics": forcing["frozen_scalar_diagnostics"],
                    "absent_forcing_variables": {
                        "snow_depth_m": (
                            "not in the frozen ERA5 request; pass None, never zero"
                        ),
                        "shortwave_radiation_w_m2": (
                            "not in the frozen ERA5 request; insolation is None"
                        ),
                    },
                    "snowfall_is_diagnostic_only": True,
                    "latitude_deg": forcing["latitude_deg"],
                    "longitude_deg": forcing["longitude_deg"],
                    "event_ids": event_ids,
                    "target_selection": target_meta,
                    "event_attributes": attributes,
                    "capture_minimum_overlap_fraction_frozen": float(
                        spec["metrics"]["event_capture_minimum_overlap_fraction"]
                    ),
                    "terrain_artifact_sha256": terrain_meta["terrain_artifact_sha256"],
                    "core_complete_input_fraction": terrain_meta[
                        "core_complete_input_fraction"
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--spot-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--partition",
        choices=("development", "holdout", "all"),
        default="all",
        help=(
            "SPOT partition to cache. Both are burned: the frozen experiment "
            "scored and reported all five blocks."
        ),
    )
    args = parser.parse_args()

    spec = legacy._load_json(args.spec.resolve())
    sources = legacy._verify_sources(
        spec,
        source_root=args.source_root.resolve(),
        spot_root=args.spot_root.resolve(),
        include_evaluation_targets=True,
    )
    partitions = (
        ("development", "holdout") if args.partition == "all" else (args.partition,)
    )
    for partition in partitions:
        # The outline source is a property of the campaign, not the block: 2018
        # for development, 2019 for the four burned holdout mountains.
        shapefile_id = str(spec["partitions"][partition]["outline_shapefile_source_id"])
        for block in legacy._partition_blocks(spec, partition):
            path = build_block(
                block,
                spec,
                sources,
                args.output_dir.resolve(),
                outline_shapefile_source_id=shapefile_id,
            )
            print(json.dumps({"block_id": block["block_id"], "cache": str(path)}))


if __name__ == "__main__":
    main()

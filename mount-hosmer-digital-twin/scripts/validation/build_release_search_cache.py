"""Cache everything a release-configuration sweep needs, per development block.

The sweep evaluates hundreds of configurations. Re-warping the DEM, re-reading
GlobCover, and re-rasterizing thousands of outlines for each one would dominate
the run, so this script does all of that once and writes a compact NPZ per
block.

Two deliberate restrictions:

* **Development blocks only.** ``--partition`` accepts ``development``. The four
  reserved 1999 lattice blocks are not addressable here at all; the block list
  comes from the frozen specification's development partition.
* **Core window only.** Every array is cropped to the fixed evaluation core.
  That is exact rather than an approximation: the regime release path masks
  every cell outside ``supported`` (the core), so no candidate cell, no
  curvature percentile, and no morphological structuring element can reach
  halo terrain. The halo exists for runout, which the release search does not
  simulate.

The cached target rasterization is the same one the frozen scorer uses --
``_target_events`` from the SPOT runner -- so a capture number computed from
this cache is the number the frozen scorer would report for the same mask.
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
from avycore.snowpack import sample_lattice  # noqa: E402

SPEC_PATH = REPOSITORY_ROOT / "validation-data/experiments/regime-hindcast-v1.json"


def _forcing_payload(block: dict[str, Any], path: Path) -> dict[str, Any]:
    """Stack the nine sample series into matrices, preserving order and units."""
    payload = json.loads(path.read_bytes())
    points = payload if isinstance(payload, list) else [payload]
    east, north = sample_lattice(
        west_m=float(block["simulation_grid"]["bounds"][0]),
        south_m=float(block["simulation_grid"]["bounds"][1]),
        east_m=float(block["simulation_grid"]["bounds"][2]),
        north_m=float(block["simulation_grid"]["bounds"][3]),
        count_per_axis=3,
    )
    if len(points) != len(east):
        raise ValueError(f"{block['block_id']} forcing point count changed.")
    times = tuple(points[0]["hourly"]["time"])
    for point in points:
        if tuple(point["hourly"]["time"]) != times:
            raise ValueError("Forcing sample points do not share one timeline.")

    def stack(name: str) -> np.ndarray:
        values = np.asarray(
            [point["hourly"][name] for point in points], dtype="float64"
        )
        if not np.isfinite(values).all():
            raise ValueError(f"{block['block_id']} {name} contains a missing hour.")
        return values

    return {
        "times": times,
        "sample_east_m": east,
        "sample_north_m": north,
        "sample_elevation_m": np.asarray(
            [float(point["elevation"]) for point in points], dtype="float64"
        ),
        "latitude_deg": float(np.mean([float(p["latitude"]) for p in points])),
        "longitude_deg": float(np.mean([float(p["longitude"]) for p in points])),
        "air_temperature_c": stack("temperature_2m"),
        "precipitation_mm": stack("precipitation"),
        "wind_speed_10m_kmh": stack("wind_speed_10m"),
        "wind_from_direction_deg": stack("wind_direction_10m"),
        "snow_depth_m": stack("snow_depth"),
        "shortwave_radiation_w_m2": stack("shortwave_radiation"),
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
) -> Path:
    terrain, terrain_meta, core = legacy._terrain(block, spec, sources)
    coverage = regime._coverage_mask(block, sources, terrain.grid)
    complete = np.logical_and.reduce(
        [
            ~np.ma.getmaskarray(terrain.layer(name))
            for name in (
                "elevation",
                "slope",
                "aspect",
                "general_curvature",
                "plan_curvature",
                "forest_mask",
            )
        ]
    )
    eligible = (complete & coverage)[core]

    forcing = _forcing_payload(block, sources[block["meteorology_source_id"]])
    sample_index = _nearest_sample_index(
        terrain.grid, forcing["sample_east_m"], forcing["sample_north_m"]
    )[core]

    layers: dict[str, np.ndarray] = {}
    for name in (
        "elevation",
        "slope",
        "aspect",
        "general_curvature",
        "plan_curvature",
        "forest_mask",
    ):
        layer = terrain.layer(name)
        layers[f"layer_{name}"] = np.asarray(layer.filled(0.0), dtype="float32")[core]
        layers[f"mask_{name}"] = np.ma.getmaskarray(layer)[core]

    # Targets. This is development data whose outlines have already been scored
    # and viewed; the reserved 1999 blocks are never reachable from here.
    shapefile = sources[block["outline_shapefile_source_id"]]
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
        event_flat_indices=np.concatenate(flat_indices) if flat_indices else np.zeros(0, "int32"),
        event_offsets=np.asarray(offsets, dtype="int64"),
        event_geometry_complete=np.asarray(geometry_complete, dtype=bool),
        forcing_air_temperature_c=forcing["air_temperature_c"],
        forcing_precipitation_mm=forcing["precipitation_mm"],
        forcing_wind_speed_10m_kmh=forcing["wind_speed_10m_kmh"],
        forcing_wind_from_direction_deg=forcing["wind_from_direction_deg"],
        forcing_snow_depth_m=forcing["snow_depth_m"],
        forcing_shortwave_radiation_w_m2=forcing["shortwave_radiation_w_m2"],
        forcing_sample_elevation_m=forcing["sample_elevation_m"],
        **layers,
        metadata_json=np.asarray(
            json.dumps(
                {
                    "block_id": block["block_id"],
                    "partition": block["partition"],
                    "campaign_year": block["campaign_year"],
                    "core_grid": block["core_grid"],
                    "core_window_rowcol": list(block["core_window_rowcol"]),
                    "resolution_m": float(block["core_grid"]["resolution_m"]),
                    "storm_cycles": block["storm_cycles"],
                    "forcing_times_utc": list(forcing["times"]),
                    "latitude_deg": forcing["latitude_deg"],
                    "longitude_deg": forcing["longitude_deg"],
                    "event_ids": event_ids,
                    "target_selection": target_meta,
                    "event_attributes": attributes,
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
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--partition", choices=("development",), default="development",
        help="Development only. The reserved blocks are not addressable here.",
    )
    args = parser.parse_args()

    spec = regime._load_json(args.spec.resolve())
    sources = regime._verify_sources(
        spec,
        source_root=args.source_root.resolve(),
        target_root=args.target_root.resolve(),
        include_targets=True,
    )
    for block in regime._partition_blocks(spec, args.partition):
        path = build_block(block, spec, sources, args.output_dir.resolve())
        print(json.dumps({"block_id": block["block_id"], "cache": str(path)}))


if __name__ == "__main__":
    main()

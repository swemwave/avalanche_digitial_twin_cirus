"""Acquire and identity the public inputs for the regime hindcast.

The script downloads immutable source files into caller-supplied directories;
it never writes ``DATA/`` or ``runtime/``. CERRA is requested through the
Open-Meteo Historical Weather API with elevation downscaling disabled and
nearest-cell selection. The returned native grid coordinates are retained in
the raw JSON, and every byte used by the experiment is SHA-256 identified.

Example::

    python scripts/validation/acquire_regime_hindcast_data.py \
      --source-root C:/temp/hosmer-regime-sources \
      --target-root C:/temp/hosmer-regime-targets \
      --holdout-blocks validation-data/experiments/regime-hindcast-v1-holdout-blocks.json \
      --manifest validation-data/experiments/regime-hindcast-v1-acquisition.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from pyproj import Transformer

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OPEN_METEO_ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"
CERRA_VARIABLES = (
    "temperature_2m",
    "precipitation",
    "snowfall",
    "snow_depth",
    "wind_speed_10m",
    "wind_direction_10m",
    "shortwave_radiation",
)
# The public API accepts at most ten simultaneous locations. A fixed 3x3
# interior lattice provides nine native-cell samples per 30.3 km simulation
# tile; the native 5.5 km product resolution is reported unchanged and no
# interpolation is performed between samples.
SAMPLE_COUNT_PER_AXIS = 3

ARCHIVES = {
    "spot_2018": {
        "url": (
            "https://www.envidat.ch/dataset/fa4adf13-d0e5-4479-9b46-cbb07233999f/"
            "resource/309d5260-f13e-4fd1-881e-8968c829941b/download/"
            "aval_outlines2018.zip"
        ),
        "name": "aval_outlines2018.zip",
        "bytes": 157_117_977,
        "sha256": "087c036f1a3e4213c2332fad4497fd292c6af6f0df1629c5aaa887a45387c2f5",
        "doi": "10.16904/envidat.77",
        "licence": "ODbL with Database Contents License (DbCL)",
        "extract_directory": "2018",
    },
    "spot_2019": {
        "url": (
            "https://www.envidat.ch/dataset/cb197291-bbfa-4d03-a24a-4f9336ecd9af/"
            "resource/33cb7780-261b-4228-8528-bf5d84f3955e/download/"
            "aval_outlines16012019.shp.zip"
        ),
        "name": "aval_outlines16012019.shp.zip",
        "bytes": 56_256_600,
        "sha256": "af4099d949fb567c0bc07b3e46cbca40e6b8f7c4340a6fbe7311e2104e93251f",
        "doi": "10.16904/envidat.235",
        "licence": "CC BY 4.0",
        "extract_directory": "2019",
    },
    "aerial_1999": {
        "url": (
            "https://www.envidat.ch/dataset/ac52bb46-c042-429b-83e2-feb5f397db99/"
            "resource/a86944ff-6bb2-4b9d-b7d2-26b4571be620/download/"
            "avalanche_data_1999_all.zip"
        ),
        "name": "avalanche_data_1999_all.zip",
        "bytes": 150_084_348,
        "sha256": "7a456616f8dfd01c39c8a7b945abf9ebe46436f084b4c13a7f09f2651fd64427",
        "doi": "10.16904/envidat.579",
        "licence": "CC BY-SA 4.0",
        "extract_directory": "1999",
    },
    "globcover_2009": {
        "url": "https://due.esrin.esa.int/files/Globcover2009_V2.3_Global_.zip",
        "name": "Globcover2009_V2.3_Global_.zip",
        "bytes": 380_992_056,
        "sha256": "3a5e46b589f6b650759308d4ccb2d62d906a8ffc6f44c6595545e18702a3f7c6",
        "licence": "ESA GlobCover product terms; attribution required",
        "extract_directory": "globcover2009",
    },
}

DEM_TILES = tuple(
    f"Copernicus_DSM_COG_10_N{latitude:02d}_00_E{longitude:03d}_00_DEM"
    for latitude in (46, 47)
    for longitude in range(6, 11)
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(path: Path, **metadata: Any) -> dict[str, Any]:
    return {
        **metadata,
        "path": path.as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _download(url: str, destination: Path) -> None:
    if destination.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "avycore-validation/1"})
    with urllib.request.urlopen(request, timeout=300) as response, temporary.open("wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)
    temporary.replace(destination)


def _verify(path: Path, expected_bytes: int, expected_sha256: str) -> None:
    if path.stat().st_size != expected_bytes or _sha256_file(path) != expected_sha256:
        raise ValueError(f"Downloaded identity mismatch for {path}.")


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = (destination / member.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"Unsafe ZIP member {member.filename!r}.")
        source.extractall(destination)


def _acquire_archives(source_root: Path, target_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source_id, definition in ARCHIVES.items():
        root = source_root if source_id == "globcover_2009" else target_root
        archive = root / definition["name"]
        _download(definition["url"], archive)
        _verify(archive, definition["bytes"], definition["sha256"])
        extraction = root / definition["extract_directory"]
        if not extraction.exists() or not any(extraction.iterdir()):
            _safe_extract(archive, extraction)
        if source_id == "aerial_1999":
            nested = extraction / "avalanche_data_1999.zip"
            _verify(
                nested,
                148_108_558,
                "43b1203e812bfb69a448944fcd0ca2e75763d0052ee78fc41306f9b44bc98c02",
            )
            if not (extraction / "avalanches1999_endversion1.shp").is_file():
                _safe_extract(nested, extraction)
        records.append(
            _record(
                archive,
                source_id=source_id,
                role=("landcover" if source_id == "globcover_2009" else "evaluation"),
                url=definition["url"],
                doi=definition.get("doi"),
                licence=definition["licence"],
            )
        )
        extracted_sources = {
            "spot_2018": ("outlines2018", "spot_2018"),
            "spot_2019": ("aval_outlines16012019", "spot_2019"),
            "aerial_1999": ("avalanches1999_endversion1", "spot_1999"),
        }
        if source_id in extracted_sources:
            stem, prefix = extracted_sources[source_id]
            for member in sorted(extraction.glob(f"{stem}.*")):
                records.append(
                    _record(
                        member,
                        source_id=f"{prefix}_{member.suffix.removeprefix('.').lower()}",
                        role="evaluation_target_vector_component",
                        extracted_from=source_id,
                        crs="EPSG:2056",
                    )
                )
        if source_id == "aerial_1999":
            for stem, prefix, role in (
                ("area_images_1999_all", "coverage_1999", "acquisition_footprint"),
                ("Clouds_1999", "cloud_1999", "cloud_exclusion"),
            ):
                for member in sorted(extraction.glob(f"{stem}.*")):
                    records.append(
                        _record(
                            member,
                            source_id=f"{prefix}_{member.suffix.removeprefix('.').lower()}",
                            role=role,
                            extracted_from=source_id,
                            crs="EPSG:2056",
                        )
                    )
        if source_id == "globcover_2009":
            landcover = extraction / "GLOBCOVER_L4_200901_200912_V2.3.tif"
            records.append(
                _record(
                    landcover,
                    source_id="globcover_2009_tif",
                    role="landcover",
                    extracted_from=source_id,
                    native_crs="EPSG:4326",
                    native_resolution="300 m",
                    unit="categorical land-cover class",
                )
            )
    return records


def _acquire_dem(source_root: Path) -> list[dict[str, Any]]:
    destination = source_root / "copernicus-dem-30m"
    records: list[dict[str, Any]] = []
    for tile_id in DEM_TILES:
        url = f"https://copernicus-dem-30m.s3.amazonaws.com/{tile_id}/{tile_id}.tif"
        path = destination / f"{tile_id}.tif"
        _download(url, path)
        records.append(
            _record(
                path,
                source_id=tile_id,
                role="terrain_elevation",
                url=url,
                product="Copernicus DEM GLO-30 Public",
                product_edition="2021",
                licence="Copernicus DEM licence; free licence terms",
                native_crs="EPSG:4326",
                native_horizontal_resolution="1 arc-second (~30 m)",
                unit="metre",
            )
        )
    return records


def _sample_coordinates(bounds: list[float]) -> tuple[list[float], list[float]]:
    west, south, east, north = bounds
    fractions = [
        (2 * index + 1) / (2 * SAMPLE_COUNT_PER_AXIS)
        for index in range(SAMPLE_COUNT_PER_AXIS)
    ]
    projected = [
        (west + x * (east - west), north - y * (north - south))
        for y in fractions
        for x in fractions
    ]
    transform = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
    lon, lat = transform.transform(
        [point[0] for point in projected], [point[1] for point in projected]
    )
    return [round(float(value), 6) for value in lat], [round(float(value), 6) for value in lon]


def _cerra_url(block: dict[str, Any]) -> str:
    latitude, longitude = _sample_coordinates(block["simulation_grid"]["bounds"])
    query = {
        "models": "cerra",
        "timezone": "GMT",
        "start_date": block["forcing_period"]["start_date"],
        "end_date": block["forcing_period"]["end_date"],
        "hourly": ",".join(CERRA_VARIABLES),
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
        "cell_selection": "nearest",
        "elevation": ",".join("nan" for _ in latitude),
        "latitude": ",".join(f"{value:.6f}" for value in latitude),
        "longitude": ",".join(f"{value:.6f}" for value in longitude),
    }
    return OPEN_METEO_ENDPOINT + "?" + urllib.parse.urlencode(query, safe=",")


def _validate_cerra(path: Path, expected_points: int) -> None:
    payload = json.loads(path.read_bytes())
    points = payload if isinstance(payload, list) else [payload]
    if len(points) != expected_points:
        raise ValueError(f"{path} returned {len(points)} points, expected {expected_points}.")
    for point in points:
        units = point["hourly_units"]
        expected_units = {
            "temperature_2m": "°C",
            "precipitation": "mm",
            "snowfall": "cm",
            "snow_depth": "m",
            "wind_speed_10m": "km/h",
            "wind_direction_10m": "°",
            "shortwave_radiation": "W/m²",
        }
        if any(units[name] != unit for name, unit in expected_units.items()):
            raise ValueError(f"Unexpected CERRA units in {path}.")
        hourly = point["hourly"]
        for name in CERRA_VARIABLES:
            if any(value is None for value in hourly[name]):
                raise ValueError(f"{path} contains missing {name}; gaps are not filled.")


def _acquire_cerra(source_root: Path, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    destination = source_root / "cerra-open-meteo"
    records: list[dict[str, Any]] = []
    for block in blocks:
        url = _cerra_url(block)
        path = destination / f"{block['block_id']}.json"
        _download(url, path)
        _validate_cerra(path, SAMPLE_COUNT_PER_AXIS**2)
        records.append(
            _record(
                path,
                source_id=f"cerra_{block['block_id']}",
                role="meteorology_and_modelled_snow_state",
                url=url,
                product="CERRA single levels via Open-Meteo Historical Weather API",
                product_version="CDS DOI 10.24381/cds.622a565a; API response retrieved 2026-08-13",
                licence="CC BY 4.0",
                native_crs="Lambert conformal conic; API returns native-cell WGS84 coordinates",
                native_resolution="5.5 km; sampled without interpolation",
                temporal_resolution="hourly",
                units={
                    "temperature_2m": "degC instantaneous",
                    "precipitation": "mm preceding-hour sum",
                    "snowfall": "cm preceding-hour sum; diagnostic only",
                    "snow_depth": "m instantaneous; modelled grid-cell state",
                    "wind_speed_10m": "km/h instantaneous",
                    "wind_direction_10m": "meteorological FROM degrees clockwise from north",
                    "shortwave_radiation": "W/m2 preceding-hour mean",
                },
                transformations=(
                    "Open-Meteo elevation=nan disables elevation downscaling; "
                    "cell_selection=nearest; 3x3 interior LV95 sample lattice transformed "
                    "with always_xy into WGS84 request coordinates."
                ),
                missing_rule="Any null required hour invalidates the block; no gap filling.",
            )
        )
    return records


def _development_blocks(old_spec: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [
        *old_spec["partitions"]["development"]["blocks"],
        *old_spec["partitions"]["holdout"]["blocks"],
    ]
    result = []
    for original in blocks:
        block = dict(original)
        block["block_id"] = "development_" + original["block_id"].removeprefix("holdout_").removeprefix("dev_")
        if original["campaign_year"] == 2018:
            block["forcing_period"] = {"start_date": "2018-01-14", "end_date": "2018-01-24"}
            cycles = [{"cycle_id": "2018-01", "antecedent_start_exclusive_utc": "2018-01-13T23:00", **original["storm_window"]}]
        else:
            block["forcing_period"] = {"start_date": "2019-01-06", "end_date": "2019-01-16"}
            cycles = [{"cycle_id": "2019-01", "antecedent_start_exclusive_utc": "2019-01-05T23:00", **original["storm_window"]}]
        block["storm_cycles"] = cycles
        result.append(block)
    return result


def _holdout_blocks(selection: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    cycles = [
        {"cycle_id": "1999-cycle-1", "antecedent_start_exclusive_utc": "1999-01-18T23:00", "start_utc": "1999-01-25T23:00", "end_utc": "1999-01-30T06:00"},
        {"cycle_id": "1999-cycle-2", "antecedent_start_exclusive_utc": "1999-01-29T23:00", "start_utc": "1999-02-04T23:00", "end_utc": "1999-02-11T06:00"},
        {"cycle_id": "1999-cycle-3", "antecedent_start_exclusive_utc": "1999-02-10T23:00", "start_utc": "1999-02-16T23:00", "end_utc": "1999-02-25T06:00"},
    ]
    for selected in selection["blocks"]:
        block = dict(selected)
        block["campaign_year"] = 1999
        block["forcing_period"] = {"start_date": "1999-01-19", "end_date": "1999-02-25"}
        block["storm_cycles"] = cycles
        result.append(block)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--holdout-blocks", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    target_root = args.target_root.resolve()
    if "DATA" in {part.upper() for part in source_root.parts + target_root.parts}:
        raise ValueError("Validation acquisition refuses to write anywhere under DATA/.")
    selection = json.loads(args.holdout_blocks.read_bytes())
    old_spec = json.loads(
        (REPOSITORY_ROOT / "validation-data/experiments/spot-blind-swiss-v1.json").read_bytes()
    )
    development = _development_blocks(old_spec)
    holdout = _holdout_blocks(selection)
    records = _acquire_archives(source_root, target_root)
    records.extend(_acquire_dem(source_root))
    records.extend(_acquire_cerra(source_root, development + holdout))
    manifest = {
        "schema": "avycore-regime-hindcast-acquisition-v1",
        "retrieval_date_utc": "2026-08-13",
        "source_root": source_root.as_posix(),
        "target_root": target_root.as_posix(),
        "development_blocks": development,
        "holdout_blocks": holdout,
        "source_files": records,
        "lineage_notes": [
            "The 2018/2019 SPOT campaigns and their previously viewed results are development only.",
            "The 1999 outlines are evaluation targets only and are not opened by prediction.",
            "CERRA snow depth is a modelled grid-cell state, not a field observation or snow profile.",
            "The CERRA API artefacts are byte-identified because upstream historical APIs can be revised.",
        ],
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"manifest": str(args.manifest), "source_count": len(records)}))


if __name__ == "__main__":
    main()

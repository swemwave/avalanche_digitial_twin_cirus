"""Deployment health gate for the public Mount Hosmer research prototype.

The gate checks availability and provenance, not physical accuracy.  It never
prints or interprets the release-potential value returned by an assessment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class HealthGateError(RuntimeError):
    """A required deployment property was unavailable or inconsistent."""


@dataclass(frozen=True)
class GateSummary:
    base_url: str
    bake_sha256: str
    schema: str
    frontend_ok: bool
    health_ok: bool
    imagery_ok: bool
    assessment_ok: bool


def _request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_s: float = 90.0,
) -> tuple[int, bytes, str]:
    body = None
    headers = {"User-Agent": "mount-hosmer-deployment-gate/1"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - URL is operator supplied.
            return response.status, response.read(), response.headers.get_content_type()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise HealthGateError(f"{method} {url} failed: {exc}") from exc


def _json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    status, body, content_type = _request(url, method=method, payload=payload)
    if status != 200:
        raise HealthGateError(f"{method} {url} returned HTTP {status}.")
    if content_type != "application/json":
        raise HealthGateError(f"{method} {url} returned {content_type!r}, not JSON.")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HealthGateError(f"{method} {url} returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise HealthGateError(f"{method} {url} returned a non-object JSON value.")
    return value


def _tile_xy(longitude: float, latitude: float, zoom: int) -> tuple[int, int]:
    latitude = max(-85.05112878, min(85.05112878, latitude))
    scale = 2**zoom
    x = int((longitude + 180.0) / 360.0 * scale)
    latitude_rad = math.radians(latitude)
    y = int((1.0 - math.asinh(math.tan(latitude_rad)) / math.pi) / 2.0 * scale)
    return x, y


def _tile_url(base_url: str, template: str, *, zoom: int, x: int, y: int) -> str:
    path = template.replace("{z}", str(zoom)).replace("{x}", str(x)).replace("{y}", str(y))
    return urljoin(f"{base_url}/", path.lstrip("/"))


def verify(base_url: str, expected_bake_sha256: str | None = None) -> GateSummary:
    base_url = base_url.rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HealthGateError("Base URL must be an absolute HTTP(S) URL.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HealthGateError("A non-local deployment gate must use HTTPS.")
    if expected_bake_sha256 is not None and (
        len(expected_bake_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_bake_sha256)
    ):
        raise HealthGateError("Expected bake identity must be a lowercase SHA-256 hex digest.")

    root_status, root_body, root_type = _request(f"{base_url}/")
    root_text = root_body.decode("utf-8", errors="replace")
    if root_status != 200 or root_type != "text/html":
        raise HealthGateError("Frontend root did not return HTTP 200 HTML.")
    if "Mount Hosmer" not in root_text or "Experimental and non-operational" not in root_text:
        raise HealthGateError("Frontend root is missing the project identity or non-operational warning.")

    health = _json(f"{base_url}/api/health")
    bake_sha256 = health.get("bake_sha256")
    if health.get("status") != "ok" or health.get("baked") is not True:
        raise HealthGateError("Assess health does not report a ready baked service.")
    if not isinstance(bake_sha256, str) or len(bake_sha256) != 64:
        raise HealthGateError("Assess health does not expose a complete bake SHA-256 identity.")
    if expected_bake_sha256 is not None and bake_sha256 != expected_bake_sha256:
        raise HealthGateError(
            f"Live bake identity {bake_sha256} does not match expected {expected_bake_sha256}."
        )

    meta = _json(f"{base_url}/api/twin/meta")
    identity = meta.get("identity")
    imagery = meta.get("imagery")
    tiles = meta.get("tiles")
    center = meta.get("center_wgs84")
    if meta.get("schema") != "stage3-baked-v2":
        raise HealthGateError(f"Live bake schema is {meta.get('schema')!r}, not stage3-baked-v2.")
    if not isinstance(identity, dict) or identity.get("bake_sha256") != bake_sha256:
        raise HealthGateError("Health and twin metadata expose different bake identities.")
    if not isinstance(imagery, dict) or imagery.get("visual_context_only") is not True:
        raise HealthGateError("Baked visual-context imagery metadata is absent or mislabelled.")
    if not isinstance(tiles, dict) or not isinstance(center, list) or len(center) != 2:
        raise HealthGateError("Twin metadata is missing tile or centre-coordinate information.")

    zoom = min(int(tiles["max_zoom"]), int(imagery["max_zoom"]))
    x, y = _tile_xy(float(center[0]), float(center[1]), zoom)
    terrain_url = _tile_url(base_url, str(tiles["url_template"]), zoom=zoom, x=x, y=y)
    imagery_url = _tile_url(base_url, str(imagery["url_template"]), zoom=zoom, x=x, y=y)
    terrain_status, terrain_bytes, terrain_type = _request(terrain_url)
    imagery_status, imagery_bytes, imagery_type = _request(imagery_url)
    if terrain_status != 200 or imagery_status != 200:
        raise HealthGateError("Centre terrain or imagery tile did not return HTTP 200.")
    if terrain_type != "image/png" or imagery_type != "image/png":
        raise HealthGateError("Centre terrain or imagery tile is not PNG.")
    if not terrain_bytes.startswith(PNG_SIGNATURE) or not imagery_bytes.startswith(PNG_SIGNATURE):
        raise HealthGateError("Centre terrain or imagery response has an invalid PNG signature.")
    if hashlib.sha256(terrain_bytes).digest() == hashlib.sha256(imagery_bytes).digest():
        raise HealthGateError("Terrain and natural-colour imagery tiles are byte-identical.")

    assessment = _json(
        f"{base_url}/api/assess",
        method="POST",
        payload={
            "new_snow_cm": 40,
            "wind_speed_kmh": 45,
            "wind_direction_deg": 225,
            "release_size": "medium",
            "simulation_mode": "fast",
        },
    )
    model = assessment.get("model")
    conditions = assessment.get("conditions")
    disclaimer = assessment.get("disclaimer")
    if assessment.get("is_operational_forecast") is not False or assessment.get("is_probability") is not False:
        raise HealthGateError("Assessment operational/probability safety flags are not false.")
    if not isinstance(model, dict) or model.get("bake_sha256") != bake_sha256:
        raise HealthGateError("Assessment is not bound to the live bake identity.")
    # The scenario contract labels which KIND of user-supplied scenario produced
    # the numbers. The gate accepts any of them and rejects anything that does not
    # announce itself as user-supplied -- a live result must never look like a feed.
    user_supplied = {
        "user_supplied",
        "user_supplied_simple_scenario",
        "structured_observation_scenario",
    }
    if not isinstance(conditions, dict) or conditions.get("provenance") not in user_supplied:
        raise HealthGateError("Assessment conditions are not labelled as a user-supplied scenario.")
    if not isinstance(assessment.get("release_potential_index"), (int, float)):
        raise HealthGateError("Assessment did not return a release-potential relative index.")

    # The composite index is the headline number the app now leads with, so its
    # absence is a deployment failure even when the release index is fine. It is
    # nullable by contract (no zone crossed the threshold), and null must then be
    # accompanied by the separately-named percentile fallback rather than a zero.
    if "area_hazard_index" in assessment:
        area_index = assessment.get("area_hazard_index")
        fallback = assessment.get("no_zone_release_percentile_index")
        if area_index is None:
            if not isinstance(fallback, (int, float)):
                raise HealthGateError(
                    "Area hazard index is null with no named percentile fallback beside it."
                )
        elif not isinstance(area_index, (int, float)) or not 0 <= float(area_index) <= 100:
            raise HealthGateError("Area hazard index is not a 0-100 relative index.")
        elif not isinstance(assessment.get("hazard_components"), dict):
            raise HealthGateError("Area hazard index was published without its components.")
    if not isinstance(disclaimer, str) or "NOT an operational avalanche forecast" not in disclaimer:
        raise HealthGateError("Assessment is missing the deterministic non-operational disclaimer.")

    return GateSummary(
        base_url=base_url,
        bake_sha256=bake_sha256,
        schema=str(meta["schema"]),
        frontend_ok=True,
        health_ok=True,
        imagery_ok=True,
        assessment_ok=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--expected-bake-sha256")
    args = parser.parse_args(argv)
    try:
        summary = verify(args.base_url, args.expected_bake_sha256)
    except HealthGateError as exc:
        print(f"deployment health gate failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

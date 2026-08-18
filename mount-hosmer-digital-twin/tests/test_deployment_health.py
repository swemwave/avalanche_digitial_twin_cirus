"""Deployment gates and rollback/monitoring infrastructure contracts."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from deploy.verify_live import HealthGateError, verify


PROJECT_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parent
BAKE_SHA256 = "9" * 64


class _HealthyDeploymentHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _send(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: object) -> None:
        self._send("application/json", json.dumps(value).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send(
                "text/html",
                b"<title>Mount Hosmer</title>Experimental research scenario"
                b" &middot; never replaces Avalanche Canada guidance or field assessment",
            )
        elif self.path == "/api/health":
            self._json({"status": "ok", "baked": True, "bake_sha256": BAKE_SHA256})
        elif self.path == "/api/twin/meta":
            self._json(
                {
                    "schema": "stage3-baked-v2",
                    "identity": {"bake_sha256": BAKE_SHA256},
                    "center_wgs84": [0.0, 0.0],
                    "tiles": {
                        "max_zoom": 1,
                        "url_template": "/api/twin/tiles/{z}/{x}/{y}.png",
                    },
                    "imagery": {
                        "max_zoom": 1,
                        "url_template": "/api/twin/imagery/{z}/{x}/{y}.png",
                        "visual_context_only": True,
                    },
                }
            )
        elif self.path == "/api/twin/tiles/1/1/1.png":
            self._send("image/png", b"\x89PNG\r\n\x1a\nterrain")
        elif self.path == "/api/twin/imagery/1/1/1.png":
            self._send("image/png", b"\x89PNG\r\n\x1a\nimagery")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/assess":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        assert payload["simulation_mode"] == "fast"
        self._json(
            {
                "model": {"bake_sha256": BAKE_SHA256},
                "conditions": {"provenance": "user_supplied"},
                "release_potential_index": 42.0,
                "is_probability": False,
                "is_operational_forecast": False,
                "disclaimer": "This is NOT an operational avalanche forecast.",
            }
        )


@contextmanager
def _healthy_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthyDeploymentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_live_gate_checks_frontend_bake_imagery_and_assessment() -> None:
    with _healthy_server() as base_url:
        summary = verify(base_url, BAKE_SHA256)

    assert summary.bake_sha256 == BAKE_SHA256
    assert summary.frontend_ok is True
    assert summary.imagery_ok is True
    assert summary.assessment_ok is True


def test_live_gate_rejects_an_unexpected_bake_identity() -> None:
    with _healthy_server() as base_url, pytest.raises(HealthGateError, match="does not match expected"):
        verify(base_url, "a" * 64)


def test_ecs_services_have_overlap_and_automatic_rollback() -> None:
    template = (PROJECT_ROOT / "deploy" / "aws" / "infra.yaml").read_text(encoding="utf-8")

    assert template.count("DeploymentCircuitBreaker: { Enable: true, Rollback: true }") == 3
    assert template.count("MinimumHealthyPercent: 100") == 3
    assert template.count("MaximumPercent: 200") == 3
    assert template.count("Type: AWS::CloudWatch::Alarm") == 4
    for name in (
        "AssessUnhealthyAlarm",
        "AssistantUnhealthyAlarm",
        "FrontendUnhealthyAlarm",
        "LiveSmokeErrorsAlarm",
    ):
        assert f"  {name}:" in template


def test_hourly_public_monitor_checks_identity_imagery_and_assessment() -> None:
    template = (PROJECT_ROOT / "deploy" / "aws" / "infra.yaml").read_text(encoding="utf-8")

    assert "ExpectedBakeSha256:" in template
    assert "ScheduleExpression: cron(37 * * * ? *)" in template
    assert "EXPECTED_BAKE_SHA256: !Ref ExpectedBakeSha256" in template
    assert 'object_at("/api/health")' in template
    assert 'object_at("/api/twin/meta")' in template
    assert 'object_at("/api/assess"' in template
    assert 'hashlib.sha256(terrain).digest()==hashlib.sha256(satellite).digest()' in template
    assert "RetentionInDays: 7" in template


def test_deploy_script_uses_digest_images_waiter_and_functional_rollback() -> None:
    script = (PROJECT_ROOT / "deploy" / "aws" / "deploy.sh").read_text(encoding="utf-8")

    assert 'echo "$reg/twin/$service@$digest"' in script
    assert "ecs wait services-stable" in script
    assert "deploy/verify_live.py" in script
    assert 'PLAYWRIGHT_BASE_URL="https://$DOMAIN"' in script
    assert '"ExpectedBakeSha256=$sha"' in script
    assert '"ExpectedBakeSha256=$previous_expected_bake"' in script
    assert "restoring the three previous image identities" in script
    assert "--services assess assistant frontend" not in script


def test_scheduled_browser_monitor_is_bound_to_the_reviewed_bake() -> None:
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "live-smoke.yml").read_text(
        encoding="utf-8"
    )

    # Pinned on purpose: the hourly public monitor must only ever accept a bake a
    # human deliberately reviewed and rolled. Bumping this constant is that act --
    # it is not something a rebuild should be able to do on its own.
    #
    # 09de0a1c: metadata only. All ten baked layers -- elevation, slope, aspect,
    # plan/general curvature, forest mask/source, terrain source, and the two
    # exposure layers -- are byte-identical to 1b6524c4. What moved is the
    # published description of the surface, not the surface: the model parameter
    # manifest now names the rain/snow thresholds, the alpha override bounds and
    # the flow regime it always used; the reprojection lattice is recorded; the
    # gap-fill DEM is labelled by source ("Copernicus DEM GLO-30 gap-fill raster")
    # instead of by consequence ("no LiDAR coverage at this pixel"), and the
    # coverage warning is reworded to match. The same 4222 pixels (0.07% of the
    # AOI) are gap-filled before and after.
    expected = "09de0a1ccbcf5ec88a6c226bfc508b94040b4dff981a6387d3eae847a0528701"
    assert "cron:" in workflow
    assert f"EXPECTED_BAKE_SHA256: {expected}" in workflow
    assert "python deploy/verify_live.py" in workflow
    assert "npm --prefix frontend run smoke" in workflow

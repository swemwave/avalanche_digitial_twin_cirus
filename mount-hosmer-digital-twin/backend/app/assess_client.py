r"""How the assistant reaches the assessment model.

The assistant's *scenario* path must run the REAL deterministic assessment -- the
language model only narrates numbers it did not produce. That was a direct
``assess_mod.assess(bt, ...)`` call, which silently required the assistant to carry
the whole 154 MB baked terrain. Once the two are separate services that is exactly
what we do not want: the assistant image should be small, and there should be
**one** place that computes a hazard number and attaches the ``DISCLAIMER``.

So the call is now injected. Two implementations, same shape:

* :class:`LocalAssessClient` -- in-process. Used by the combined dev app, and by
  the tests, which stay hermetic and never open a socket.
* :class:`HttpAssessClient` -- POSTs to a remote assess service. Used by the
  assistant service in the cloud.

Both take and return exactly what ``app.assess.assess`` does, so the assistant is
unaware of which one it holds. Nothing here imports rasterio or pyproj.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Protocol

from avycore.hazard import risk

#: How long to wait on a remote assessment. A big storm at `advanced` can light up
#: ~40 release zones and take a while; the assistant only ever asks for `fast`, but
#: the ceiling is generous so a slow cold start is not read as an outage.
DEFAULT_TIMEOUT_S = float(os.environ.get("AVALANCHE_ASSESS_TIMEOUT_S", "90"))


class AssessUnavailableError(RuntimeError):
    """The assessment could not be run. Surfaces to the caller as a 503.

    Deliberately distinct from a *low* hazard result: a failed assessment is
    reported as failed, never as a benign-looking zero (invariant I3).
    """


class AssessClient(Protocol):
    """Conditions in, one finished assessment out."""

    def __call__(
        self, conditions: risk.Conditions, *, simulation_mode: str = "fast"
    ) -> dict[str, Any]: ...


class LocalAssessClient:
    """Runs the assessment in this process against already-loaded baked terrain."""

    def __init__(self, baked: Any) -> None:
        self._baked = baked

    def __call__(
        self, conditions: risk.Conditions, *, simulation_mode: str = "fast"
    ) -> dict[str, Any]:
        # Imported here, not at module scope: the assistant *service* never loads
        # baked terrain, and app.assess pulls in the runout engine to do it.
        from app import assess as assess_mod

        return assess_mod.assess(self._baked, conditions, simulation_mode=simulation_mode)


def _id_token(audience: str) -> str | None:
    """A Google-signed ID token for ``audience``, or None when not running on GCP.

    Cloud Run services deployed with ``--no-allow-unauthenticated`` require the
    caller to present an ID token whose audience is the *target* service URL. On
    Cloud Run that token comes from the instance metadata server; anywhere else
    (a laptop, CI, a test) the metadata host simply is not there, and returning
    None lets the same code path work unauthenticated against a local service.
    """
    import httpx

    try:
        response = httpx.get(
            "http://metadata.google.internal/computeMetadata/v1/"
            "instance/service-accounts/default/identity",
            params={"audience": audience},
            headers={"Metadata-Flavor": "Google"},
            timeout=5.0,
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    token = response.text.strip()
    return token or None


class HttpAssessClient:
    """POSTs to a remote assess service's ``/api/assess``.

    Tokens are cached for :data:`_TOKEN_TTL_S` because the metadata server is a
    real network hop and the assistant would otherwise pay it on every scenario
    turn. Google's tokens last an hour; we refresh well inside that.
    """

    _TOKEN_TTL_S = 30 * 60

    def __init__(self, base_url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._token: str | None = None
        self._token_at: float = 0.0
        self._lock = threading.Lock()

    def _auth_header(self) -> dict[str, str]:
        with self._lock:
            fresh = self._token and (time.monotonic() - self._token_at) < self._TOKEN_TTL_S
            if not fresh:
                self._token = _id_token(self.base_url)
                self._token_at = time.monotonic()
            token = self._token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def __call__(
        self, conditions: risk.Conditions, *, simulation_mode: str = "fast"
    ) -> dict[str, Any]:
        import httpx

        clamped = conditions.clamped()
        body = {
            "new_snow_cm": clamped.new_snow_cm,
            "wind_speed_kmh": clamped.wind_speed_kmh,
            "wind_direction_deg": clamped.wind_direction_deg,
            "release_size": clamped.release_size,
            "simulation_mode": simulation_mode,
        }
        try:
            response = httpx.post(
                f"{self.base_url}/api/assess",
                json=body,
                headers=self._auth_header(),
                timeout=self.timeout_s,
            )
        except Exception as exc:  # network down, DNS, timeout
            raise AssessUnavailableError(
                f"The assessment service at {self.base_url} could not be reached: {exc}"
            ) from exc

        if response.status_code != 200:
            raise AssessUnavailableError(
                f"The assessment service returned {response.status_code}: {response.text[:200]}"
            )
        return response.json()


def from_environment(baked: Any | None = None) -> AssessClient:
    """Pick an implementation: remote when ``AVALANCHE_ASSESS_URL`` is set, else local.

    That single variable is what separates the two deployment shapes. Unset (a
    laptop, the combined app, the tests) means in-process against ``baked``; set
    (the assistant service on Cloud Run) means HTTP to the assess service.
    """
    remote = os.environ.get("AVALANCHE_ASSESS_URL", "").strip()
    if remote:
        return HttpAssessClient(remote)
    if baked is None:
        raise AssessUnavailableError(
            "No assessment backend: set AVALANCHE_ASSESS_URL to a remote assess "
            "service, or provide baked terrain for in-process assessment."
        )
    return LocalAssessClient(baked)

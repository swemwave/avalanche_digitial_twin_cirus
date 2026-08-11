r"""Assistant routes: explain an assessment, and hold a scenario conversation.

This router carries no terrain and computes no hazard. ``/explain`` is pure text
over an assessment the caller already has; ``/chat`` may need a *new* assessment,
and gets one from :mod:`app.assess_client` -- in-process locally, or a call to the
assess service when deployed separately.

Both failure modes are 503, and they are different outages worth telling apart in
the message: the language model being unreachable (:class:`AssistantError`) versus
the assessment model being unreachable (:class:`AssessUnavailableError`).
"""

from __future__ import annotations

from typing import Any

from avycore.assistant import AssistantError, chat as assistant_chat, explain as assistant_explain
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import assess_client
from app.api.models import AssessResult, ChatRequest, ChatResult, ExplainRequest, ExplainResult
from app.assess_client import AssessUnavailableError

router = APIRouter(prefix="/api", tags=["assistant"])


@router.get("/assistant/health")
def assistant_health() -> dict[str, Any]:
    """The assistant's own health, reachable from outside a path-routing proxy.

    Behind one load balancer the services share an origin and are told apart by
    path, so ``/api/health`` reaches whichever service owns ``/api/*`` -- the assess
    service. Without this route the assistant's health would be visible only to the
    load balancer's internal target check, which is exactly when you least want to
    be guessing. Mirrors the payload of the service's own ``/api/health``.
    """
    import os

    from app.service import NON_OPERATIONAL_NOTE, assess_backend
    from app.core.settings import get_settings

    payload: dict[str, Any] = {
        "status": "ok",
        "service": "assistant",
        "application_version": get_settings().app_version,
        "note": NON_OPERATIONAL_NOTE,
        "ollama_model": os.environ.get("AVALANCHE_OLLAMA_MODEL", "llama3.1:8b"),
        "ollama_configured": bool(os.environ.get("AVALANCHE_OLLAMA_URL", "").strip()),
    }
    payload.update(assess_backend())
    return payload

@router.post(
    "/assistant/explain", response_model=ExplainResult, operation_id="postExplain"
)
def assistant_explain_route(body: ExplainRequest) -> ExplainResult:
    """Plain-language read of an assessment. The disclaimer is appended in code."""
    try:
        assessment = (
            body.assessment.model_dump() if isinstance(body.assessment, AssessResult) else body.assessment
        )
        return ExplainResult.model_validate(assistant_explain(assessment))
    except AssistantError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/assistant/chat", response_model=ChatResult, operation_id="postChat")
def assistant_chat_route(body: ChatRequest, assess=Depends(assess_client)) -> ChatResult:
    """Scenario chat: parse the message to slider values, re-run the assessment, narrate."""
    try:
        return ChatResult.model_validate(
            assistant_chat(
                assess,
                body.message,
                body.assessment.model_dump()
                if isinstance(body.assessment, AssessResult)
                else body.assessment,
                [turn.model_dump() for turn in body.history] if body.history else None,
            )
        )
    except AssistantError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AssessUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

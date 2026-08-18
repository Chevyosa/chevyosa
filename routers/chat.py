"""
routers/chat.py — POST /chat and GET /chat/limit endpoints.

Routes messages to either:
  - RAG pipeline (default)
  - Intake state machine (when session is in intake mode)

Supports SSE streaming via `stream: true`. Enforces a per-IP 24h rate limit.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from models import ChatRequest, ChatResponse, SourceItem
from services.rag import run_rag_pipeline, RAGResult
from services.intake import get_intake_session, process_intake_llm, start_intake_llm, detect_intake_intent
from services.rate_limit import check_rate_limit, consume_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


def get_client_ip(request: Request) -> str:
    """Resolve the real client IP, honoring reverse-proxy headers on a VPS."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_headers(status: dict) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(status["limit"]),
        "X-RateLimit-Remaining": str(status["remaining"]),
        "X-RateLimit-Reset": str(status["resets_in"]),
    }


@router.get("/chat/limit")
async def chat_limit(request: Request):
    """Return the current rate-limit status for the calling client."""
    status = await check_rate_limit(get_client_ip(request))
    return JSONResponse(
        status_code=200,
        content={
            "limit": status["limit"],
            "remaining": status["remaining"],
            "resets_in": status["resets_in"],
        },
        headers=_rate_limit_headers(status),
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, req: Request, response: Response):
    """
    Main chat endpoint.

    Routes to intake state machine if session is active,
    otherwise runs the RAG pipeline.
    """
    message = request.message.strip()
    session_id = request.session_id or "anonymous"

    # ── Rate limit (per client IP, 24h window) ──────────────────────
    status = await consume_rate_limit(get_client_ip(req))
    if not status["allowed"]:
        return JSONResponse(
            status_code=429,
            headers=_rate_limit_headers(status),
            content={
                "code": "rate_limited",
                "message": "Kamu sudah mencapai batas pesan harian. Coba lagi nanti ya.",
                "limit": status["limit"],
                "remaining": 0,
                "resets_in": status["resets_in"],
            },
        )

    response.headers.update(_rate_limit_headers(status))

    # ── Check for active intake session ─────────────────────────────
    intake_session = await get_intake_session(session_id)

    if intake_session is not None:
        # Active intake session → LLM-driven natural flow
        result = await process_intake_llm(session_id, message, intake_session, request.history)
        return ChatResponse(
            mode="intake",
            response=result["response"],
            step=result.get("step"),
            chips=result.get("chips"),
        )

    # ── Check for project-request intent (keyword fast-path + LLM) ──
    if _is_intake_trigger(message) or await detect_intake_intent(message, request.history):
        result = await start_intake_llm(session_id, message, request.history)
        return ChatResponse(
            mode="intake",
            response=result["response"],
            step=result.get("step"),
            chips=result.get("chips"),
        )

    # ── RAG pipeline ────────────────────────────────────────────────
    if request.stream:
        return await _handle_stream(message, request.history, _rate_limit_headers(status))

    rag_result = await run_rag_pipeline(
        message=message,
        history=request.history,
        stream=False,
    )

    if isinstance(rag_result, RAGResult):
        return ChatResponse(
            mode=rag_result.mode,
            response=rag_result.response,
            sources=[
                SourceItem(title=s["title"], score=s["score"])
                for s in rag_result.sources
            ],
        )

    # Fallback
    return ChatResponse(mode="rag", response=str(rag_result))


async def _handle_stream(message: str, history: list[dict], headers: dict[str, str] | None = None):
    """Handle SSE streaming response."""
    generator = await run_rag_pipeline(
        message=message,
        history=history,
        stream=True,
    )

    async def sse_generator():
        try:
            async for chunk in generator:
                data = json.dumps({"content": chunk}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"SSE stream error: {e}")
            error_data = json.dumps({"error": str(e)}, ensure_ascii=False)
            yield f"data: {error_data}\n\n"
            yield "data: [DONE]\n\n"

    stream_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    if headers:
        stream_headers.update(headers)

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers=stream_headers,
    )


def _is_intake_trigger(message: str) -> bool:
    """Check if the message triggers the intake/project-request flow."""
    msg_lower = message.lower()
    triggers = [
        "/project-request",
        "/request",
        "mau bikin", "mau buat", "mau hire", "mau pesan",
        "saya mau bikin", "saya mau buat", "aku mau bikin", "aku mau buat",
        "ingin bikin", "ingin buat", "ingin membuat", "ingin pesan",
        "pengen bikin", "pengen buat", "pengen buatin", "pengen dibuat",
        "pingin bikin", "pingin buat",
        "buatin", "bikinin", "buatkan", "dibuatin", "dibikinin",
        "minta buat", "minta tolong buat", "minta dibuat", "minta dibuatin",
        "tolong buatin", "tolong bikinin", "tolong buatkan",
        "berapa harga", "berapa biaya", "biaya pembuatan", "harga pembuatan",
        "bisa bantu skripsi", "bisa bantu tugas", "bisa buatkan", "bisa bikinin",
        "butuh jasa", "butuh developer", "butuh bantu", "butuh dibuatkan",
        "order project", "order proyek",
        "request proyek", "request project", "ajak kerja sama",
        "project untuk", "proyek untuk", "projectnya untuk", "proyeknya untuk",
        "project saya", "proyek saya", "projectku", "proyekku",
        "aplikasi untuk", "website untuk", "sistem untuk",
        "cari developer", "cari jasa",
    ]
    return any(t in msg_lower for t in triggers)

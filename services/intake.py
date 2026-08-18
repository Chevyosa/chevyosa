"""
services/intake.py — LLM-driven project request intake.

Natural conversation collects 5 data points (service, description, budget,
deadline, contact). Once the required points are collected, the lead is saved
and a Telegram notification is sent automatically.

Sessions persisted to intake_sessions table (anti-kehilangan jika restart).
Timeout: 15 menit tanpa aktivitas → session expired.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from groq import AsyncGroq

from config import get_pool, settings

logger = logging.getLogger(__name__)


TIMEOUT_MINUTES = 15


# ── Session management ──────────────────────────────────────────────

async def get_intake_session(chat_id: str) -> dict | None:
    """
    Get active intake session from DB.

    Returns None if no session or session expired (>15 min idle).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT step, data, updated_at FROM intake_sessions WHERE chat_id = $1",
            chat_id,
        )

    if row is None:
        return None

    # Check timeout
    updated_at = row["updated_at"]
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    elapsed = (now - updated_at).total_seconds()

    if elapsed > TIMEOUT_MINUTES * 60:
        # Session expired — clean up
        await _delete_session(chat_id)
        return None

    data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
    return {"step": row["step"], "data": data}


# ── Internal helpers ────────────────────────────────────────────────

async def _update_session(chat_id: str, step: int, data: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE intake_sessions
            SET step = $1, data = $2::jsonb, updated_at = now()
            WHERE chat_id = $3
            """,
            step,
            json.dumps(data),
            chat_id,
        )


async def _delete_session(chat_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM intake_sessions WHERE chat_id = $1",
            chat_id,
        )


async def _save_lead(data: dict) -> str:
    """Insert lead into leads table. Returns lead ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        lead_id = await conn.fetchval(
            """
            INSERT INTO leads (service, description, budget, deadline, contact, raw)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING id
            """,
            data.get("service", ""),
            data.get("description", ""),
            data.get("budget", ""),
            data.get("deadline", ""),
            data.get("contact", ""),
            json.dumps(data),
        )
    logger.info(f"Lead saved: {lead_id}")
    return str(lead_id)


async def _notify_telegram(data: dict, lead_id: str):
    """Send lead notification to Riyanda via Telegram bridge."""
    try:
        from services.bridge import send_telegram_message

        text = (
            f"New Project Request!\n\n"
            f"Lead ID: {lead_id}\n"
            f"Layanan: {data.get('service', '-')}\n"
            f"Deskripsi: {data.get('description', '-')}\n"
            f"Budget: {data.get('budget', '-')}\n"
            f"Deadline: {data.get('deadline', '-')}\n"
            f"Kontak: {data.get('contact', '-')}"
        )
        await send_telegram_message(text)
    except Exception as e:
        logger.error(f"Telegram notification failed: {e}")
        # Don't fail the intake — lead is already saved


# ── LLM-driven intake (natural conversation, not rule-based) ────────

INTAKE_FIELDS = ["service", "description", "budget", "deadline", "contact"]

INTAKE_SYSTEM = """Kamu adalah Chevyosa, asisten virtual Riyanda Azis Febrian. Seorang pengunjung sedang ingin membuat request project.

Kumpulkan informasi berikut secara natural dan ramah (JANGAN kaku seperti mengisi form):
- service: jenis layanan (Web App, Mobile App, Skripsi/TA, Coaching, Desain, atau Lainnya)
- description: deskripsi project (tujuan & fitur utama)
- budget: estimasi budget
- deadline: target selesai
- contact: kontak WA/email

Aturan:
- Jawab dalam bahasa yang sama dengan pengunjung (default Bahasa Indonesia).
- Obrolan tetap natural dan hangat, tapi jangan keluar jalur: tugasmu adalah melengkapi info project di atas.
- Tanyakan SATU info yang belum lengkap per giliran, dengan bahasa natural dan hangat.
- Kalau pengunjung bertanya balik (misalnya "jasanya apa aja?" atau "berapa harganya?"), jelaskan dengan ramah lalu lanjut kumpulkan info.
- JANGAN menyuruh pengunjung menghubungi email/LinkedIn. Kumpulkan info sampai lengkap; sistem yang akan meneruskan request ke Riyanda.
- Kalau semua info sudah lengkap, buat rangkuman singkat yang hangat.

Info yang SUDAH terkumpul:
{fields_text}

Keluarkan HANYA JSON valid tanpa teks lain, dengan format:
{{"response": "...", "fields": {{"service": "...", "description": "...", "budget": "...", "deadline": "...", "contact": "..."}}}}

- "response": balasan natural kamu untuk pengunjung.
- "fields": nilai field yang sudah bisa disimpulkan dari SELURUH percakapan (termasuk pesan terakhir). Isi "" untuk field yang belum diketahui. Jangan mengarang nilai."""

_intake_groq_client: AsyncGroq | None = None


def _get_intake_groq_client() -> AsyncGroq:
    global _intake_groq_client
    if _intake_groq_client is None:
        _intake_groq_client = AsyncGroq(api_key=settings.groq_api_key)
    return _intake_groq_client


def _build_fields_text(data: dict) -> str:
    lines = []
    for f in INTAKE_FIELDS:
        val = (data.get(f) or "").strip()
        lines.append(f"- {f}: {val or '(belum)'}")
    return "\n".join(lines)


async def _call_intake_llm(data: dict, history: list[dict], message: str) -> dict | None:
    """Call Groq for a natural intake response + structured field extraction."""
    client = _get_intake_groq_client()
    system = INTAKE_SYSTEM.format(fields_text=_build_fields_text(data))

    messages = [{"role": "system", "content": system}]
    for turn in history[-6:]:
        messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
    messages.append({"role": "user", "content": message})

    try:
        resp = await client.chat.completions.create(
            model=settings.groq_model,
            messages=messages,
            temperature=0.4,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content.strip()
        parsed = json.loads(content)
        return {
            "response": (parsed.get("response") or "").strip(),
            "fields": parsed.get("fields") or {},
        }
    except Exception as e:
        logger.error("Intake LLM error: %s", e)
        return None


def merge_fields(data: dict, fields: dict) -> dict:
    """Merge non-empty extracted fields into the collected data."""
    for f in INTAKE_FIELDS:
        val = (fields.get(f) or "").strip()
        if val:
            data[f] = val
    return data


ESSENTIAL_FIELDS = ["service", "description", "contact"]
OPTIONAL_FIELDS = ["budget", "deadline"]


def is_lead_ready(data: dict) -> bool:
    """A lead is ready to auto-send when the essential fields are complete
    plus at least one of the optional fields (budget/deadline) — i.e. 4 or 5
    of the 5 intake points are satisfied."""
    essential_ok = all((data.get(f) or "").strip() for f in ESSENTIAL_FIELDS)
    optional_ok = any((data.get(f) or "").strip() for f in OPTIONAL_FIELDS)
    return essential_ok and optional_ok


# ── LLM-based project-request intent detection ─────────────────────

INTENT_SYSTEM = (
    "Klasifikasikan pesan pengunjung website portfolio Riyanda (Full-Stack Developer). "
    'Jawab "yes" jika pesan menunjukkan pengunjung ingin MEMBUAT/MEMESAN project, atau SEDANG '
    'MENJELASKAN project yang ingin dibuat — misalnya menyebut "web app untuk...", "aplikasi yang...", '
    '"saya mau...", budget, deadline, butuh developer, minta dibuatkan, atau minta dikerjakan. '
    'Jawab "no" HANYA jika pesan jelas-jelas: salam/basa-basi, bertanya INFO tentang Riyanda '
    '(siapa dia, pengalaman, layanan, harga secara umum), atau bertanya "project apa saja yang '
    'SUDAH dikerjakan". Jika ragu, jawab "yes". '
    'Balas HANYA dengan JSON valid: {"intent": "yes"} atau {"intent": "no"}.'
)


async def detect_intake_intent(message: str, history: list[dict] | None = None) -> bool:
    """Return True if the user seems to be requesting a project."""
    history = history or []
    client = _get_intake_groq_client()
    messages = [{"role": "system", "content": INTENT_SYSTEM}]
    for turn in history[-4:]:
        messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
    messages.append({"role": "user", "content": message})

    try:
        resp = await client.chat.completions.create(
            model=settings.groq_model,
            messages=messages,
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        content = (resp.choices[0].message.content or "").strip().lower()
        if "yes" in content:
            return True
        try:
            if (json.loads(content).get("intent") or "").strip().lower() == "yes":
                return True
        except Exception:
            pass
        # Fallback: reasoning models may leave `content` empty (answer in `reasoning`).
        reasoning = getattr(resp.choices[0].message, "reasoning", None) or ""
        return "yes" in reasoning.lower()
    except Exception as e:
        logger.error("Intent detection error: %s", e)
        return False


async def start_intake_llm(chat_id: str, message: str, history: list[dict] | None = None) -> dict:
    """Start a new intake session and process the triggering message via the LLM."""
    history = history or []
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO intake_sessions (chat_id, step, data, updated_at)
            VALUES ($1, 1, '{}', now())
            ON CONFLICT (chat_id) DO UPDATE
            SET step = 1, data = '{}', updated_at = now()
            """,
            chat_id,
        )

    session = {"step": 1, "data": {}}
    return await process_intake_llm(chat_id, message, session, history)


async def process_intake_llm(
    chat_id: str,
    message: str,
    session: dict,
    history: list[dict] | None = None,
) -> dict:
    """Process a user message in intake mode using the LLM (natural flow).

    Once the required intake points are collected, the lead is saved and the
    Telegram notification is sent automatically (no separate confirmation step).
    """
    history = history or []
    data = session.get("data", {})
    step = session.get("step", 1)

    # Cancel
    if message.lower().strip() in ("/cancel", "batal", "cancel"):
        await _delete_session(chat_id)
        return {
            "response": "Oke, request dibatalin ya. Kalau berubah pikiran, tinggal bilang aja!",
            "step": None,
            "chips": None,
        }

    result = await _call_intake_llm(data, history, message)
    if result is None:
        return {
            "response": "Maaf, aku lagi sedikit kesulitan memproses itu. Bisa diulang atau lanjut ceritain kebutuhan project kamu?",
            "step": step,
            "chips": None,
        }

    merge_fields(data, result["fields"])

    # Auto-send once the required points are collected
    if is_lead_ready(data):
        lead_id = await _save_lead(data)
        await _notify_telegram(data, lead_id)
        await _delete_session(chat_id)
        return {
            "response": (
                "Siap! Request kamu sudah lengkap dan sudah aku kirim ke Riyanda.\n"
                "Dia akan segera menghubungi kamu lewat kontak yang kamu kasih.\n"
                "Ada yang lain yang bisa aku bantu?"
            ),
            "step": None,
            "chips": None,
        }

    await _update_session(chat_id, 1, data)
    return {"response": result["response"], "step": 1, "chips": None}

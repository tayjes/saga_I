import json
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pathlib import Path

app = FastAPI(title="SAGA Meeting UI")
BASE = Path(__file__).resolve().parent
ALICE_URL = os.getenv("ALICE_AGENT_URL", "https://alice-agent:9001")
BOB_URL = os.getenv("BOB_AGENT_URL", "https://bob-agent:9002")
SEED_ADMIN_URL = os.getenv("SEED_ADMIN_URL", "http://seed-admin:8081")
SEED_ADMIN_TOKEN = os.getenv("SEED_ADMIN_TOKEN", "")
VERIFY_TLS = os.getenv("AGENT_VERIFY_TLS", "false").lower() == "true"
ALICE_EMAIL = os.getenv("ALICE_EMAIL", "")
BOB_EMAIL = os.getenv("BOB_EMAIL", "")


class MeetingRequest(BaseModel):
    initiator: str
    target_agent: str
    title: str
    start: str
    end: str


class RefreshRequest(BaseModel):
    uid: str
    num_otks: int = 10


class AssistantMessage(BaseModel):
    role: str
    content: str


class AssistantMeetingRequest(BaseModel):
    initiator: str
    target_agent: str
    messages: list[AssistantMessage]
    timezone: str = "UTC"


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, f"invalid ISO datetime: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _agent(name: str) -> str:
    if name == "alice":
        return ALICE_URL
    if name == "bob":
        return BOB_URL
    raise HTTPException(400, "sender agent must be alice or bob")


async def _get_events(name: str, start: str, end: str):
    async with httpx.AsyncClient(verify=VERIFY_TLS, timeout=15.0) as c:
        r = await c.get(
            f"{_agent(name)}/calendar/events",
            params={"time_min": start, "time_max": end},
        )
        r.raise_for_status()
        return r.json()


def _event_interval(event: dict, default_tz) -> tuple[datetime, datetime] | None:
    start = event.get("start", {})
    end = event.get("end", {})
    s = start.get("dateTime") or start.get("date")
    e = end.get("dateTime") or end.get("date")
    if not s or not e:
        return None
    try:
        event_start = datetime.fromisoformat(s.replace("Z", "+00:00"))
        event_end = datetime.fromisoformat(e.replace("Z", "+00:00"))
    except ValueError:
        return None
    if event_start.tzinfo is None:
        event_start = event_start.replace(tzinfo=default_tz)
    if event_end.tzinfo is None:
        event_end = event_end.replace(tzinfo=default_tz)
    return event_start, event_end


def _free_slots(start: str, end: str, alice_events: list, bob_events: list, duration_minutes: int = 30):
    window_start = _parse_iso(start)
    window_end = _parse_iso(end)
    if window_end <= window_start:
        raise HTTPException(400, "end must be after start")
    if duration_minutes < 1:
        raise HTTPException(400, "duration must be at least 1 minute")

    busy = []
    for event in alice_events + bob_events:
        interval = _event_interval(event, window_start.tzinfo)
        if interval:
            event_start, event_end = interval
            if event_end > event_start:
                busy.append((event_start, event_end))

    slots = []
    cursor = window_start
    duration = timedelta(minutes=duration_minutes)
    while cursor + duration <= window_end and len(slots) < 12:
        candidate_end = cursor + duration
        conflict = any(
            busy_start < candidate_end and cursor < busy_end
            for busy_start, busy_end in busy
        )
        if not conflict:
            slots.append({"start": cursor.isoformat(), "end": candidate_end.isoformat()})
        cursor += duration
    return slots


@app.get("/api/health")
async def health():
    result = {}
    async with httpx.AsyncClient(verify=VERIFY_TLS, timeout=5.0) as c:
        for name, url in (("alice", ALICE_URL), ("bob", BOB_URL)):
            try:
                r = await c.get(f"{url}/health")
                result[name] = r.json()
            except Exception as e:
                result[name] = {"status": "offline", "detail": str(e)}
    return result


@app.get("/api/availability")
async def availability(start: str, end: str):
    window_start = _parse_iso(start)
    window_end = _parse_iso(end)
    if window_end <= window_start:
        raise HTTPException(400, "end must be after start")

    alice_events = await _get_events("alice", start, end)
    bob_events = await _get_events("bob", start, end)
    return {
        "alice": alice_events,
        "bob": bob_events,
        "shared_free_slots": _free_slots(start, end, alice_events, bob_events),
    }


@app.post("/api/meeting")
async def create_meeting(req: MeetingRequest):
    if req.initiator not in ("alice", "bob"):
        raise HTTPException(400, "initiator must be alice or bob")

    target = req.target_agent.strip()
    if not target:
        raise HTTPException(400, "target_agent is required")
    if target == f"{req.initiator}:calendar_agent":
        raise HTTPException(400, "sender and target agents must be different")

    start_dt = _parse_iso(req.start)
    end_dt = _parse_iso(req.end)
    if end_dt <= start_dt:
        raise HTTPException(400, "end must be after start")

    alice_events = await _get_events("alice", req.start, req.end)
    bob_events = await _get_events("bob", req.start, req.end)
    duration_minutes = max(1, int((end_dt - start_dt).total_seconds() // 60))
    if _free_slots(
        req.start,
        req.end,
        alice_events,
        bob_events,
        duration_minutes=duration_minutes,
    ) == []:
        raise HTTPException(409, "the selected time overlaps an existing event on Alice or Bob's calendar")

    initiator_email = ALICE_EMAIL if req.initiator == "alice" else BOB_EMAIL
    target_email = BOB_EMAIL if target == "bob:calendar_agent" else ALICE_EMAIL if target == "alice:calendar_agent" else ""
    payload = {
        "action": "schedule_meeting",
        "summary": req.title,
        "start": req.start,
        "end": req.end,
        "attendees": [initiator_email] if initiator_email else [],
    }

    async with httpx.AsyncClient(verify=VERIFY_TLS, timeout=20.0) as c:
        try:
            target_result = await c.post(
                f"{_agent(req.initiator)}/saga/send",
                json={"target_aid": target, "payload": payload},
            )
            target_result.raise_for_status()

            own_payload = {**payload, "attendees": [target_email] if target_email else []}
            own_result = await c.post(
                f"{_agent(req.initiator)}/calendar/create",
                json=own_payload,
            )
            own_result.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text or str(exc)
            raise HTTPException(
                exc.response.status_code,
                f"agent/provider operation failed: {detail}",
            ) from exc

    return {
        "status": "scheduled",
        "initiator": req.initiator,
        "target": target,
        "target_result": target_result.json(),
        "own_result": own_result.json(),
    }



GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

ASSISTANT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check both Alice and Bob calendars for free slots in the requested time window. This must be called before scheduling. Use ISO 8601 datetimes with an explicit UTC offset, and set duration_minutes to the meeting length.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Window start, ISO 8601 with UTC offset"},
                    "end": {"type": "string", "description": "Window end, ISO 8601 with UTC offset"},
                    "duration_minutes": {"type": "integer", "description": "Meeting length in minutes", "minimum": 1, "maximum": 480}
                },
                "required": ["start", "end", "duration_minutes"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": "Actually book the meeting through the existing SAGA /api/meeting workflow. Only call after check_availability returned the exact requested slot as free and the user explicitly asked to schedule/book it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short calendar event title"},
                    "start": {"type": "string", "description": "Meeting start, ISO 8601 with explicit UTC offset"},
                    "end": {"type": "string", "description": "Meeting end, ISO 8601 with explicit UTC offset"}
                },
                "required": ["title", "start", "end"],
                "additionalProperties": False
            }
        }
    }
]


async def _run_assistant_tool(name: str, args: dict, req: AssistantMeetingRequest, checked_slots: set):
    if name == "check_availability":
        start = args.get("start", "")
        end = args.get("end", "")
        start_dt, end_dt = _parse_iso(start), _parse_iso(end)
        if end_dt <= start_dt:
            return {"error": "end must be after start"}
        if start_dt.utcoffset() is None or end_dt.utcoffset() is None:
            return {"error": "Use ISO 8601 datetimes with an explicit UTC offset."}
        duration = int(args.get("duration_minutes", 30))
        if duration < 1 or duration > 480:
            return {"error": "duration_minutes must be between 1 and 480"}
        alice_events = await _get_events("alice", start, end)
        bob_events = await _get_events("bob", start, end)
        slots = _free_slots(start, end, alice_events, bob_events, duration_minutes=duration)
        for slot in slots:
            checked_slots.add((_parse_iso(slot["start"]), _parse_iso(slot["end"])))
        return {
            "initiator": req.initiator,
            "target_agent": req.target_agent,
            "duration_minutes": duration,
            "shared_free_slots": slots,
            "slot_count": len(slots),
        }

    if name == "schedule_meeting":
        title = str(args.get("title", "")).strip()
        start, end = str(args.get("start", "")), str(args.get("end", ""))
        if not title:
            return {"error": "Meeting title is required."}
        start_dt, end_dt = _parse_iso(start), _parse_iso(end)
        if start_dt.utcoffset() is None or end_dt.utcoffset() is None:
            return {"error": "Use ISO 8601 datetimes with an explicit UTC offset."}
        if (start_dt, end_dt) not in checked_slots:
            return {"error": "Safety check blocked booking: this exact slot has not been confirmed free by check_availability. Check availability for the exact proposed interval first."}
        result = await create_meeting(MeetingRequest(
            initiator=req.initiator,
            target_agent=req.target_agent,
            title=title,
            start=start,
            end=end,
        ))
        return {"booking_result": result}

    return {"error": "Unknown tool."}


@app.post("/api/assistant/meeting")
async def assistant_meeting(req: AssistantMeetingRequest):
    if not GROQ_API_KEY:
        raise HTTPException(503, "GROQ_API_KEY is not configured for the UI service.")
    if req.initiator not in ("alice", "bob"):
        raise HTTPException(400, "initiator must be alice or bob")
    if req.target_agent not in ("alice:calendar_agent", "bob:calendar_agent"):
        raise HTTPException(400, "target_agent must be alice:calendar_agent or bob:calendar_agent")
    if req.target_agent == f"{req.initiator}:calendar_agent":
        raise HTTPException(400, "sender and target agents must be different")
    if not req.messages or len(req.messages) > 20:
        raise HTTPException(400, "Send between 1 and 20 chat messages.")
    conversation = []
    for message in req.messages:
        if message.role not in ("user", "assistant") or not message.content.strip():
            continue
        conversation.append({"role": message.role, "content": message.content[:4000]})
    if not conversation or conversation[-1]["role"] != "user":
        raise HTTPException(400, "The latest chat message must be from the user.")

    try:
        from zoneinfo import ZoneInfo
        local_zone = ZoneInfo(req.timezone)
        now_local = datetime.now(local_zone).isoformat()
        zone_name = req.timezone
    except Exception:
        now_local = datetime.now(timezone.utc).isoformat()
        zone_name = "UTC"

    system_prompt = (
        "You are SAGA Meeting Assistant. You can call tools that check both participants' real calendars "
        "and schedule a meeting using the existing SAGA API. Current local date/time: " + now_local +
        " (timezone " + zone_name + "). Sender is fixed to " + req.initiator +
        ":calendar_agent and target is fixed to " + req.target_agent + ". Do not change these identities. "
        "Be concise and conversational. If title, duration, date, or time window is missing, ask a clarifying "
        "question instead of inventing it. Interpret relative dates using the supplied local timezone. "
        "Always use ISO 8601 timestamps with the correct UTC offset for that timezone. "
        "Before scheduling, call check_availability. Only schedule if the exact slot is returned as available. "
        "Never claim a booking succeeded unless schedule_meeting returns booking_result with status scheduled. "
        "Only create a calendar event when the user clearly asks to schedule/book/create it; when merely comparing "
        "options, check availability and present options without booking. If a requested slot is busy, offer actual "
        "free slots returned by the tool and ask which one the user wants, unless the user explicitly authorized "
        "booking the first suitable free slot in a stated window. Never claim to have checked a calendar unless the "
        "tool actually ran. Tool errors must be explained honestly."
    )
    messages = [{"role": "system", "content": system_prompt}] + conversation
    checked_slots = set()
    actions = []

    async with httpx.AsyncClient(timeout=45.0) as client:
        for _ in range(6):
            try:
                response = await client.post(
                    GROQ_API_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": GROQ_MODEL,
                        "messages": messages,
                        "tools": ASSISTANT_TOOLS,
                        "tool_choice": "auto",
                        "temperature": 0.1,
                    },
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:1000] or "Groq API request failed"
                raise HTTPException(502, f"Groq API error: {detail}") from exc
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"Could not reach Groq API: {exc}") from exc

            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise HTTPException(502, "Groq returned no assistant response.")
            assistant_message = choices[0].get("message", {})
            messages.append(assistant_message)
            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                return {
                    "reply": assistant_message.get("content") or "I couldn't produce a response. Please try again.",
                    "model": data.get("model", GROQ_MODEL),
                    "actions": actions,
                }

            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name", "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    result = await _run_assistant_tool(name, arguments, req, checked_slots)
                except HTTPException as exc:
                    result = {"error": exc.detail}
                except (ValueError, TypeError, KeyError) as exc:
                    result = {"error": f"Invalid tool arguments: {exc}"}
                except Exception as exc:
                    result = {"error": f"Tool failed: {type(exc).__name__}: {exc}"}
                actions.append({"tool": name, "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": name,
                    "content": json.dumps(result, default=str),
                })

    return {
        "reply": "I reached the tool-call limit before finishing. Please narrow the request or try again.",
        "model": GROQ_MODEL,
        "actions": actions,
    }


@app.post("/api/otk-refresh")
async def refresh_otks(req: RefreshRequest):
    if req.uid not in ("alice", "bob"):
        raise HTTPException(400, "uid must be alice or bob")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"{SEED_ADMIN_URL}/refresh-otks",
            headers={"X-Seed-Admin-Token": SEED_ADMIN_TOKEN},
            json=req.model_dump(),
        )
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        return r.json()


def _serve_dashboard():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/")
def index():
    """Overview dashboard with a selectable sender."""
    return _serve_dashboard()


@app.get("/alice")
def alice_dashboard():
    """Alice-specific dashboard; the browser locks Alice as the sender."""
    return _serve_dashboard()


@app.get("/bob")
def bob_dashboard():
    """Bob-specific dashboard; the browser locks Bob as the sender."""
    return _serve_dashboard()

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

"""
SAGA Agent -- minimal implementation.

Exposes the receiver-side of the agent-to-agent protocol (SAGA paper,
Section IV-E): a peer presents its Provider-signed identity plus one of
our OTKs, we run the DH handshake and issue a token, then the peer
attaches that token to subsequent /saga/message calls.

`handle_task_message()` at the bottom is the seam where actual agent
logic (smolagents + Google Calendar tool) plugs in -- for now it just
echoes, so the transport layer can be verified independently first.
"""
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import config, crypto, provider_client, peer_client, session_store, calendar_tool
from .keys import load_identity

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(config.AID)

app = FastAPI(title=f"SAGA Agent: {config.AID}")
identity = load_identity()


class TokenRequest(BaseModel):
    aid: str
    device: str
    ip: str
    port: int
    pac_hex: str
    provider_signature_hex: str
    otk_hex: str


class MessageRequest(BaseModel):
    token_hex: str
    from_aid: str
    payload: dict


class CalendarEventRequest(BaseModel):
    summary: str
    start: str
    end: str
    attendees: list[str] | None = None


class SendRequest(BaseModel):
    target_aid: str
    payload: dict


@app.get("/health")
async def health():
    return {"status": "ok", "aid": identity.aid}


@app.post("/saga/token-request")
async def token_request(req: TokenRequest):
    """
    Receiver side of SAGA paper Section IV-E, steps 6-7: verify the
    initiator's Provider-signed identity, run the DH handshake using the
    OTK they present, and issue a fresh access token.
    """
    provider_vk = provider_client.get_provider_verify_key()
    if not crypto.verify_provider_signature(
        provider_vk, req.aid, req.device, req.ip, req.port, req.pac_hex, req.provider_signature_hex
    ):
        raise HTTPException(400, "invalid Provider signature on initiator identity")

    otk_private = identity.take_otk_private(req.otk_hex)
    if otk_private is None:
        raise HTTPException(403, "unknown or already-used OTK")

    box = crypto.make_box(otk_private, req.pac_hex)
    token = crypto.build_token(initiator_aid=req.aid)
    token_hex = crypto.encrypt_token(box, token)

    session_store.store_issued(token_hex, token, box)
    log.info(
        "issued session to %s (expires in %ds, qmax=%d)",
        req.aid,
        config.TOKEN_LIFETIME_SECONDS,
        token["qmax"],
    )

    return {"token_hex": token_hex, "expires_at": token["expires_at"], "qmax": token["qmax"]}



@app.get("/calendar/events")
async def calendar_events(time_min: str, time_max: str):
    try:
        return calendar_tool.list_events(time_min, time_max, max_results=100)
    except Exception as e:
        raise HTTPException(502, f"calendar read failed: {e}")


@app.post("/calendar/create")
async def calendar_create(req: CalendarEventRequest):
    try:
        return calendar_tool.create_event(req.summary, req.start, req.end, req.attendees)
    except Exception as e:
        raise HTTPException(502, f"calendar write failed: {e}")


@app.post("/saga/send")
async def saga_send(req: SendRequest):
    try:
        return peer_client.send_message(identity, req.target_aid, req.payload)
    except Exception as e:
        log.exception("peer request failed")
        raise HTTPException(502, f"peer request failed: {e}")


@app.post("/saga/message")
async def message(req: MessageRequest):
    """Receiver side of step 8: validate the token, enforce its limits,
    and hand the payload off to the agent's task logic."""
    session = session_store.get_issued(req.token_hex)
    if session is None:
        raise HTTPException(404, "unknown token")
    if session_store.is_expired(session):
        raise HTTPException(410, "token expired -- initiator must request a new one")
    if session["request_count"] >= session["qmax"]:
        raise HTTPException(429, "token request quota exhausted")
    if session["initiator_aid"] != req.from_aid:
        raise HTTPException(403, "token was not issued for this sender")

    session_store.bump_issued_request_count(req.token_hex)

    response_payload = handle_task_message(req.from_aid, req.payload)
    return {"status": "ok", "response": response_payload}


def handle_task_message(from_aid: str, payload: dict) -> dict:
    """
    Actual agent task logic. Currently handles one action type:
    "schedule_meeting" -- creates a real event on this agent's owner's
    Google Calendar. Anything else falls back to an echo, so the SAGA
    transport layer can still be exercised without touching Calendar.

    Expected payload for scheduling:
        {
            "action": "schedule_meeting",
            "summary": "Alice & Bob sync",
            "start": "2026-09-10T14:00:00+05:30",
            "end": "2026-09-10T14:30:00+05:30",
            "attendees": ["alice@example.com"]   # optional
        }
    """
    log.info("received task message from %s: %s", from_aid, payload)

    if payload.get("action") == "schedule_meeting":
        try:
            event = calendar_tool.create_event(
                summary=payload["summary"],
                start_iso=payload["start"],
                end_iso=payload["end"],
                attendees=payload.get("attendees"),
            )
            log.info("created calendar event %s for %s", event.get("id"), identity.aid)
            return {
                "status": "scheduled",
                "event_id": event.get("id"),
                "event_link": event.get("htmlLink"),
            }
        except Exception as e:
            log.exception("calendar write failed")
            return {"status": "error", "detail": str(e)}

    return {"echo": payload, "handled_by": identity.aid}

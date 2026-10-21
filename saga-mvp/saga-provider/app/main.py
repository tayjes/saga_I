"""
SAGA Provider -- minimal implementation.

Covers: user registration, agent registration (with signature
verification), contact policy management, agent deactivation, and
agent lookup / OTK issuance for initiating agents.

Deliberately out of scope for this MVP:
- OpenID Connect / human verification (uses plain password auth instead)
- CA-issued TLS certificates for agents (agents trust each other's
  pinned pubkeys returned by lookup, for now)
- Provider fault tolerance (RAFT) / sharding (Section V-A of the paper)
"""
import hashlib
import hmac
import json
import secrets

from fastapi import FastAPI, HTTPException

from app.crypto import PROVIDER_VERIFY_KEY_HEX, provider_sign, verify_signature
from app.db import agents_col, ensure_indexes, users_col
from app.models import (
    AgentRegisterRequest,
    OTKRefreshRequest,
    DeactivateRequest,
    LookupResponse,
    PolicyUpdateRequest,
    UserRegisterRequest,
)
from app.policy import match_budget

app = FastAPI(title="SAGA Provider (MVP)")


# ---------------------------------------------------------------- helpers
def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


async def _authenticate(uid: str, password: str) -> dict:
    user = await users_col.find_one({"uid": uid})
    if not user:
        raise HTTPException(404, "user not found")
    expected = _hash_password(password, user["salt"])
    if not hmac.compare_digest(expected, user["password_hash"]):
        raise HTTPException(401, "invalid credentials")
    return user


def _canonical_agent_info(aid: str, device: str, ip: str, port: int, pac_hex: str) -> bytes:
    payload = {"aid": aid, "device": device, "ip": ip, "port": port, "pac": pac_hex}
    return json.dumps(payload, sort_keys=True).encode()


@app.on_event("startup")
async def startup():
    await ensure_indexes()


@app.get("/health")
async def health():
    return {"status": "ok", "provider_verify_key_hex": PROVIDER_VERIFY_KEY_HEX}


# ---------------------------------------------------------------- users
@app.post("/users/register")
async def register_user(req: UserRegisterRequest):
    if await users_col.find_one({"uid": req.uid}):
        raise HTTPException(409, "uid already registered")
    salt = secrets.token_hex(16)
    await users_col.insert_one(
        {
            "uid": req.uid,
            "password_hash": _hash_password(req.password, salt),
            "salt": salt,
            "verify_key_hex": req.verify_key_hex,
        }
    )
    return {"status": "registered", "uid": req.uid}


# ---------------------------------------------------------------- agents
@app.post("/agents/register")
async def register_agent(req: AgentRegisterRequest):
    if not req.aid.startswith(f"{req.uid}:"):
        raise HTTPException(400, "aid must be formatted as '{uid}:{name}'")

    user = await _authenticate(req.uid, req.password)

    if await agents_col.find_one({"aid": req.aid}):
        raise HTTPException(409, "aid already registered")

    if len(req.otks) != len(req.otk_signatures):
        raise HTTPException(400, "otks and otk_signatures length mismatch")

    # Verify the agent-info signature
    info_bytes = _canonical_agent_info(req.aid, req.device, req.ip, req.port, req.pac_hex)
    if not verify_signature(user["verify_key_hex"], info_bytes, req.signature_hex):
        raise HTTPException(400, "invalid agent info signature")

    # Verify each OTK signature
    for otk_hex, sig_hex in zip(req.otks, req.otk_signatures):
        msg = f"{req.aid}:{otk_hex}".encode()
        if not verify_signature(user["verify_key_hex"], msg, sig_hex):
            raise HTTPException(400, f"invalid signature for OTK {otk_hex[:8]}...")

    provider_signature = provider_sign(info_bytes)

    await agents_col.insert_one(
        {
            "aid": req.aid,
            "uid": req.uid,
            "device": req.device,
            "ip": req.ip,
            "port": req.port,
            "pac_hex": req.pac_hex,
            "otks": [{"otk_hex": o, "used": False} for o in req.otks],
            "contact_policy": [r.model_dump() for r in req.contact_policy],
            "otk_counters": {},  # {initiator_aid: remaining_count}
            "signature_hex": req.signature_hex,
            "provider_signature_hex": provider_signature,
            "active": True,
        }
    )
    return {
        "status": "registered",
        "aid": req.aid,
        "provider_signature_hex": provider_signature,
    }


@app.put("/agents/{aid}/policy")
async def update_policy(aid: str, req: PolicyUpdateRequest):
    await _authenticate(req.uid, req.password)
    agent = await agents_col.find_one({"aid": aid})
    if not agent or agent["uid"] != req.uid:
        raise HTTPException(404, "agent not found or not owned by user")
    await agents_col.update_one(
        {"aid": aid},
        {
            "$set": {
                "contact_policy": [r.model_dump() for r in req.contact_policy],
                "otk_counters": {},  # reset budgets so the new policy takes effect
            }
        },
    )
    return {"status": "policy updated", "aid": aid}


@app.post("/agents/{aid}/deactivate")
async def deactivate_agent(aid: str, req: DeactivateRequest):
    await _authenticate(req.uid, req.password)
    agent = await agents_col.find_one({"aid": aid})
    if not agent or agent["uid"] != req.uid:
        raise HTTPException(404, "agent not found or not owned by user")
    await agents_col.update_one({"aid": aid}, {"$set": {"active": False}})
    return {"status": "deactivated", "aid": aid}


@app.post("/agents/{aid}/otks/refresh")
async def refresh_agent_otks(aid: str, req: OTKRefreshRequest):
    """Replace an agent's OTK pool after authenticating its owner."""
    if len(req.otks) != len(req.otk_signatures):
        raise HTTPException(400, "otks and otk_signatures length mismatch")
    if not req.otks:
        raise HTTPException(400, "at least one OTK is required")

    user = await _authenticate(req.uid, req.password)
    agent = await agents_col.find_one({"aid": aid})
    if not agent or agent["uid"] != req.uid:
        raise HTTPException(404, "agent not found or not owned by user")

    for otk_hex, sig_hex in zip(req.otks, req.otk_signatures):
        msg = f"{aid}:{otk_hex}".encode()
        if not verify_signature(user["verify_key_hex"], msg, sig_hex):
            raise HTTPException(400, f"invalid signature for OTK {otk_hex[:8]}...")

    await agents_col.update_one(
        {"aid": aid},
        {"$set": {
            "otks": [{"otk_hex": o, "used": False} for o in req.otks],
            "otk_counters": {},
        }},
    )
    return {"status": "refreshed", "aid": aid, "num_otks": len(req.otks)}


@app.get("/agents/{aid}/lookup", response_model=LookupResponse)
async def lookup_agent(aid: str, initiator_aid: str):
    """
    Called by an initiating agent (e.g. Alice) to get a receiving
    agent's (e.g. Bob) connection info and a fresh OTK, subject to
    Bob's contact policy (SAGA paper, Section IV-E, step 2).
    """
    agent = await agents_col.find_one({"aid": aid})
    if not agent or not agent["active"]:
        raise HTTPException(404, "agent not found or inactive")

    budget = match_budget(agent["contact_policy"], initiator_aid)
    if budget is None:
        raise HTTPException(403, "initiator not permitted by contact policy")
    if budget == -1:
        raise HTTPException(403, "initiator explicitly blocked")

    counters = agent.get("otk_counters", {})
    remaining = counters.get(initiator_aid, budget)
    if remaining <= 0:
        raise HTTPException(403, "OTK budget exhausted for this initiator")

    unused = [o for o in agent["otks"] if not o["used"]]
    if not unused:
        raise HTTPException(503, "agent has no unused OTKs left; ask owner to refresh")
    otk = unused[0]

    await agents_col.update_one(
        {"aid": aid, "otks.otk_hex": otk["otk_hex"]},
        {
            "$set": {
                "otks.$.used": True,
                f"otk_counters.{initiator_aid}": remaining - 1,
            }
        },
    )

    user = await users_col.find_one({"uid": agent["uid"]})

    return LookupResponse(
        aid=agent["aid"],
        device=agent["device"],
        ip=agent["ip"],
        port=agent["port"],
        pac_hex=agent["pac_hex"],
        otk_hex=otk["otk_hex"],
        user_verify_key_hex=user["verify_key_hex"],
        agent_signature_hex=agent["signature_hex"],
        provider_signature_hex=agent["provider_signature_hex"],
    )

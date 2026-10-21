import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from seed_data import load_signing_key, refresh_agent_otks

app = FastAPI(title="SAGA Seed Admin")
ADMIN_TOKEN = os.getenv("SEED_ADMIN_TOKEN", "")
CONFIG_PATH = Path(os.getenv("SEED_CONFIG_PATH", "/config/seed_config.json"))
KEYS_DIR = Path(os.getenv("SEED_KEYS_DIR", "/keys"))
PROVIDER_URL = os.getenv("PROVIDER_URL", "https://provider:8443")
VERIFY_TLS = os.getenv("PROVIDER_VERIFY_TLS", "false").lower() == "true"

class RefreshRequest(BaseModel):
    uid: str
    agent_name: str = "calendar_agent"
    num_otks: int = 10

def _authorize(token: str | None):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(401, "invalid seed admin token")

def _find_password(uid: str) -> str:
    config = json.loads(CONFIG_PATH.read_text())
    for user in config.get("users", []):
        if user.get("uid") == uid:
            return user["password"]
    raise HTTPException(404, f"user {uid!r} not found in seed config")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/refresh-otks")
def refresh_otks(req: RefreshRequest, x_seed_admin_token: str | None = Header(default=None)):
    _authorize(x_seed_admin_token)
    if req.num_otks < 1 or req.num_otks > 100:
        raise HTTPException(400, "num_otks must be between 1 and 100")
    password = _find_password(req.uid)
    signing_key = load_signing_key(req.uid, req.agent_name, KEYS_DIR)
    if signing_key is None:
        raise HTTPException(404, f"missing signing key for {req.uid}:{req.agent_name}")
    cfg = {"name": req.agent_name, "num_otks": req.num_otks}
    with httpx.Client(base_url=PROVIDER_URL, verify=VERIFY_TLS, timeout=15.0) as client:
        return refresh_agent_otks(client, signing_key, req.uid, password, cfg, KEYS_DIR, req.num_otks)

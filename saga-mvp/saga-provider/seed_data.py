"""
Seed the SAGA Provider with users, agents, and contact policies from a
JSON config file, instead of hand-editing a Python script each time.

Usage:
    python seed_data.py seed_config.json

For each agent registered, this generates real Ed25519 (signing) and
X25519 (access control + one-time) keypairs, registers the agent with
the Provider, and saves the PRIVATE halves to ./seed-keys/<aid>.json.
Agent containers will load these later to actually perform the
Diffie-Hellman handshake and decrypt tokens -- treat that directory as
a secret (it's gitignored-worthy, chmod 600 per file).

Safe to re-run: already-registered users/agents are skipped rather than
erroring out. Note: if a user already exists from a previous run, this
script can't register new agents for them in the same run, since it
doesn't have their original signing key -- load it from seed-keys/
instead (see load_signing_key() below) if you need to add agents later.
"""
import argparse
import json
from pathlib import Path

import httpx
from nacl.public import PrivateKey
from nacl.signing import SigningKey

import os

PROVIDER_URL = os.getenv("PROVIDER_URL", "https://localhost:8443")
VERIFY_TLS = os.getenv("PROVIDER_VERIFY_TLS", "false").lower() == "true"


def canonical_agent_info(aid, device, ip, port, pac_hex):
    return json.dumps(
        {"aid": aid, "device": device, "ip": ip, "port": port, "pac": pac_hex}, sort_keys=True
    ).encode()


def load_signing_key(uid: str, agent_name: str, keys_dir: Path) -> SigningKey | None:
    """Recover a previously-generated signing key so you can register
    additional agents for an already-registered user."""
    key_file = keys_dir / f"{uid}__{agent_name}.json"
    if not key_file.exists():
        return None
    data = json.loads(key_file.read_text())
    return SigningKey(bytes.fromhex(data["signing_key_seed_hex"]))


def register_user(client: httpx.Client, uid: str, password: str) -> SigningKey | None:
    signing_key = SigningKey.generate()
    resp = client.post(
        "/users/register",
        json={"uid": uid, "password": password, "verify_key_hex": signing_key.verify_key.encode().hex()},
    )
    if resp.status_code == 409:
        print(f"  user '{uid}' already registered, skipping")
        return None
    resp.raise_for_status()
    print(f"  registered user '{uid}'")
    return signing_key


def register_agent(client: httpx.Client, signing_key: SigningKey, uid: str, password: str, agent_cfg: dict, keys_dir: Path):
    name = agent_cfg["name"]
    aid = f"{uid}:{name}"

    pac = PrivateKey.generate()
    pac_hex = bytes(pac.public_key).hex()

    num_otks = agent_cfg.get("num_otks", 10)
    otk_privs = [PrivateKey.generate() for _ in range(num_otks)]
    otks = [bytes(k.public_key).hex() for k in otk_privs]
    otk_signatures = [signing_key.sign(f"{aid}:{o}".encode()).signature.hex() for o in otks]

    info = canonical_agent_info(aid, agent_cfg["device"], agent_cfg["ip"], agent_cfg["port"], pac_hex)
    signature_hex = signing_key.sign(info).signature.hex()

    resp = client.post(
        "/agents/register",
        json={
            "aid": aid,
            "uid": uid,
            "password": password,
            "device": agent_cfg["device"],
            "ip": agent_cfg["ip"],
            "port": agent_cfg["port"],
            "pac_hex": pac_hex,
            "otks": otks,
            "signature_hex": signature_hex,
            "otk_signatures": otk_signatures,
            "contact_policy": agent_cfg.get("contact_policy", []),
        },
    )
    if resp.status_code == 409:
        print(f"  agent '{aid}' already registered, skipping (edit its policy separately -- see README)")
        return
    resp.raise_for_status()
    provider_signature_hex = resp.json()["provider_signature_hex"]
    print(f"  registered agent '{aid}' with {len(otks)} OTKs and {len(agent_cfg.get('contact_policy', []))} policy rule(s)")

    keys_dir.mkdir(parents=True, exist_ok=True)
    key_file = keys_dir / f"{uid}__{name}.json"
    key_file.write_text(
        json.dumps(
            {
                "aid": aid,
                "device": agent_cfg["device"],
                "ip": agent_cfg["ip"],
                "port": agent_cfg["port"],
                "provider_signature_hex": provider_signature_hex,
                "signing_key_seed_hex": signing_key.encode().hex(),
                "pac_private_hex": bytes(pac).hex(),
                "otk_private_hexes": [bytes(k).hex() for k in otk_privs],
            },
            indent=2,
        )
    )
    key_file.chmod(0o600)
    print(f"  saved private keys -> {key_file}")


def refresh_agent_otks(client: httpx.Client, signing_key: SigningKey, uid: str, password: str, agent_cfg: dict, keys_dir: Path, num_otks: int | None = None):
    """Rotate an agent's OTK pool and atomically update its mounted private-key file."""
    name = agent_cfg["name"]
    aid = f"{uid}:{name}"
    key_file = keys_dir / f"{uid}__{name}.json"
    if not key_file.exists():
        # Check this before changing the Provider, or the public/private pools can diverge.
        raise FileNotFoundError(f"Cannot refresh {aid}: missing {key_file}")

    count = num_otks or agent_cfg.get("num_otks", 10)
    if count < 1 or count > 100:
        raise ValueError("num_otks must be between 1 and 100")

    data = json.loads(key_file.read_text())
    otk_privs = [PrivateKey.generate() for _ in range(count)]
    otks = [bytes(k.public_key).hex() for k in otk_privs]
    otk_signatures = [signing_key.sign(f"{aid}:{o}".encode()).signature.hex() for o in otks]

    # Prepare a protected temporary file before updating the Provider.
    updated_data = dict(data)
    updated_data["otk_private_hexes"] = [bytes(k).hex() for k in otk_privs]
    import tempfile
    fd, temp_name = tempfile.mkstemp(prefix=f".{uid}-{name}-otks-", dir=str(keys_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            json.dump(updated_data, temp_file, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_name, 0o600)

        resp = client.post(
            f"/agents/{aid}/otks/refresh",
            json={
                "uid": uid,
                "password": password,
                "otks": otks,
                "otk_signatures": otk_signatures,
            },
        )
        resp.raise_for_status()
        # Atomic replace means agents never read a partially-written key file.
        os.replace(temp_name, key_file)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise

    print(f"  refreshed {aid}: {len(otks)} OTKs -> {key_file}")
    return resp.json()

def run(config: dict, keys_dir: Path, client: httpx.Client):
    for user_cfg in config["users"]:
        uid = user_cfg["uid"]
        password = user_cfg["password"]
        print(f"User: {uid}")
        signing_key = register_user(client, uid, password)
        if signing_key is None:
            # user already existed -- try to recover a key for each agent
            for agent_cfg in user_cfg.get("agents", []):
                recovered = load_signing_key(uid, agent_cfg["name"], keys_dir)
                if recovered is None:
                    print(f"  no saved key for '{uid}:{agent_cfg['name']}', can't register it this run")
                    continue
                register_agent(client, recovered, uid, password, agent_cfg, keys_dir)
            continue
        for agent_cfg in user_cfg.get("agents", []):
            register_agent(client, signing_key, uid, password, agent_cfg, keys_dir)


def main():
    parser = argparse.ArgumentParser(description="Seed the SAGA Provider with users, agents, and contact policies")
    parser.add_argument("config", help="Path to a JSON config file (see seed_config.example.json)")
    parser.add_argument(
        "--keys-dir",
        default=str(Path(__file__).resolve().parent / "seed-keys"),
        help="Where to save/load generated private keys (default: seed-keys/ next to this script, "
        "regardless of your current directory)",
    )
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text())
    keys_dir = Path(args.keys_dir)

    with httpx.Client(base_url=PROVIDER_URL, verify=VERIFY_TLS) as client:
        run(config, keys_dir, client)


if __name__ == "__main__":
    main()
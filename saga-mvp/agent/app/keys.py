"""Loads identity/key material and refreshes OTKs from the mounted seed file."""
import json

from nacl.public import PrivateKey
from nacl.signing import SigningKey

from . import config


class AgentIdentity:
    def __init__(self, data: dict):
        self.aid = data["aid"]
        self.device = data.get("device")
        self.ip = data.get("ip")
        self.port = data.get("port")
        self.provider_signature_hex = data.get("provider_signature_hex")
        self.signing_key = SigningKey(bytes.fromhex(data["signing_key_seed_hex"]))
        self.pac_private = PrivateKey(bytes.fromhex(data["pac_private_hex"]))
        self.pac_public_hex = bytes(self.pac_private.public_key).hex()
        self._load_otks(data.get("otk_private_hexes", []))

    def _load_otks(self, private_hexes: list[str]):
        self._otk_privates = {}
        for h in private_hexes:
            priv = PrivateKey(bytes.fromhex(h))
            self._otk_privates[bytes(priv.public_key).hex()] = priv
        self._used_otks = set()

    def reload_from_disk(self):
        data = json.loads(config.KEY_FILE.read_text())
        self.provider_signature_hex = data.get("provider_signature_hex", self.provider_signature_hex)
        self._load_otks(data.get("otk_private_hexes", []))

    def take_otk_private(self, otk_hex: str) -> PrivateKey | None:
        if otk_hex in self._used_otks:
            return None
        priv = self._otk_privates.get(otk_hex)
        if priv is None:
            # A seed refresh updates the mounted JSON. Reload once before rejecting it.
            self.reload_from_disk()
            priv = self._otk_privates.get(otk_hex)
        if priv is None or otk_hex in self._used_otks:
            return None
        self._used_otks.add(otk_hex)
        return priv


def load_identity() -> AgentIdentity:
    if not config.KEY_FILE.exists():
        raise FileNotFoundError(f"No key file at {config.KEY_FILE} -- run seed_data.py first.")
    identity = AgentIdentity(json.loads(config.KEY_FILE.read_text()))
    if not identity.provider_signature_hex:
        raise ValueError(f"Key file for {identity.aid} has no provider_signature_hex.")
    return identity

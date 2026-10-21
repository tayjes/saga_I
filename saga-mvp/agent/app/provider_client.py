import httpx

from . import config

_provider_verify_key_hex: str | None = None


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.PROVIDER_URL, verify=config.PROVIDER_VERIFY_TLS, timeout=10.0)


def get_provider_verify_key() -> str:
    global _provider_verify_key_hex
    if _provider_verify_key_hex is None:
        with _client() as c:
            resp = c.get("/health")
            resp.raise_for_status()
            _provider_verify_key_hex = resp.json()["provider_verify_key_hex"]
    return _provider_verify_key_hex


def lookup_peer(initiator_aid: str, target_aid: str) -> dict:
    """Look up a peer and preserve Provider's status/body for auditable diagnostics."""
    with _client() as c:
        resp = c.get(f"/agents/{target_aid}/lookup", params={"initiator_aid": initiator_aid})
        if resp.is_error:
            raise RuntimeError(
                f"Provider lookup failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()

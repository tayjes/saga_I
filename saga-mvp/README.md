# SAGA Provider — MVP

A minimal, working implementation of the SAGA Provider: user registration,
agent registration with real Ed25519 signature verification, contact-policy
enforcement, and OTK issuance on lookup. This is Phase 1 of the build plan —
no proxy or agents yet, just a Provider you can register against and query.

## Project layout

```
.
├── docker-compose.yml
├── certs/
│   └── generate_certs.sh      # self-signed TLS cert for local dev
└── saga-provider/
    ├── Dockerfile
    ├── requirements.txt
    ├── client_example.py      # working example: register + lookup
    └── app/
        ├── main.py            # FastAPI endpoints
        ├── crypto.py          # Provider signing key + signature verification
        ├── models.py          # request/response schemas
        ├── policy.py          # contact-policy matching
        └── db.py              # MongoDB (motor) connection
```

## Setup

1. **Generate a TLS cert** (self-signed, for local dev):
   ```bash
   bash certs/generate_certs.sh
   ```

2. **Start everything**:
   ```bash
   docker compose up --build
   ```
   This starts MongoDB and the Provider on `https://localhost:8443`.
   The Provider's own signing keypair is generated on first boot and
   persisted in the `provider_keys` Docker volume — it survives restarts.

3. **Check it's alive**:
   ```bash
   curl -k https://localhost:8443/health
   ```

## Try it out

`client_example.py` shows the full client-side flow: generating Ed25519/X25519
keypairs, registering two users (Alice, Bob), registering their agents with
correctly-signed metadata and OTKs, and having Alice look up Bob (which
checks Bob's contact policy and issues an OTK).

```bash
pip install pynacl httpx
python saga-provider/client_example.py
```

Expected output ends with a successful lookup response containing Bob's
endpoint info and a fresh OTK.

## API summary

| Endpoint | Purpose |
|---|---|
| `POST /users/register` | Register a user (`uid`, `password`, Ed25519 verify key) |
| `POST /agents/register` | Register an agent under a user, with a signed contact policy and a batch of signed OTKs |
| `PUT /agents/{aid}/policy` | Update an agent's contact policy (resets OTK budgets) |
| `POST /agents/{aid}/deactivate` | Deactivate an agent (owner only) |
| `GET /agents/{aid}/lookup?initiator_aid=...` | Initiating agent requests target's info + a fresh OTK; enforced by the target's contact policy |

## What's simplified vs. the full SAGA paper (intentionally, for MVP speed)

- **Auth**: plain password + SHA-256 hash instead of OpenID Connect + human
  verification. Fine for a local MVP; swap in real OIDC before this touches
  anything real.
- **TLS certs for agents**: agents currently trust the pubkeys the Provider
  hands back on lookup, rather than a CA-issued cert chain. Add a small
  internal CA (even a self-signed root + `openssl ca`) if you want to match
  the paper exactly.
- **No RAFT/sharding**: single Provider instance. Not needed until you're
  benchmarking scalability like the paper does in Section VI-D.
- **Contact-policy specificity**: uses "literal character count" of the glob
  pattern as a proxy for the paper's specificity ranking — good enough for
  an MVP, but worth revisiting if you have overlapping wildcard rules.

## Signing scheme (needed for your agent/proxy code later)

Agent registration signs the JSON `{"aid", "device", "ip", "port", "pac"}`
with `sort_keys=True` compact encoding, using the user's Ed25519 signing key.
Each OTK is signed separately as the raw bytes of the string
`f"{aid}:{otk_hex}"`. `client_example.py` implements this exactly — reuse it
when you build the agent containers and the security proxy, since the proxy
will need to verify these same signatures when it validates tokens.

## Next

This gives you a working Provider. Next up: the two agent containers
(Alice/Bob, using `smolagents` + Google Calendar) doing the DH handshake and
token exchange directly over TLS — Phase 1 of the build plan — before
inserting the security proxy in Phase 2.


## Meeting UI and OTK management

The Docker Compose stack includes a local web UI on port **8080**:

- Overview: http://localhost:8080/
- Alice dashboard: http://localhost:8080/alice
- Bob dashboard: http://localhost:8080/bob

Each dedicated dashboard locks the sender to its own agent, schedules a meeting with the other agent, checks calendar availability, and exposes an OTK refresh control for that agent. Refreshing OTKs updates the Provider pool and the matching private keys in the mounted key file. The page also includes a Groq-powered natural-language assistant that uses function/tool calling to check both calendars and then invokes the existing SAGA meeting workflow to book a slot.

### Local setup

From this directory:

1. Create local environment settings and replace the admin-token placeholder with a long random value. Add your Groq API key to `GROQ_API_KEY` (get one from [Groq Console](https://console.groq.com/keys)); `GROQ_MODEL` defaults to `openai/gpt-oss-20b` and can be changed to another tool-calling model available to your account:
   ```bash
   cp .env.example .env
   ```
2. Create the local seed configuration and set real local-only passwords:
   ```bash
   cp saga-provider/seed_config.example.json saga-provider/seed_config.json
   ```
3. Make sure the local Google service-account JSON exists at `secrets/gcal-service-account.json`, and that the local certificates and agent key files exist.
4. Start the stack:
   ```bash
   docker compose up --build -d
   ```
5. Open one of the URLs above. To inspect startup issues, use `docker compose logs --tail=100 ui seed-admin provider alice-agent bob-agent`.

Do not commit `.env`, `seed_config.json`, Google credentials, private keys, or generated local key files. The admin token protects the seed-admin endpoint; keep it private and use this stack for local development only.


### Groq meeting assistant

Use the natural-language panel on the overview, Alice, or Bob dashboard. State the meeting title, duration, preferred date/time, and any constraints. The assistant uses Groq tool calling to invoke `check_availability` against both calendars before it can invoke `schedule_meeting`; the backend rejects booking a slot that was not returned as free. It asks follow-up questions when required details are missing. It only creates calendar events when the user explicitly asks to book or schedule. Keep `GROQ_API_KEY` in your local `.env` only—never commit the real key.

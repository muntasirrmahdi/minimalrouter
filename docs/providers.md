# Providers

Rows, fallback, the registry, OAuth profiles, and the per-provider traps that
actually bite. Status lines are dated; free upstreams rot independently of the
router, so re-probe before relying on any row.

## Rows

Rows live in `~/.minimalrouter/rows.json` (a JSON list, mode 600) and are read on
every request — a hand edit or dashboard change takes effect immediately.

```json
[
  {
    "name": "groq-qwen3.8-27b",
    "base": "https://api.groq.com/openai/v1",
    "model": "qwen/qwen3.8-27b",
    "key_env": "GROQ_API_KEY"
  },
  {
    "name": "kilocode-free",
    "base": "https://api.kilo.ai/api/openrouter",
    "model": "kilo-auto/free",
    "key_env": ""
  }
]
```

| Field | Meaning |
|---|---|
| `name` | unique label shown in the dashboard and the log |
| `base` | provider base URL, with or without a trailing `/v1` |
| `model` | model id to request |
| `key_env` | environment variable holding the key (process env first, then `~/.hermes/.env`) |
| `key` | inline key, used for rows added through the dashboard |
| `aliases` | optional extra model ids that route to this row |

A `key_env` value never lands in the file; inline keys do, hence mode 600.

## Fallback

Rows sharing a model id (or listing each other's ids in `aliases`) form a chain in
file order. 401/402/403/429/5xx/timeout moves to the next row. A exhausted chain
returns 502 with the last error — including a snippet of the upstream's own response
body, so the vendor's reason and suggested fix survive. Every attempt is logged to
`~/.minimalrouter/minimalrouter.log`.

## The registry

`provider-registry.json` maps a tool name to `{label, base_url, models, api_mode?}`
plus curated flow fields only when the flow breaks convention. It is data, not a
whitelist: the generic paste-a-base-URL path always stays open. The installer seeds
it once; learned entries survive upgrades.

Standing rules for growing it (one tool at a time):

1. **Probe keyless first.** POST a completion with the tool's free model and no
   Authorization header. Some gateways serve free models anonymously; accept only a
   real completion, never a bare 200 (canned-text 200s are common).
2. **Primary source only.** The tool's own code/docs are the authority for base URL
   and login endpoints. A named tool is a lead, not a finding.
3. **Live-probe before adding.** The authorize endpoint must return a real
   `device_code` for `{}`; the token endpoint must answer that code (pending counts).
   A 422 listing required body fields is a shape bug, not a dead flow.
4. **Login-verified is not routable.** Client-fingerprint gates can sit after token
   validation. Only a real approved token proves routability.
5. **ToS-risky providers stay out of the public seed.** Wire them live-only with
   `routable:false` and the ban note; do not ship the entry to other users.

## OAuth profiles

`oauth add` (type a name or paste a base URL once) creates a profile in
`oauth-providers.json`; `oauth <id>` runs the device login; tokens land in
`oauth.json` (600, gitignored). Profile rows join `/v1/models` at runtime and are
never written into `rows.json`.

| Field | Meaning |
|---|---|
| `authorize_url` | device-code endpoint (returns code + verification URL) |
| `token_url` | poll endpoint; may contain `{device_code}` or take it appended |
| `refresh_url` | optional; called when the access token expires |
| `base_url` | OpenAI-compatible base for chat |
| `models` | list of ids, or objects `{model, aliases?}` |
| `api_mode` | `chat_completions` (default), `responses`, `zcode`, `omnirush` |
| `chat_path` | per-row chat path override when the base already carries a path |
| `headers` | extra headers; `{token}` replaced at request time |
| `authorize_body` / `token_body` | extra JSON; `{device_code}` / `{refresh_token}` substituted |
| `poll_method` | `POST` (default) or `GET` (appends `/<device_code>`) |
| form-encoded variants | `authorize_form`, `token_form`, `token_headers`, `refresh_headers`, `refresh_body` |
| `client_secret_mode` | `random_hex` mints a client bearer that must match between init and poll |

Poll contract: pending answers (202/400/401/409/428 with pending codes) keep waiting;
credentials may arrive as `access_token`, `token`, `session_token` or `api_key`,
possibly wrapped in `data`. Refresh contract: rotating refresh tokens are persisted
atomically; mirror a rotated pair back to any other consumer of the same account
(the vendor CLI) or its next refresh signs the user out.

## Keep in mind — the traps

**A client gate is not a token problem.** `omnirush` and `opencode` both
complete a real device login and then refuse non-official clients at the API step.
Re-approval and refresh do not fix a client gate. The router reports the vendor's
literal error; the registry carries `routable:false` with the evidence.

**`dl` (Directionally Local) — REMOVED 2026-11-19.** Dropped from the pool and the
registry after its free device tokens became client-gated (403 `dl_only`: "Free tokens
work inside dl only"). Refresh succeeded but the client was refused, so re-approval
could not fix it. The free models still work inside the dl CLI itself; a paid dl API
key as an API-class row is the only router-side path back (untested).

**`dahl` is an API-class row, not OAuth.** Inline key, no token to expire; it is a
paid/credit endpoint — watch the balance, not an expiry. It intermittently 429s with
`model_concurrency_limit` (paid-first upstream); the chain moves on.

**Keyless free tiers are rationed.** A successful call followed by 429/503 seconds
later is normal. The seeded `kilocode-free` row works with zero setup; pollinations
also serves keyless upstream but is not a seeded row. Some kilocode profile models
(`baidu/ernie-5.5-flash-preview:free`, `nvidia/nemotron-3.5-super-vl:free`,
`inclusionai/ling-3.2-flash:free`) serve keyless-open upstream even without a token;
`kilo-auto/efficient` and `liquid/lfm-3-1.7b:free` need auth. Reasoning models can
spend a small `max_tokens` budget before writing content — a 200 with null content
is an upstream property, raise the budget (>= 200-300).

**`zcode` (z.ai plan lane).** OAuth class, native translator, verified serving. The
plan JWT has no `exp` claim — it does not expire, and there is no refresh token;
`refresh` honestly says "re-login". Free plan grants are public and daily-rationed.
The identity asset ships in `bridges/zcode-identity.json`; a clean install without it
would send an empty identity and earn 405 code 3012.

**`inference.sh`.** Connects (real device flow, token stored) but the account balance
is $0.00 — every model answers 402 `insufficient_quota`. No free models exist there.
A stored token is not a working route; credits are required. The stored session token
also expires with no refresh token — re-login is needed before any paid probe.

**`omnirush`.** OAuth login works; routing does not. With a real approved token the
gateway answers SSE `event:error` `official_client_required` to every non-official
client (current CLI version header, browser UA, older pinned version alike). The
native translator is implemented and unit-tested; the registry stays `routable:false`.
Community bridges claiming otherwise are likely broken or pin an older gateway.

**`opencode`.** Sidelined: free Zen models are client-locked (429
`FreeUsageLimitError` to every non-OpenCode client; UA/header spoof does not bypass).
The device flow itself is real. Not a working free OAuth preset.

**`freebuff`.** API class via a personal loopback bridge — **not shipped in this
repo**. The vendor's ToS (effective 2026-11-12) prohibits wrappers, relays,
automation and multiple accounts, enforced with identity verification and device
fingerprinting. Personal, single-account, loopback use only, at the user's own risk;
the live-only registry entry is `routable:false` with the ban note. The bridge needs
a one-time `--login` and `--serve`; the free path needs session admission + agent-run
+ a byte-exact system marker + `cost_mode:free` inside the bridge — a bare token never
routes. Without login it answers an honest "not logged in" error, which the router
now propagates verbatim in the failure envelope.

**Refreshing can honestly fail.** `r` in the dashboard and `minimalrouter refresh`
call the real refresh endpoint. Providers with no refresh token (zcode plan JWT,
inference session token) say so plainly; API/keyless rows have no token at all —
press `t` to re-test. A successful refresh never implies the route now works.

## Status at a glance (2026-11-19)

| Provider | Class | State |
|---|---|---|
| kilocode-free (seed) | keyless | works, rationed/flaky |
| groq (x2 rows) | API | works, fast |
| gwarden | API | works, slow reasoner (~10s) |
| dahl | API | works; intermittent upstream 429; watch balance |
| clixad | API | works |
| nous | OAuth | works — four `:free` models; single-use rotating refresh |
| zcode | OAuth | works — native translator, no-expiry plan JWT |
| kilocode profile | OAuth | not connected (no token); 3 models serve keyless-open anyway |
| dl | — | REMOVED 19 Nov (pool + registry); free tokens client-gated; use the dl CLI |
| omnirush | OAuth | auth only; `official_client_required`; routable:false |
| opencode | OAuth | sidelined; free tier client-locked |
| inference | OAuth | connected, token expired, $0.00 balance — credits + re-login needed |
| freebuff | API (bridge) | live-only, not shipped; needs --login; ToS risk |

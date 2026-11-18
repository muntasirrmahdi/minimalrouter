# MinimalRouter

One OpenAI-compatible endpoint over as many API providers as you want, with a
terminal dashboard. One file, Python standard library only, no dependencies,
no server authentication, localhost only. Provider logins (OAuth device flow)
are supported through profiles.

Free tiers are scattered across a dozen providers: each has its own base URL,
its own key, its own rate limit, and its own way of dying quietly. MinimalRouter
puts them behind a single endpoint, keeps trying the next one when a row fails,
and shows you the state of every row in your terminal.

```
 MINIMALROUTER  |  http://127.0.0.1:8790/v1  |  8 rows  8 models  |  up 4m12s  |  no server auth / localhost only
   NAME                        MODEL                          HOST               LAT      LAST
------------------------------------------------------------------------------------------------------
 > * groq-qwen3.8-27b          qwen/qwen3.8-27b                api.groq.com      343ms     0s
   * groq-gpt-oss-20b          openai/gpt-oss-20b              api.groq.com      417ms     1m
   * or-nemotron-free          nvidia/nemotron-3.5-super-vl:free openrouter.ai   816ms     1m
   * pollinations-openai-fast  openai-fast                     text.pollinations. 4.2s     2m
   o or-gemma-free             google/gemma-4-31b-it:free      openrouter.ai      -       -
|- SELECTED --------------------------------------------------------------------------------------------
 name   groq-qwen3.8-27b
 model  qwen/qwen3.8-27b
 base   https://api.groq.com/openai/v1
 key    env:GROQ_API_KEY
 state  OK  343ms  last 0s
 error  -
|- EVENTS ----------------------------------------------------------------------------------------------
 OK  qwen/qwen3.8-27b via groq-qwen3.8-27b 0.3s
 FAIL google/gemma-4-31b-it:free via or-gemma-free: HTTP Error 429 (1.0s) -> next
 up/down select  t test  a add  d delete  r refresh  q quit (stops server)  Esc cancel
```

## Why

If you run agents or coding tools, you probably have several free or cheap
endpoints and no single place to use them. MinimalRouter gives you:

- **one base URL** for every client that speaks the OpenAI format
- **ordered fallback** per model: 401, 402, 403, 429, 5xx and timeouts move to
  the next row automatically
- **a live view** of what is alive, how fast, and why something failed
- **an add flow in three keystrokes** that tests before it saves

It is deliberately small: one Python file, no pip install, no database, no web
UI, no server auth layer, nothing to configure beyond a JSON file of rows and,
if you want provider logins, an OAuth profile.

## Requirements

- Python 3.8 or newer (standard library only)
- Linux or macOS. On Windows, use WSL.
- A terminal. That is the whole user interface.

## Install

One line (installs to `~/.minimalrouter`, launcher to `~/.local/bin`):

```sh
curl -fsSL https://raw.githubusercontent.com/muntasirrmahdi/minimalrouter/main/install.sh | sh
```

Or clone and install from the checkout:

```sh
git clone https://github.com/muntasirrmahdi/minimalrouter
cd minimalrouter
sh install.sh
```

If `~/.local/bin` is not on your `PATH`, add it:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && exec $SHELL
```

Requirements check and install, start to finish, takes under a minute.

## Quickstart

```sh
minimalrouter
```

That starts the server and opens the dashboard. The example rows file ships with
keyless free-tier entries — the first row, `kilocode-free`, works with no
setup at all — plus entries that need a key, which stay `unknown` until you
give one. A failed row never blocks the others.

Add your first provider:

1. Press `a`
2. Paste the base URL, for example `https://api.groq.com/openai/v1`
3. Paste your API key, then press Enter
4. The model list is fetched from the provider: type to filter, Enter to pick
5. The row is tested; it is saved only if the test passes

Quickstart for OAuth instead of a key: `minimalrouter oauth add` once, then
`minimalrouter oauth <profile-id>`, and the profile's models join `/v1/models`.

Press `t` to re-test the selected row at any time. Press `q` to quit.

For a long-running endpoint without the dashboard:

```sh
minimalrouter serve
```

## Dashboard keys

| Key | Action |
|---|---|
| Up / Down | select a row |
| `t` | re-test the selected row (writes state, latency and last-checked) |
| `a` | add a row: base URL, key, pick a model, auto-test, save |
| `d` | delete the selected row |
| `r` | refresh now |
| `q` | quit (also stops the server) |
| Esc | cancel any prompt |

The dashboard shows the header (endpoint, counts, uptime, online state), the row
table, a details pane for the selected row (full base URL, key source, last
error), and a live tail of the request log.

## API

Base URL: `http://127.0.0.1:8790/v1`. There is no server authentication: any
string works as an API key from your own machine. Provider logins are separate —
see [OAuth providers](#oauth-providers).

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI-format chat, streaming and non-streaming |
| GET | `/v1/models` | every pooled model id |
| GET | `/health` | rows, models, port |
| GET | `/api/state` | rows plus per-row state (what the dashboard reads) |
| POST | `/api/test` | `{base, key, model}` probe, does not save |
| POST | `/api/fetch_models` | `{base, key}` returns the provider's model ids |
| POST | `/api/add` | `{base, key, model}` tests, then saves |
| POST | `/api/remove` | `{name}` |

```sh
curl http://127.0.0.1:8790/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen/qwen3.8-27b","messages":[{"role":"user","content":"hi"}]}'
```

Streaming is a passthrough: `"stream": true` returns the provider's SSE chunks
byte for byte, including reasoning deltas.

## Rows

Rows live in `~/.minimalrouter/rows.json` and are read on every request, so a
hand edit or a dashboard change takes effect immediately with no restart.

```json
[
  {
    "name": "groq-qwen3.8-27b",
    "base": "https://api.groq.com/openai/v1",
    "model": "qwen/qwen3.8-27b",
    "key_env": "GROQ_API_KEY"
  },
  {
    "name": "pollinations-openai-fast",
    "base": "https://text.pollinations.ai/openai",
    "model": "openai-fast",
    "key_env": ""
  }
]
```

| Field | Meaning |
|---|---|
| `name` | unique label shown in the dashboard and the log |
| `base` | provider base URL, with or without a trailing `/v1` |
| `model` | model id to request; aliases are also accepted as model ids |
| `key_env` | name of an environment variable holding the key |
| `key` | inline key, used for rows added through the dashboard |
| `aliases` | optional list of extra model ids that should route to this row |

Keys are resolved at request time from the process environment first, then from
`~/.hermes/.env` if it exists. A key stored as `key_env` never lands in
`rows.json`. Inline keys do, so the file is written `chmod 600`.

Set the environment variable your rows name, for example:

```sh
export GROQ_API_KEY="..."
export OPENROUTER_API_KEY="..."
```

## Fallback

Rows that share a model id, or that list each other's ids in `aliases`, form a
chain in file order. On 401, 402, 403, 429, a 5xx, or a timeout the next row in
the chain is tried. If the whole chain fails the client gets a 502 with the last
error. Every attempt is written to `~/.minimalrouter/minimalrouter.log`.

```
OK  qwen/qwen3.8-27b via groq-qwen3.8-27b 0.3s
FAIL qwen/qwen3.8-27b via backup-row: HTTP Error 429 (1.0s) -> next
```

## Use it from other tools

Any OpenAI-compatible client works with the base URL:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8790/v1", api_key="unused")
print(client.chat.completions.create(
    model="qwen/qwen3.8-27b",
    messages=[{"role": "user", "content": "hello"}],
).choices[0].message.content)
```

Most coding agents accept a custom OpenAI endpoint: set the base URL to
`http://127.0.0.1:8790/v1`, the API key to any string, and the model to one of
the ids from `/v1/models`.

Hermes Agent custom provider:

```yaml
custom_providers:
  - name: minimalrouter
    base_url: "http://127.0.0.1:8790/v1"
    api_key: "unused"
    api_mode: "chat_completions"
    model: "qwen/qwen3.8-27b"
```

## OAuth providers

Adding an OAuth-backed CLI tool is one word when the tool is known, and a short
guided prompt when it is new. Two paths:

**Known tool — type the name:**

```sh
minimalrouter oauth add          # then type the tool's name
```

If the tool is in `provider-registry.json`, the base URL and models are
pre-filled; the router then auto-discovers the device login endpoints, you
confirm, and you're connected:

```sh
minimalrouter oauth myprovider     # prints a URL + code, waits for approval
minimalrouter oauth list           # profile + connected state
minimalrouter oauth rm myprovider  # remove profile and its token
```

**New tool — paste the base URL once:**

If the name is not in the registry yet, the same command asks for the base URL
(the one you would use for chat). The router probes the host's OIDC discovery
document and the usual device-auth paths, and accepts an authorize endpoint
**only when the server actually returns a device code for it**; the token
endpoint is confirmed by the way it answers that code. You see what it found
and confirm. If nothing answers, it asks for the authorize and token URLs, so a
provider that follows no convention is still addable — nothing is locked out.

The name, base URL and models are then **remembered in
`~/.minimalrouter/provider-registry.json`**, so the next add of the same tool
is a single word. The registry is data, not a whitelist: append an entry by
hand or pull request and the tool becomes one-word-connectable for everyone.
The repo ships a seed registry; entries are usually minimal (`label`,
`base_url`, `models`, optional `api_mode`) because the login endpoints are
discovered live. When a provider's device flow does not follow a convention, the
entry may also carry curated `authorize_url` / `token_url` / `poll_method`
values; those override discovery and are still data, not code.

**By hand:** copy `~/.minimalrouter/oauth-providers.example.json` to
`~/.minimalrouter/oauth-providers.json` and edit the `profiles` object. Same
fields as the prompts above.

Profile fields:

| Field | Meaning |
|---|---|
| `authorize_url` | device-code endpoint that returns a code and a verification URL |
| `token_url` | endpoint polled with `device_code` until it returns an access token |
| `refresh_url` | optional; called with the refresh token when the access token expires |
| `base_url` | OpenAI-compatible base for chat requests |
| `models` | list of model ids, or objects with `model` + `aliases` |
| `api_mode` | `chat_completions` (default) or `responses` for Responses-style upstreams |
| `headers` | optional extra headers; `{token}` is replaced at request time |
| `authorize_body` | optional extra JSON sent to `authorize_url` |
| `token_body` | optional static poll body; `{device_code}` is replaced at poll time |
| `poll_method` | `POST` (default) or `GET`; GET appends `/<device_code>` to `token_url` |

Tokens land in `~/.minimalrouter/oauth.json` (mode 600, gitignored) and the
profile's models appear in `/v1/models`. Expired tokens are refreshed on demand
through `refresh_url`. Rows that come from a profile are never written into
`rows.json`; delete the profile to remove them.

Claude Code is the one notable format exception: it speaks the Anthropic
`/v1/messages` format, which this router does not serve. Pointing Claude Code at
it will fail with 404. Use an Anthropic-format router for that client, or wait
for the shim on the roadmap.

Because the server binds `127.0.0.1` and has no server auth, it is reachable only from
your own machine. Do not expose it to a network. OAuth tokens stay in
`~/.minimalrouter/oauth.json` (600) and never enter the repo.

OAuth-backed rows use the saved access token and rotate it when the profile
provides a refresh endpoint. A background refresh scheduler is still a
follow-up; requests refresh on demand.

Security boundary: MinimalRouter deliberately does not auto-import tokens from
another app's credential store. Connect an account through
`minimalrouter oauth <provider>` so the provider's own device flow is visible
and the resulting token is stored locally.

## Three connection classes

One engine path, three ways a row gets its credential. The class is **derived from the
row's actual shape**, never from a label — a row cannot claim to be keyless to skip auth,
or OAuth to borrow a token.

| Class | How it authenticates | Where it lives | How to add |
|---|---|---|---|
| **API** | a key or `key_env` | `rows.json` | dashboard `a`, or `POST /api/add` |
| **OAuth** | device-flow token | profile + `oauth.json` | `minimalrouter oauth add` → `oauth <id>` |
| **Keyless** | nothing (no Authorization header) | `rows.json` with empty key | `POST /api/add` without a key, or ship it in `rows.example.json` |

`GET /health` returns a `classes` count, `GET /api/state` returns `class` per row, and the
dashboard shows a CLASS column plus the class in the SELECTED panel. `oauth list` prints the
class of each profile.

Keyless is **not** a separate auth mechanism in code — it is the existing file-row path with
no credential. Do not add an `auth: none` branch to the router.

## Keep in mind — DL and provider classes

These are the traps that actually bite. Read them before you pick a provider.

**Directionally Local (`dl`) is an OAuth-class provider, and its device token expires.**
It is not an API-key row: you sign in once through `minimalrouter oauth dl` and the
router stores the token locally. When that token dies, the router attempts a refresh
on **every** request and logs `OAUTH-REFRESH dl failed: HTTP 401` — all four of its
models (`gpt-6-luna`, `gpt-6.1-sol`, `gpt-6-astra`,
`deepseek/deepseek-v4.1-flash-1017`) then fail with 502 even though other rows work.
The device flow itself is healthy (a real `user_code` is issued); the fix is a
re-approval, not a reinstall:

```sh
minimalrouter oauth dl       # prints a URL + user code, waits for browser approval
```

If you also use the dl/Autopilot CLI, the router rotates the refresh token on
refresh — mirror the new pair back to that CLI's config or it will sign you out.
No install of the dl CLI is needed to route through MinimalRouter.

**`dahl` is an API-class row, not OAuth.** It carries an inline `dahl_...` key, so there
is no device login and no token to expire. Verified live 2026-11-18: real completion,
correct model echo, ~4s. Its key is stored in `~/.minimalrouter/rows.json` (600); it
is a paid/credit endpoint, so watch its balance, not a token expiry.

**Three classes, one engine path.** API = paste a key; OAuth = browser approve;
Keyless = an API row with an empty key (kilocode, pollinations). Keyless is a
documentation label, not a third auth mechanism. Keyless free tiers are rationed —
a successful call followed by 429/503 seconds later is normal.

**freebuff is API class via a local bridge, and it carries a real ToS warning.** The bridge
(`~/.minimalrouter/bridges/freebuff_bridge.py`) must be logged in once (`--login`) and running
(`--serve`) before the row works; then MinimalRouter sees a plain loopback OpenAI endpoint with a
dummy key. The free path needs admission + agent-run + a byte-exact "You are Buffy..." marker +
`cost_mode:free` inside the bridge — a bare token never routes. **Warning from freebuff's own ToS
(effective 2026-11-12): direct endpoint access through wrappers/relays/automation and use of more
than one account are prohibited, enforced with identity verification and device fingerprinting.**
Personal, single-account, loopback use only; the registry entry is `routable:false` and was left
out of the public repo seed on purpose.

**Why refreshing a row can honestly fail.** In the dashboard, `r` refreshes the selected row's
OAuth token — it no longer just prints a timestamp. But two providers have nothing to refresh:
**zcode** mints a plan JWT with **no `exp` claim** (it doesn't expire) and no refresh token, so the
answer is "re-login with `minimalrouter oauth zcode`" — not a bug. **omnirush** refreshes fine, but
its gateway rejects every non-official client with `official_client_required`, so a fresh token
still won't route. API and keyless rows have no token at all — press `t` to re-test them instead.

**Commands you can run any time.** `minimalrouter stop` stops the running server/dashboard by
verified PID (never a pattern match — a same-named copy elsewhere is untouched). `minimalrouter
refresh [id]` forces an OAuth token refresh now; with no id it refreshes every profile. Providers
with no refresh token (inference.sh, zcode plan JWT) report that plainly instead of pretending.
`minimalrouter oauth list` shows every profile's class and connection state.

**inference.sh is registered but NOT yet a working route.** Device flow verified live
(POST `oauth/device_authorization` → real code; GET `device/auth/poll/{code}` → `pending`),
and `oauth add inference` resolves it from the registry in one word. But no human completed
the browser approval, so token→completion is UNVERIFIED. Keep in mind: run
`minimalrouter oauth inference`, approve in the browser, then test a real completion before
trusting it. Base is `/models/openai`, models are `namespace/name` refs from the official docs
(e.g. `openrouter/claude-sonnet-5`).

**Class status at a glance (2026-11-18):** `kilocode` keyless works but flaky;
`pollinations` keyless works but 402s quickly; `dahl` API works (~4s); `dl` OAuth
device flow re-approved and working (verified 2026-11-18 ~5:07 AM); `opencode`
is sidelined (`routable:false` — free tier is client-locked); `inference` OAuth preset
registered, completion pending human approval.

**OAuth login does not always mean OAuth class here.** `omnirush` and `freebuff`
both have real device-code logins, but a bare token cannot route: omnirush needs
a Responses-only translator with policed client headers; freebuff needs an
agent-run plus a system-message marker. When either runs through a local bridge,
register that bridge's loopback endpoint as an **API-class row** (base URL + a
static key) — the OAuth lives inside the bridge, not in MinimalRouter. Do NOT
add either as an OAuth preset: the profile would connect but every call would
fail. `opencode` stays in the sidebar for now.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| every row 404s | base URL already ends in `/v1` twice | the router normalises this; if it persists, check the provider path |
| 200 with empty content | reasoning model spent the token budget before writing | raise `max_tokens` to 200 or more |
| 429 on a `:free` row | provider rate limit | transient; the chain moves on, or add another row |
| 403 on every call | key group or plan gate | the key does not include that model |
| 402 | empty wallet or abuse gate | top up, or drop the row |
| `cannot bind 127.0.0.1:8790` | another instance is running | stop it, or set `MINIMALROUTER_PORT` |
| dashboard shows SERVER OFFLINE | the API is not answering | the process died; check `~/.minimalrouter/minimalrouter.log` |

`minimalrouter -h` prints usage; `minimalrouter -v` prints the version.

## Files

```
~/.minimalrouter/minimalrouter.py    the whole tool
~/.minimalrouter/rows.json           your rows (chmod 600)
~/.minimalrouter/rows.example.json          template copied on first install
~/.minimalrouter/oauth-providers.example.json generic OAuth profile template
~/.minimalrouter/provider-registry.json     name -> base URL/model map (seeded on install)
~/.minimalrouter/oauth-providers.json        optional personal provider profiles
~/.minimalrouter/oauth.json                   local OAuth tokens (chmod 600)
~/.minimalrouter/minimalrouter.log            per-request OK/FAIL log
~/.local/bin/minimalrouter                    launcher
```

## Uninstall

```sh
rm -f ~/.local/bin/minimalrouter
rm -rf ~/.minimalrouter
```

## Roadmap

- Anthropic `/v1/messages` shim so Claude Code can use the pool
- a systemd user unit for always-on use
- a background token-refresh scheduler
- weights or latency-based chain ordering
- a config flag for per-row concurrency limits

## License

MIT. See [LICENSE](LICENSE).

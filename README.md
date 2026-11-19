# MinimalRouter

One OpenAI-compatible endpoint over all your free API rows — a single Python file,
standard library only, zero dependencies, localhost only, with a terminal dashboard
built in. Provider logins (OAuth device flow) are first-class.

Free tiers are scattered across a dozen providers: each has its own base URL, its own
key, its own rate limit, and its own way of dying quietly. MinimalRouter puts them
behind one endpoint, keeps trying the next row when one fails, and shows you the state
of every row in your terminal.

| | |
|---|---|
| **One endpoint** | Every client that speaks the OpenAI format uses one base URL: `http://127.0.0.1:8790/v1`. No server auth, no lock-in, no pip install. |
| **Three connection classes** | API key, OAuth device flow, or keyless — one engine path. The class is derived from the row's actual shape, never from a label. |
| **Ordered fallback** | Rows sharing a model id form a chain. 401/402/403/429/5xx/timeout moves to the next row automatically, and the failure envelope carries the upstream's own error message. |
| **A real terminal dashboard** | Live row table with class, latency and last-checked; add/test/delete/refresh without leaving the TUI. |
| **One-word provider connects** | A name-to-endpoint registry makes a known tool a single word: `oauth add`, type the name, approve in the browser. Unknown tools stay addable by pasting a base URL once. |
| **Native translators** | Providers that need a transformed request (Responses-only gateways, Anthropic-Messages plan lanes) route through sibling transports in the same file — endpoints and identity assets are data, not code. |
| **Runs anywhere Python runs** | Linux, macOS, WSL2. One file, Python 3.8+, no database, no web UI, nothing to configure beyond a JSON file of rows. |

## Quick Install

### Linux, macOS, WSL2

```sh
curl -fsSL https://raw.githubusercontent.com/muntasirrmahdi/minimalrouter/main/install.sh | sh
```

Installs to `~/.minimalrouter`, launcher to `~/.local/bin`. If `~/.local/bin` is not on
your `PATH`:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && exec $SHELL
```

After installation:

```sh
minimalrouter        # server + terminal dashboard, one process
```

### Windows

Use WSL2 and the one-liner above. The native Windows dashboard is not supported yet
(the TUI needs curses); server mode and `stop` are written portably, and a native
installer is a possible future contribution — see [CONTRIBUTING.md](CONTRIBUTING.md).

### From a checkout

```sh
git clone https://github.com/muntasirrmahdi/minimalrouter
cd minimalrouter
sh install.sh        # installs from the checkout, no network needed
```

## Getting Started

```sh
minimalrouter              # dashboard + server
minimalrouter serve        # server only (tmux / systemd / background)
minimalrouter oauth add    # connect a provider account (device flow)
minimalrouter oauth list   # profiles, class, connection state
minimalrouter refresh      # force OAuth token refresh (all, or one id)
minimalrouter stop         # stop the running instance by verified PID
minimalrouter -h           # usage (-v for version)
```

The seeded rows file ships with a keyless free-tier entry first — `kilocode-free`
works with no setup at all — plus entries that need a key, which stay `unknown` until
you give one. A failed row never blocks the others.

Add your first provider from the dashboard:

1. Press `a`
2. Paste the base URL, for example `https://api.groq.com/openai/v1`
3. Paste your API key (leave blank for a keyless endpoint), press Enter
4. The model list is fetched from the provider: type to filter, Enter to pick
5. The row is tested; it is saved only if the test passes

Or connect an account instead of a key: `minimalrouter oauth add`, type the tool's
name (known tools pre-fill from the registry), approve in the browser, and the
profile's models join `/v1/models`.

## Three connection classes

One engine path, three ways a row gets its credential. The class is **derived from the
row's actual shape**, never from a label — a row cannot claim to be keyless to skip
auth, or OAuth to borrow a token.

| Class | How it authenticates | Where it lives | How to add |
|---|---|---|---|
| **API** | a key or `key_env` | `rows.json` | dashboard `a`, or `POST /api/add` |
| **OAuth** | device-flow token | profile + `oauth.json` | `oauth add` then `oauth <id>` |
| **Keyless** | nothing (no Authorization header) | `rows.json` with empty key | `POST /api/add` without a key |

`GET /health` returns a `classes` count, `GET /api/state` returns `class` per row, the
dashboard shows a CLASS column, and `oauth list` prints each profile's class. Keyless
is not a separate auth mechanism in code — it is the existing file-row path with no
credential.

## Use it from any client

```sh
curl http://127.0.0.1:8790/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"kilo-auto/free","messages":[{"role":"user","content":"hi"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8790/v1", api_key="unused")
print(client.chat.completions.create(
    model="kilo-auto/free",
    messages=[{"role": "user", "content": "hello"}],
).choices[0].message.content)
```

Most coding agents accept a custom OpenAI endpoint: base URL
`http://127.0.0.1:8790/v1`, any string as the API key, a model id from `/v1/models`.
Hermes Agent custom provider:

```yaml
custom_providers:
  - name: minimalrouter
    base_url: "http://127.0.0.1:8790/v1"
    api_key: "unused"
    api_mode: "chat_completions"
    model: "kilo-auto/free"
```

Claude Code is the one notable format exception: it speaks the Anthropic `/v1/messages`
format, which this router does not serve yet (404). The shim is on the roadmap.

## CLI quick reference

| Command | Purpose |
|---|---|
| `minimalrouter` | dashboard + server in one process |
| `minimalrouter serve` | headless server |
| `minimalrouter stop` | stop by verified PID (never a pattern match); clears a stale pid file only after the PID is proven dead |
| `minimalrouter refresh [id]` | force OAuth refresh now; providers with no refresh token say so plainly |
| `minimalrouter oauth add` | create a profile (registry name or a base URL once) |
| `minimalrouter oauth <id>` | sign in: prints URL + code, waits for browser approval |
| `minimalrouter oauth list` / `oauth rm <id>` | inspect / remove profiles |
| `minimalrouter -h` / `-v` | usage / version |

Dashboard keys: Up/Down select, `t` re-test, `a` add, `d` delete, `r` refresh the
selected row's OAuth token (API/keyless rows have none — use `t`), `q` quit (stops the
server), Esc cancel.

## API

Base URL `http://127.0.0.1:8790/v1`. No server authentication: any string works as an
API key from your own machine. Provider logins are separate (OAuth profiles).

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI-format chat, streaming and non-streaming |
| GET | `/v1/models` | every pooled model id |
| GET | `/health` | rows, models, classes, port |
| GET | `/api/state` | rows plus per-row state (what the dashboard reads) |
| POST | `/api/test` | `{base, key, model}` probe, does not save |
| POST | `/api/fetch_models` | `{base, key}` returns the provider's model ids |
| POST | `/api/add` | `{base, key, model, name?}` tests, then saves |
| POST | `/api/remove` | `{name}` |
| POST | `/api/refresh` | `{provider?}` force refresh; absent = every profile |

Streaming is a passthrough: `"stream": true` returns the provider's SSE chunks byte for
byte, including reasoning deltas. When a whole chain fails, the 502 envelope carries
the last row's error **including a snippet of the upstream's own response body**, so
the vendor's actual reason (and its suggested fix) survives the router boundary.

## Documentation

| Document | What's covered |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Engine map, request lifecycle, class system, translators, OAuth layer, lifecycle commands |
| [docs/providers.md](docs/providers.md) | Rows, fallback, registry, device-flow profiles, per-provider status and the traps that actually bite |
| [docs/development.md](docs/development.md) | Live-vs-repo workflow, offline tests, the live verification battery, release checklist |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Ground rules, code style, adding a provider, PR process |
| [SECURITY.md](SECURITY.md) | Trust model, scope, reporting, deployment hardening |

## Provider status

Free upstreams rot independently of the router; the full, dated status table lives in
[docs/providers.md](docs/providers.md). Current shape (2026-11-19):

- **Working:** seeded keyless `kilocode-free`; Groq free tier; Nous Portal `:free`
  models; the zcode plan lane through its native translator; several kilocode models
  that serve keyless-open upstream.
- **Client-gated (auth works, routing refused):** `omnirush`
  (`official_client_required`), `opencode` free tier (sidelined). A valid token does
  not imply a routable client — the router reports these honestly instead of pretending.
- **Removed:** `dl` (Directionally Local) — dropped from the pool and the registry
  2026-11-19 after its free device tokens became client-gated (403 `dl_only`). The free
  models still work inside the dl CLI itself; a paid dl API key is the only router-side
  path back.
- **Needs action:** `inference.sh` (connected, $0.00 balance — credits required);
  `freebuff` (personal loopback bridge, login required, ToS-risky — not shipped in
  this repo).

## Architecture

One file, three layers: a row chain (file rows + OAuth profile rows, read per
request), a credential-class derivation that no label can spoof, and sibling
transports for providers whose gateway needs a transformed body. Endpoints and
identity assets are data (`provider-registry.json`, `oauth-providers.json`,
`bridges/`), never provider names in code. Full map:
[docs/architecture.md](docs/architecture.md).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| every row 404s | base URL already ends in `/v1` twice | the router normalises this; if it persists, check the provider path |
| 200 with empty content | reasoning model spent the token budget before writing | raise `max_tokens` to 200 or more |
| 429 on a `:free` row | provider rate limit | transient; the chain moves on, or add another row |
| 403 on every call | key group, plan gate, or client gate | read the upstream body in the error envelope — it names the gate |
| 402 | empty wallet or abuse gate | top up, or drop the row |
| `cannot bind 127.0.0.1:8790` | another instance is running | `minimalrouter stop`, or set `MINIMALROUTER_PORT` |
| dashboard shows SERVER OFFLINE | the API is not answering | the process died; check `~/.minimalrouter/minimalrouter.log` |

## Files

```
~/.minimalrouter/minimalrouter.py             the whole tool
~/.minimalrouter/rows.json                    your rows (chmod 600)
~/.minimalrouter/rows.example.json            template copied on first install
~/.minimalrouter/oauth-providers.example.json generic OAuth profile template
~/.minimalrouter/provider-registry.json       name -> base URL/model map (seeded once)
~/.minimalrouter/oauth-providers.json         personal provider profiles
~/.minimalrouter/oauth.json                   local OAuth tokens (chmod 600)
~/.minimalrouter/minimalrouter.log            per-request OK/FAIL log
~/.minimalrouter/minimalrouter.pid            pid file (mode 600, cleared on stop)
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
- a background token-refresh scheduler (manual refresh already exists)
- weights or latency-based chain ordering
- a config flag for per-row concurrency limits

## Contributing

Contributions are welcome — read [CONTRIBUTING.md](CONTRIBUTING.md) first. The two
ground rules: the core stays one Python file on the standard library, and provider
names/endpoints stay in data, never in code. Search existing issues and PRs before
opening one; run the offline tests (`python -m unittest discover -s tests`) and the
flow you changed before submitting.

## Community

- [Issues](https://github.com/muntasirrmahdi/minimalrouter/issues) — bugs and feature requests (use the templates)
- [Security advisories](https://github.com/muntasirrmahdi/minimalrouter/security/advisories/new) — private vulnerability reports, see [SECURITY.md](SECURITY.md)

## License

MIT. See [LICENSE](LICENSE).

# Architecture

MinimalRouter's router core is deliberately one Python file (`minimalrouter.py`,
standard library only). The interactive dashboard's primary is the Textual "Mission
Control" board (`minimalrouter_dashboard.py`), which the installer bundles into a
dedicated venv so it works out of the box; the core's built-in stdlib curses board is
the automatic fallback when Textual is absent or the board crashes. This document maps the core file and the
request lifecycle. Function names are the anchors; line numbers are not quoted because
they drift.

## Design laws

These are the invariants every change is reviewed against:

1. **Core is one file, stdlib only.** No pip dependencies in `minimalrouter.py`, ever.
   A PR that adds a dependency to the core is closed. The Mission Control dashboard
   (`minimalrouter_dashboard.py`) uses Textual, but only as a guarded lazy import: the
   installer bundles it into a dedicated venv so the beautiful board works out of the
   box, and the core falls back to its curses board when that module or its dependency
   is absent or it crashes. The request path never touches it. Dev-only tooling
   (tests, CI lint) may use external tools; the shipped engine may not.
2. **No provider names or endpoints in code.** Names live in
   `provider-registry.json`; endpoints are discovered live or curated in
   `oauth-providers.json` / the registry. The generic engine never knows a provider
   by name. (Named sibling transports — `chat_from_zcode`, `chat_from_omnirush` —
   are the one deliberate exception: they implement a vendor wire contract, and even
   there the base URL comes from row/profile data.)
3. **Class is derived, never labeled.** `row_class()` inspects the row's actual
   shape: `oauth_provider` present -> oauth; else `key` or `key_env` -> api; else
   keyless. A spoofed `class` field can never skip auth or borrow a token. The
   registry's `class`/`routable` fields are display/documentation hints only and
   must never reach a request decision.
4. **Keyless is not an engine path.** It is the existing file-row path with an empty
   credential. Do not add an `auth: none` branch.
5. **The generic path always stays open.** A tool missing from the registry is still
   addable by pasting its base URL once. A closed whitelist is the failure mode.
6. **Fail loud at integration boundaries.** Unusable input is rejected where it is
   read, with a message naming the cause and the remediation — never a silent no-op,
   never a bare symptom. The failure envelope carries the upstream's own error body
   snippet for exactly this reason.
7. **Login-verified and routable are separate facts.** Some gateways bind tokens to
   their official client. Report the verdict honestly; never promote a source-only
   bridge claim to a shipped route.

## File map (top to bottom)

| Region | What lives there |
|---|---|
| Constants + state paths | `MR_HOME`, `ROWS_PATH`, `OAUTH_PATH`, `PID_PATH`, `ENV_PATH`, `VALID_CLASSES`, translator constants, `ROW_STATE`, `REQLOG` + `STAT_LOCK` (client-traffic ledger), `MODEL_CW`/`RUNTIME_CW` + `CW_KEYS` (context windows), `BOOT_TIME`, `PROBE_*` (background probe pool), locks |
| Env + row loading | `load_env()` (process env first, then `~/.hermes/.env`), `read_rows()` (per request — hand edits take effect immediately), `oauth_rows()` (profile models become rows at runtime), `all_rows()`, `BY_MODEL()` (model id + aliases -> chain) |
| Class + token helpers | `row_class()`, `class_label()`, `token_live()` (no-expiry JWTs never expire), `ep()` (base+path join without doubling `/v1`), `error_code()` (normalises the four OAuth error shapes) |
| Generic upstream | `upstream()` — one request path for every plain row; per-profile `chat_path` override; `oauth_headers()` for token/org headers |
| Translators | `chat_to_omnirush()` / `chat_from_omnirush()` (Responses-only, forced `stream:true`+`store:false`, policed client header, rotating control-plane refresh), `chat_to_zcode()` / `chat_from_zcode()` (Anthropic-Messages plan lane, 3-block system identity from `bridges/zcode-identity.json`, header bundle, business errors inside HTTP 200 mapped by `zcode_status()`), `oauth_chat()` (generic `api_mode: responses`) |
| OAuth device-flow layer | `discover_device_endpoints()` (OIDC + conventional paths, accepted only on a real device code), `oauth_add` CLI flow, poll loop (pending codes 202/400/401/409/428 keep waiting; `token`/`session_token`/`api_key`/`access_token` all accepted; `data` unwrapped), `oauth_refresh()` (generic body/header refresh, header-carried secrets, rotating vendor control-plane), `oauth_access_token()` (refresh-on-demand gated by `token_live()`) |
| HTTP handler | `do_GET`: `/health`, `/v1/models`, `/api/state`, OAuth status endpoints. `do_POST`: `/v1/chat/completions` (the chain loop), `/api/test`, `/api/fetch_models`, `/api/add`, `/api/remove`, `/api/disable`, `/api/refetch`, `/api/refresh`. `/v1/messages` is intentionally absent (404) — the Anthropic shim is roadmap |
| Dashboard | Two boards, same data. Primary (bundled by the installer): Textual module (`minimalrouter_dashboard.py`) in a dedicated `~/.minimalrouter/.venv` — centered max-width frame (hero zone dead-center, table left-flush inside) with PINNED panel heights and NO frame scroll (the frame's `overflow-y: auto` would size it to content and break `fr` resolution, so panels that do not fit DROP OUT instead of being pushed off-screen). Tier from the live screen height — micro < 32 rows (1-line wordmark, 1-line numbers, EVENTS dropped, SELECTED kept), compact 32-39, airy >= 40 (4-row wordmark, 3-row pixel stat face, both secondary panels) — re-decided on `on_idle` AND every poll so it converges regardless of event order. Only ROUTES is flexible (`1fr` + `min-height`), because a Vertical child inside an auto-height container defaults to `1fr` and never resolves — that defect starved the table to border-only in 1.2.0-1.2.2), block-pixel wordmark with shimmer, 8 pixel-digit stat tiles (5x5 face) fed by the engine's `pool_stats` (rows/models/serving/down/req-min/tok-s/err%/uptime; serving+down+unknown partitions rows — a parked or errored row is never invisible; the curses strip carries the same DOWN aggregate), dense table ROW/PROV/CLASS/MODEL/CW/LAT(sparkline)/TPS/OK%/STATUS with responsive column drops, multi-row marks + batch test/delete(with confirm)/disable, SELECTED panel with TPS+CW pixel gauges, scrollable event tail (offset-tracked log reader, rotation-safe), in-board add via modal, 14 themes + density + motion toggles, help overlay, offline-tolerant polling, theme/density/motion persisted to `dashboard.json`. Fallback: the core's stdlib curses board (`_curses_main`), same data minus the true-color polish — used automatically when the venv/Textual is absent or the board crashes. Both read `/api/state`; the core picks Textual via `_load_textual_dashboard()` (guarded lazy import, validates the entry point is callable, `MINIMALROUTER_TUI=auto\|textual\|curses`). Status via `classify_status` (ok/gated/empty/cooldown/dead/stale/error/unknown — gates outrank streak death), per-row history via `record_state` (bounded JSON-safe list + `fail_streak`/`recent`/`tps`), client traffic via `note_req` -> `REQLOG` (probes never inflate it), context windows via `resolve_cw` (override > learned > curated > none), parked rows excluded from routing via `enabled_rows()`, self-reference refused by `_is_self_base` at the `upstream()` choke point, pool warmed by `_probe_loop` (boot + `MINIMALROUTER_PROBE_INTERVAL`) |
| Lifecycle | `write_pidfile()`/`read_pidfile()`/`remove_pidfile()`, `pid_alive()` (no signaling; `/proc/<pid>/stat` state, zombie = dead; `tasklist` fallback off Linux), `pid_is_ours()` (realpath identity check), `wait_dead()` (bounded poll — SIGTERM is asynchronous), `stop_server()` (verified-PID targeting, never `pkill -f`), `usage()` |

## Request lifecycle

```
client POST /v1/chat/completions {model, messages, ...}
  -> BY_MODEL(all_rows())[model]            # file rows first, then OAuth profile rows, file order
  -> for each row in chain:
       row_class(row)                        # derived, spoof-proof
       credential resolution                 # key/key_env | oauth_access_token() (refresh if expired) | none
       api_mode dispatch                     # plain upstream() | zcode | omnirush | responses
       success -> passthrough (SSE byte-for-byte when stream:true) + ROW_STATE + log OK
       failure -> ROW_STATE + log FAIL (with upstream body snippet) -> next row
  -> chain exhausted -> 502 {error, last}    # last carries the upstream's own message
```

Rows are re-read on every request; there is no restart-to-apply. OAuth profile rows
are never persisted into `rows.json`.

## Translators

A provider whose gateway needs a transformed body is a sibling transport in the same
file, reusing the row chain and state — `upstream()` is not rewritten.

- **omnirush** (`api_mode: omnirush`): Responses-only endpoint; forces
  `stream:true` + `store:false` even for non-streaming clients and assembles locally;
  typed `input` array; `User-Agent: omnirush` + `X-OmniRush-Client: cli/<version>`
  resolved lazily from the npm registry (memoized, env override, pinned fallback);
  rotating refresh at the vendor control plane persisted atomically under a
  single-flight lock. **Verdict on record: the gateway refuses every non-official
  client (`official_client_required`) even with a valid fresh token — the transport
  exists and is tested, the route is not routable.**
- **zcode** (`api_mode: zcode`): Anthropic-Messages-only plan endpoint; system is
  exactly three blocks (cliPrefix, canonical stable prefix with `cache_control`,
  client system); `metadata.user_id` is a JSON string; model ids lowercased; full
  vendor header bundle; identity ids persisted per account in the OAuth record
  (a fresh device_id per request is itself a rejection trigger). Business errors
  arrive inside HTTP 200 and are mapped (`1005/1113/3008/3009 -> 429`, `3012 -> 403`)
  before the chain sees them. The stable prefix is a vendor-inspected constant
  shipped as data (`bridges/zcode-identity.json`, fetched by the installer).
- **freebuff**: not an `api_mode` — a personal loopback sidecar
  (`bridges/freebuff_bridge.py`, live-only, **not shipped in this repo** because the
  vendor's ToS bans wrappers/relays/automation). The router consumes it as a plain
  API-class row. See docs/providers.md for the risk statement.

## OAuth device-flow layer

Generic in code, specific in data. A profile carries `authorize_url`, `token_url`,
optional `refresh_url`, `base_url`, `models`, and — only when the flow breaks
convention — curated fields (`authorize_body`, `token_body` with `{device_code}`
substitution, `poll_method`, form-encoded variants, `headers`, `client_secret_mode`
for self-minted client bearers that must match between init and poll). Discovery
fills gaps and is accepted only when the server actually answers with a device code;
a 400/404 on a guessed path is not proof a flow is dead — read the tool's own source.

Token liveness goes through one helper (`token_live`): plan JWTs with no `exp` claim
never expire; a fabricated expiry is never stored. Refresh honesty is a feature:
providers with no refresh token report "re-login with `minimalrouter oauth <id>`"
instead of pretending, and a successful refresh never implies the route works
(client gates sit after token validation).

## Lifecycle and portability

The server writes `minimalrouter.pid` (mode 600) at bind and removes it on normal
exit. `stop` targets only verified PIDs: on Linux it scans `/proc` and compares each
candidate's cmdline realpath against this script; elsewhere it falls back to the pid
file. Liveness never signals (`os.kill(pid, 0)` terminates a process on Windows);
a zombie (state `Z`) counts as dead so an unreaped child cannot strand the pid file.
SIGTERM is followed by a bounded death poll before the pid file is cleared. A
same-named copy at a different path is never touched.

## Security posture

Binds `127.0.0.1` only; no server auth by design (local access = pool access — see
SECURITY.md). Tokens and inline keys live in mode-600 files outside the repo. The
router never auto-imports tokens from another app's credential store; every account
connects through the provider's own visible device flow.

# Changelog

All notable changes to MinimalRouter are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version
lives in exactly one place — `__version__` in `minimalrouter.py` — and `-v`, the HTTP
`server_version`, and the git tag all derive from it. A CI test asserts they never drift.

Semver policy for this tool: **MAJOR** when the client-facing contract breaks (the
OpenAI-compatible request/response shape, the endpoint, or the install command); **MINOR**
for backward-compatible features (new providers, classes, dashboard capability, CLI verbs);
**PATCH** for fixes only.

## [1.2.0] — 2026-11-21

The Mission Control overhaul. The client contract (one OpenAI-compatible endpoint, the
one-line install) is unchanged and every addition is backward-compatible — MINOR bump.

### Added

- **Mission Control v1.2 board.** Centered max-width frame (btop-class): hero zone
  (wordmark, URL bar, stat tiles) dead-center, data table left-flush inside.
  Wordmark shimmer + mast typewriter; `m` pauses every animation timer.
- **8 pixel-digit stat tiles** (5x5 block face, labels at 1/4 wordmark height):
  ROWS, MODELS, SERVING, BLOCKED, REQ/MIN, TOK/S (floor — streamed usage is not
  parsed), ERR%, UPTIME. Values flash on change.
- **Traffic ledger in the engine** (`REQLOG` + `note_req`): client requests through
  `/v1` only — dashboard and boot probes never inflate REQ/MIN or ERR%.
  `/api/state` now carries `stats`, `probe` progress and `version`.
- **Route table rebuilt:** `ROW · PROV · CLASS · MODEL · CW · LAT · TPS · OK% · STATUS`.
  The TREND-only column is gone; PROV derives from the OAuth label or the base host,
  TPS is a per-row tokens/sec EMA (only when the upstream reports usage), OK% is the
  last-10-outcomes ratio. Columns drop responsively on narrow terminals.
- **Richer status vocabulary:** `COOLDOWN` (429), `DEAD` (3+ consecutive failures),
  `STALE` (unverified-ok for 30+ min), `OFF` (parked). Account gates (403/402)
  outrank streak death.
- **Disabled rows.** `e` parks marked/cursor rows (persisted `disabled: true` on file
  rows, profile-level for OAuth rows); parked rows stay visible on the board but can
  never serve `/v1` — `enabled_rows()` gates `/v1/models`, `/health` counts and the
  routing chain. `POST /api/disable {name, disabled?}`.
- **Context windows (CW).** Resolution chain: per-row `context_length` override >
  what the upstream's `/v1/models` publishes (learned at boot and by `f`/`POST
  /api/refetch`) > a curated table of officially documented windows > `-`. The tool
  never guesses a number.
- **Background probes.** Every enabled row is probed at boot (bounded concurrency)
  and re-probed every `MINIMALROUTER_PROBE_INTERVAL` seconds (default 600; 0 = boot
  only). The board shows `probing n/N` in the mast until the pool warms — no more
  all-UNKNOWN boot screen. This is the roadmap's "background refresh" item.
- **Multi-row marks + batch actions.** `space` marks, `T` tests marked (all enabled
  if none), `e` parks/unparks marked, `d d` deletes with a 4s confirm arm, `R`
  refreshes every OAuth token, `f` re-fetches every catalog.
- **In-board add.** `a` opens a modal (base/key/model/name); Enter advances fields;
  the engine's test-before-save still gates the write.
- **SELECTED gauges.** Live TPS + CW rendered in the pixel face at half-wordmark
  height, auto-updating every poll; the model name is highlighted hardest, the
  provider lighter but still highlighted.
- **Events, fully.** Every log class (OK/FAIL/TEST/ADD/REMOVE/DISABLE/ENABLE/PROBE/
  OAUTH-REFRESH/CATALOG/boot) with per-class colors, a 300-line buffer, `Tab` focus +
  up/down scroll + `g`/`G` jumps.
- **14 themes** (midnight, ocean, ember, nous, claude, dracula, gruvbox, nord,
  tokyo, monokai, solarized, onedark, crtgreen, crtamber) — `p`/`P` cycle; the
  claude board is the warm-cream light theme. `dashboard.json` persists
  theme/density/motion.
- **Help overlay** (`?`) and boxed btop-class zones with titled borders.

### Changed

- `/health` and `/api/state` response shapes (additive fields only: `disabled`,
  `serving`, `blocked`, `stats`, `probe`, `version`, per-row `provider`/`cw`/
  `disabled`/`ok_pct`).

### Fixed

- **Self-referential base guard.** A row whose base resolves to the router's own
  listener made the router call itself in unbounded recursion (found live: a port
  collision with the freebuff bridge sidecar produced 18 nested `all rows failed`
  envelopes and flooded the traffic ledger). `_is_self_base` now refuses at the
  `upstream()` choke point before any socket opens — honest error, no recursion,
  and `/api/add` cannot save such a row (its probe fails first).
- **Flat latency history** renders a steady mid-line sparkline (`▄▄▄`) instead of a
  bottom bleed, in both the engine helper and the board.
- **The mast refreshes on every poll.** With motion off it previously froze at the
  pre-boot render (no version, no live dot).

## [1.1.0] — 2026-11-20

Everything since the 1.0.0 baseline. The client contract (one OpenAI-compatible endpoint,
`curl … | sh` install) is unchanged, so this is a MINOR bump.

### Added

- **Three connection classes — API, OAuth device-flow, Keyless.** The class is always
  *derived* from a row's real shape (`row_class()`), never read from a label, so a spoofed
  `class` field can't skip auth or borrow a token.
- **OAuth device-flow layer.** One-word provider connects: `minimalrouter oauth add` →
  type a name (a self-growing name→endpoint registry prefills the base URL) → approve in the
  browser. Curated endpoint overrides, 400/401/428-pending polling, rotating-refresh tokens,
  and no-expiry plan-JWT handling.
- **Native translators** for gateways that cannot be a plain row: `zcode` (Anthropic-Messages
  plan lane with a shipped identity asset) and `omnirush` (Responses-only, forced
  `stream:true`+`store:false`, version-policed client header). `freebuff` bridge-ready.
- **Mission Control dashboard.** Block-pixel two-tone wordmark, stat strip
  (rows/models/serving/blocked), dense table with per-row latency sparklines and status
  pills, selected-row detail, and a live event tail.
- **Textual true-color board** — 3 themes (`p`), density toggle (`v`), offline-tolerant
  polling, theme/density persisted to `dashboard.json`. Bundled by the installer in a
  dedicated venv; the stdlib **curses** board is the automatic fallback when Textual is
  absent or crashes. `MINIMALROUTER_TUI=auto|textual|curses` forces the mode.
- **Ordered fallback** across rows sharing a model id; the failure envelope now carries the
  upstream's own error body, not just an HTTP status.
- **CLI lifecycle verbs**: `stop` (verified-PID targeting, portable across Linux/macOS/WSL)
  and `refresh [id]` (force a token refresh; honest "no refresh token" for session/JWT
  providers, non-zero exit when a requested profile did not refresh).
- **Provider registry** (`provider-registry.json`) as a growing data file — kilocode,
  omnirush, opencode, inference.sh, Nous Portal, zcode — never a hardcoded table in code.
- **hermes-agent repo design system**, scaled to a single-file tool: README skeleton,
  SECURITY.md, CONTRIBUTING, `docs/` split (architecture / providers / development), CI
  (Python 3.11–3.14 + clean-HOME install-smoke + a dedicated dashboard job), YAML issue
  forms, PR template, CODEOWNERS, actions-only dependabot.
- **Versioning**: single `__version__` source, this CHANGELOG, semver git tags, GitHub releases.

### Changed

- The installer now **bundles the dashboard**: it creates `~/.minimalrouter/.venv`, installs
  Textual, and pins the launcher to that venv — so one install gives the beautiful board
  regardless of whichever `python3` is on the user's PATH. `--curses` opts out to a strict
  zero-dep install. (Previously the board was opt-in, which meant strangers got the plain
  curses board by default.)
- Bounded per-row latency history via a single `record_state` write path (JSON-safe list,
  not a deque) so `/api/state` can serialize it for the sparklines.
- Hardened the dashboard loader: it validates the entry point is callable and falls back
  quietly on an incomplete module, instead of logging a scary line into the dashboard.

### Fixed

- Audit defects **D1–D4, D7**: `state_for` carries `oauth_provider` (the TUI `r` key now
  refreshes OAuth rows, PTY-proven); `oauth add` URL-shape validation (clean cancel on
  garbage); zombie-aware `pid_alive` (`stop` clears the pidfile on an unreaped child);
  upstream error body in the failure envelope; dead constants removed.
- Portable `stop`: pid file + `/proc` verification, no `os.kill(pid, 0)` (which terminates
  on Windows), stale-file cleanup.
- The zcode identity asset ships in the installer, so a fresh install no longer earns
  405 `3012` from an empty identity.

### Removed

- **dl (Directionally Local)** preset + pool profile. Its free device tokens became
  client-gated (403 `dl_only`) — the token is valid but the client is refused, so routing
  through the router is impossible. The free models still work inside the dl CLI itself; a
  paid dl API key is the only router-side path back.

## [1.0.0] — 2026-11-13

Initial release. One OpenAI-compatible endpoint (`http://127.0.0.1:8790/v1`) over many
API-key rows, standard library only, single file, localhost only, no server auth, with a
curses terminal dashboard.

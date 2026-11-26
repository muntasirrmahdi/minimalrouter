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

## [1.7.0] — 2026-11-26

### Added

- **`doctor` findings are machine-readable.** Every check now carries a stable
  `id`, a `category` (`A` safe deterministic, `B` network repair, `C` human hands,
  `D` your data, `E` nothing to fix), and a `fixable` flag. The display name is
  still there for humans; the id is the contract an external tool can depend on.
  `--json` also returns a `coverage` block.
- **Three coverage numbers, never conflated.** `doctor` prints:
  - **catalogue coverage** — auto-fixable checks / all checks (structural);
  - **observed coverage** — auto-fixable findings / all findings on this run;
  - **frequency-weighted coverage** — the same ratio across the local history
    ledger, with the sample size and date range printed. No history means `null`,
    never an invented percentage.
  `minimalrouter doctor --coverage` adds the per-category table.
- **Expanded safe repairs.** `doctor --fix` now also repairs a launcher missing
  its exec bit, a launcher pinned to bare `python3` when the dashboard venv
  exists, a missing dashboard module, a missing zcode bridge asset, and a
  `provider-registry.json` whose device-flow fields were stripped by the old
  `oauth add` clobber. Each repair is backed up before it writes, uses atomic
  writes, and is reported as `fix applied` or a WARN with a remedy. The boundary
  holds: `rows.json`, `oauth.json` and `dashboard.json` are never rewritten.
- **`minimalrouter fix`** is an alias for `doctor --fix`.
- **Local history ledger.** `~/.minimalrouter/doctor-history.jsonl` stores only
  `{ts, version, findings:[{id, level, fixable}]}` — no detail, no paths, no
  secrets — bounded to 100 runs. It is the only instrument that can produce a
  frequency-weighted number without pretending one install is a population.

### Fixed

- **`oauth add` no longer strips a preset's device-flow fields.** The registry
  entry is merged, not replaced: `authorize_url`, `token_url`, `poll_method`,
  `headers`, `token_body` and notes survive a re-add. This was the standing
  prerequisite for the 15-preset promise: 15 presets whose fields vanish on first
  re-add is worse than 6 that survive.

## [1.6.0] — 2026-11-26

### Added

- **Add a row from a base URL and a key alone.** `/api/add` no longer requires a
  model id: with none given, the base's own `/v1/models` is the candidate set and
  the set is saved behind ONE real completion — the test-before-save gate still
  decides, the verified id leads the chain, and the rest of the fetched ids ride in
  unprobed until the background probe reports each one. An explicit model id or a
  row name keeps the single-row behaviour. Auto-adds stop at `AUTO_ADD_CAP` (25) so
  one paste cannot flood the boot probe; the reply reports `advertised` vs `saved`.
- **A Responses-only gateway is detected, not configured.** Some upstreams disable
  `/v1/chat/completions` and serve only `/v1/responses`. `probe_auto()` reads the
  chat endpoint's own answer (400/404/405) as a PROTOCOL fact — never a provider
  name — retries the same model through the Responses transport, and when that is
  the attempt that passes, the saved row carries `api_mode: responses` so the
  client path uses the same transport from then on. When both fail, the error
  reported is the Responses-side verdict (`403 client_not_allowed`, `401`, quota),
  because that is the actionable one; the chat refusal is only the reason we moved.
- **A row can carry its own `user_agent`.** Client-gated gateways accept only
  recognised coding-client identities: the same request answers `403
  client_not_allowed` with a browser UA and 200 with a coding-client UA. Rows and
  `/api/add` accept `user_agent`, and the dashboard's add modal gained an optional
  UA field. The default stays the browser UA, unchanged for existing rows.
- **`minimalrouter update`.** Self-update from the public repo (`main`, the same
  ref `install.sh` uses). Prints installed vs published, parses the download's
  `__version__` and AST-checks it BEFORE replacing anything, backs the old files up
  beside the new ones, writes atomically (tmp + os.replace), and touches CODE ONLY
  — `rows.json`, OAuth profiles, tokens, `provider-registry.json` and board
  preferences are never rewritten. `--check` reports without writing; `--force`
  re-installs the published file over a same-or-newer local copy.

### Fixed

- **A `key_env` row's resolved secret is never persisted.** `read_rows()` injects
  the key from `~/.hermes/.env` so the rest of the engine sees a key like any other
  row — and `write_rows()` then wrote that resolved value back, copying every
  env-held secret into `rows.json` on the first disable toggle or reorder.
  `write_rows()` now drops `key` from rows that carry `key_env`; the env reference
  survives, the plaintext never lands.

## [1.5.0] — 2026-11-24

### Added

- **The pool now remembers what the last request taught it.** A four-account
  upstream measured on 2026-11-24 exposed the defect: `enabled_rows()` filtered
  only on `disabled` and the chain was walked in plain row order, so failover
  worked *within* a request — a row that threw was abandoned and the next tried —
  but nothing crossed over. The account that had just returned 429 stayed at the
  head of the chain and absorbed the next concurrent request too, so every
  request re-derived the same dead end. `order_chain()` is a stable partition at
  request time: rows the upstream has *declared* unusable (`cooldown` 429,
  `empty` 402, `gated` 403, `dead` streak) move behind the rest. It is a
  partition, never a filter — when every row is gated the order is unchanged and
  the caller still tries all of them, because a daily-allowance pool that is
  spent must wait for its reset, not answer 502. `stale` and `error` deliberately
  do not defer: those are uncertain, and an untried row is worth more than a
  deferred one.
- **A row can set its own `timeout`.** `TIMEOUT` was a single global, and for an
  upstream that is slow to *fail* — one that hangs instead of returning 429 when
  its session limit is hit — a global timeout is a per-row multiplier: four gated
  rows at 90 s is 360 s of silence before the caller learns anything. The row
  field wins over the global; rows without it keep the global default. The
  Responses-dialect path honours it too, so the fix covers both call paths.
- **An upstream's allowance meter survives the proxy.** Some upstreams expose
  remaining allowance *only* as response headers and ship no usage API at all, so
  a pool of such accounts could not see how much of today's quota was left — the
  one number the pool exists to manage. `x-quota-*`, `x-qos` and `retry-after` are
  read off the upstream response (success *and* 429, since the reset clock is the
  useful half of a rate-limit answer) and carried in row state, visible at
  `/api/state`. Non-numeric values stay strings; a header that is not a meter is
  never copied, so a `Set-Cookie` cannot leak into state. The value describes the
  LAST response: a call carrying none clears it rather than showing a stale
  number from a previous call.

### Notes

- No dashboard column was added for the meter. `provider` and `key_src` already
  crowd the row, and the geometry gate exists because a panel that reflows
  silently hides routes; that is a layout decision for the owner, not a
  side-effect of a state change.
- **This release is a subset of what the roadmap names 1.5.0.** Still open there:
  per-account pacing, jitter, and single-flight locks on the generic refresher
  (only omnirush holds one). The public commitments do not claim those yet.
- Verified: 211 unit tests OK (66 dashboard skips, textual absent locally; CI
  runs them with it present), `ruff check --select F` clean on engine, dashboard
  and tests. The 14 new tests fail 14/14 against the pre-fix v1.4.0 engine
  (`MR_TEST_ENGINE=`), including the end-to-end one, which reproduces the defect
  as `['a','b'] != ['b']` — request 2 restruck the spent account.

## [1.4.0] — 2026-11-23

### Added

- **Copy keys for the values the board clips.** A member asked in the BAI group
  how the long model ids are copied, because they all render as dots. The cause
  is layout: the MODEL cell is `clip(model, w["MODEL"])` and the SELECTED panel
  clips at 36 characters, so `openrouter/anthropic/claude-sonnet-5-5-thinking`
  shows as `openrouter/anthropic/claude-…`. Enabling mouse drag-selection alone
  would not fix it, because Textual's `get_selected_text()` returns the
  RENDERED string — selecting a long id copies the ellipsis, which is a string
  that does not work when pasted. So `y` copies the cursor row's **untruncated**
  model id from the data, `Y` the row name, `u` the base URL, and the toast
  prints the whole value so it is obvious whether the dots or the id landed on
  the clipboard. Drag-selection is switched on as well, for the rows that do fit.

  Clipboard goes through Textual's own `App.copy_to_clipboard()`, which emits
  OSC 52, so it works over SSH and not only in a local terminal. An empty field
  reports itself and leaves the clipboard untouched rather than copying nothing
  and claiming success.

  Drag-selection itself needed no enabling, and the first draft of this release
  was wrong about that: `Widget.ALLOW_SELECT` is already `True` in Textual 8.2.8
  and `allow_select` is a read-only property, so passing it to the constructor
  raises `TypeError` and assigning it to the screen silently no-ops. It was
  caught by the suite, not by inspection. The reason it cannot solve the
  complaint is measured, not assumed: `get_selected_text()` returns the rendered
  board text with the ellipsis intact, which is exactly the broken string the
  keys exist to avoid.

### Verification

The gate proves the defect is real before proving the fix: it asserts the id is
longer than the column, that the rendered cell carries an ellipsis, and that the
whole id is absent from the screen — so the key is not solving a problem that
does not exist. Also covered: cursor-following (copies the row under the cursor,
not row 0), no collision with the existing `space`/`T`/`e`/`d` marks, and no
crash on an empty pool.

## [1.3.3] — 2026-11-23

### Fixed

- **The board got slower the longer it stayed open.** `on_idle` created the poll
  and animation timers, and Textual fires `Idle` after *every* batch of messages,
  not once. Each cycle stacked another live pair: reassigning `self._poll_timer`
  drops the handle but never cancels the running timer. Measured against the live
  engine on a 140x42 board: **8 timers at boot, 28 at +5 s, 48 at +10 s, 86 at
  +20 s, 162 at +40 s**, with the row rebuild rate climbing **227 -> 456 -> 751
  -> 1353 rows/sec** and no plateau. `on_mount` created no timers at all, so the
  board also did not begin polling until the first idle landed.

  One defect, three symptoms that looked unrelated: the progressive slowdown
  while touching nothing, the lag on the first keypress (dozens of timers are
  already stacked by then), and `?` feeling slow. Timers are created exactly once
  in `on_mount`, guarded so a second call cannot stack a second pair; `on_idle`
  keeps only the idempotent tier convergence and a once-flagged border-title
  pass, and no longer polls.

  A permanent gate replaces the guesswork: `TestTimersAreCreatedOnce` samples the
  live timer registry and the poll count at two points in an idle run and
  requires both to be FLAT, plus a static assertion that `set_interval` and
  `self.poll()` appear nowhere inside `on_idle` and exactly twice inside
  `on_mount`. A board must not degrade with uptime.

- **`doctor` was invisible from the board.** The self-diagnostic command shipped
  in 1.3.0 as a shell verb and nothing in the terminal UI said it existed, so the
  instrument built to diagnose an install could not be found by the person using
  the install. It is named in the help overlay, in `usage()`, and in the README
  as the first move when the board misbehaves.

- **`doctor` now satisfies the two rules its governing spec states.** The skill's
  self-diagnostic section and `references/self-diagnostic-command.md` were written
  while this command was being built, and a gap audit against that catalogue found
  one divergence left: an OK finding carried remedy text, which breaks the
  invariant that a remedy line means "act here". The from-checkout launcher
  guidance moved into the detail and the invariant is asserted in both
  directions — so it cannot be satisfied by stripping every remedy from the tool.
  All 28 catalogue rows were re-checked against the implementation and every one
  is present.

## [1.3.2] — 2026-11-22

### Fixed

- **`doctor` reported an unused expired credential as a broken request.** Its own
  docstring defines severity — FAIL means a request will break — but the expiry
  branch fired FAIL whenever any stored token was past its `expires_at`, whether
  or not a row depended on it. The first real run on a live install printed
  `UNHEALTHY — 1 thing(s) will break requests` for an `inference` token that had
  been dead for four days while **zero** of the install's eight rows rode an
  OAuth profile: nothing could break. A diagnostic that cries wolf about hygiene
  trains the user to ignore every finding it produces, which is the exact failure
  the command exists to prevent.

  Expired and near-expiry tokens are now graded by dependency: FAIL only when a
  row references the profile, WARN when it does not, and a purely informational
  line when an unused token is merely expiring soon. The remedy text still names
  the right verb in each case — `minimalrouter refresh <id>` when a headless
  refresh path is stored, interactive `minimalrouter oauth <id>` when it is not.
  The law is now stated in the docstring and pinned by a structural test that
  rejects any FAIL without a request-breaking subject.

## [1.3.1] — 2026-11-22

### Fixed

- **The test suite was writing to the live log, and `doctor` then reported that
  noise as a real failure.** `probe_row()`, `api_disable()` and the OAuth
  translators call `log()` on their own code path, so any test that exercises
  them appends to `~/.minimalrouter/minimalrouter.log` no matter what the test
  class patches. Per-class isolation cannot cover an indirect write: the module
  global is reached from inside the engine function, not from the test.
  `test_probe_of_self_row_fails_honestly` left
  `FAIL m via self-row … 127.0.0.1:9322` lines in the real log, and the first
  `minimalrouter doctor` run printed that as its last log line — a diagnostic
  reporting the estate's own test noise is worse than no diagnostic, because it
  trains the user to distrust every finding.

  The log is now redirected once, at module import, into a session temp
  directory, so the live path is unavailable to the suite rather than opt-in.
  Verified by measurement rather than by reading the code: a full 122-test run
  leaves the real log byte-identical (mtime `07:13:06.164316945`, 828,185 bytes
  before and after). No engine test reads the log, so nothing lost a fixture.

## [1.3.0] — 2026-11-22

### Added

- **`minimalrouter doctor`** — self-diagnosis for the install a stranger just made. stdlib only, no Textual import, and it never issues an inference request, so it is safe to run against a wallet-backed pool: the only network reads are loopback `/api/state` and `/v1/models` when a server is already up.

  The installer's failure modes are silent, which is the gap this closes. A venv that cannot be built or a `pip install textual` that cannot reach the network leaves a working router with a plain curses board and no message anywhere saying why. Doctor checks each promise the installer makes and prints a one-line fix per finding: launcher pinned to a missing interpreter, launcher pinned to bare `python3` (curses lane), `~/.local/bin` off `PATH`, missing `bridges/zcode-identity.json` (the vendor answers 405 code 3012), a provider registry whose device-flow fields were lost to the `oauth add` rewrite, rows that can never serve, duplicate row names, a row pointing at the router's own port, an expired OAuth token with or without a headless renewal path, a saved theme that no longer exists, a stale pidfile, a port held by a foreign process, and secret files readable by the group.

  One finding is new knowledge, not a restatement: `load_env()` reads `~/.hermes/.env` and never merges `os.environ`, so a key the user `export`ed into his shell is invisible to the engine while looking perfectly set in every other tool. Doctor reports that row as a failure and says where the variable has to live.

  `--fix` applies only safe reversible repairs (stale pidfile, `chmod 600` on the two secret files). It never touches `rows.json`, `oauth.json` or `dashboard.json`: those hold the user's data and his live preferences, and a diagnostic that overwrites them creates a second incident. `--json` emits the same findings machine-readably for a heartbeat cron. Exit code is 1 when anything FAILs, 0 otherwise.

  Contribution taken from PR #1 (`akash-xzh`, closed 2026-11-19 with the approved note at comment `6989476775`), which proposed a `doctor` command; the implementation here is ours, written against this project's own recorded failure modes.

## [1.2.5] — 2026-11-22

### Fixed

- **Pressing `?` could kill the whole board.** The help modal popped itself
  through `app.pop_screen()` unconditionally. A key event queued behind a pop
  reached the already-removed screen while the stack was back to one entry,
  `pop_screen()` raised `ScreenStackError`, Textual rendered the traceback and
  the app exited. The same unguarded pop also re-delivered the OPENING
  keystroke to the new modal, which closed it before it could be read — help
  looked like it never opened at all. The modal now swallows the opening
  keystroke for 0.3s, dismisses through its own `ModalScreen.dismiss()`, and
  refuses to pop when it is no longer on the stack.
- **The context-window readout rendered broken digits.** The CW gauge is a
  24-column box with one column of padding on each side, so 22 columns of
  content, while `256K` in the pixel face is 23 columns wide —
  `overflow-x: hidden` sliced the last glyph mid-stroke. A clipped pixel digit
  reads as a broken number, not a long one. The gauges are 32 columns now, and
  every pixel readout (gauges and the eight POOL tiles alike) measures its own
  box and falls back to a single bold text line when the face does not fit, so
  no digit is ever cut. `1049K` is written `1M` for the same reason.

### Changed

- **Four key handlers stopped blocking the UI thread.** `r` made its OAuth
  refresh request synchronously on the board's own thread (30s timeout), and
  `p` / `P` / `v` called the full poll — a synchronous `/api/state` request
  plus a rebuild of every panel. A theme change needs no fresh server data, so
  those keys repaint from cache now and the 2s poll brings the state; `r`
  runs in a worker thread like `t` already did.
- **Cursor movement repaints two rows instead of twenty-one.** Every arrow
  press rebuilt the whole table through `repaint_rows()`.
- **Animation costs halved.** The tick is 0.25s rather than 0.15s and the
  wordmark shimmer advances on alternate ticks; the mast pulse, which carries
  the live/probing signal, still moves every tick. While rows are testing only
  the testing rows are repainted, not the table.
- **The column plan is cached** per frame width, density and tier — `row_line`
  recomputed the identical plan once per row, 21 times per repaint.
- **Batch test runs four rows in parallel** instead of summing every row's
  latency, which on slow free tiers read as a hung board.
- **`t` honours marks, and `T` finally means all.** Marked rows are the
  selection, so `t` tests them and falls back to the cursor row when nothing
  is marked; `T` tests every enabled row and ignores marks. Previously `t`
  ignored marks entirely, and `T` was documented as "all enabled" while
  `_targets()` returned the cursor row whenever no row was marked — so `T`
  tested one row and reported the toast as the whole pool. Disabled rows are
  excluded from any batch.
- The legend label `m=move` was wrong — the key toggles motion, not the
  cursor. It reads `m=motion` now.

## [1.2.4] — 2026-11-22

### Fixed

- **Table header and data columns did not line up** (found by capturing the
  rendered 1.2.3 grid and measuring token offsets). The header emits a 4-cell
  prefix (`" ✓▶ "`) while a data row emitted 3 (`mark + cursor + space`), so
  ROW/PROV/CLASS/MODEL all started **one character left** of their labels
  (header `ROW@4`, data `@3`). The latency cell diverged too: the header
  allocated `w['LAT']+5` while a row emits `spark(5) + " " + lat.rjust(w) + " "`
  = `w['LAT']+7`, so TPS/OK% drifted up to 5 cells from their headers.
- **Single source of truth for the gutter**: `row_prefix()` and the
  `LAT_SPARK`/`ROW_PREFIX` constants now build both the header line and every
  data row, so the two cannot disagree again. `spark()` defaults to the shared
  `LAT_SPARK` width.

### Added

- **Horizontal alignment assertion in the geometry gate**
  (`test_columns_line_up_under_their_labels`): left-justified columns must have
  their token start exactly under the header token; right-justified numeric
  columns share the header's cell right edge. The gate previously measured
  HEIGHTS only, which is why it certified a crooked table — a composition check
  that measures one axis is not a composition check.

## [1.2.3] — 2026-11-22

### Fixed

- **The board was geometrically broken at every terminal size** (Moon: "nicher
  dike blank keno eto? Baki shb kichu kothya? ... routes blank and selected er
  niche ar kichui nei! Pure black blank?!"). `.gauge` is a Textual `Vertical`,
  whose default height is `1fr`; inside `#selrow { height: auto }` it never
  resolved, so SELECTED expanded to fill the screen (measured: 40 rows on a
  42-row terminal). Total content then exceeded the frame, free space went
  NEGATIVE, and the route table's `1fr` collapsed to its minimum — border only.
  `rows_visible = 0/21` at every size from 80x24 to 200x60: an empty ROUTES box,
  a black void under the selected model, and EVENTS / toast / key legend pushed
  below the bottom edge, clipped and unreachable. The 1.2.0-1.2.2 board shipped
  in that state because 52 per-widget CONTENT assertions passed while nothing
  measured the rendered geometry.
- **Every panel height is pinned and only ROUTES is flexible** (`1fr` +
  `min-height`), so the route table takes the real slack and can never be
  starved. No container may use `height: auto` while holding a Vertical child.
- **Layout tier from the live screen height**, re-evaluated continuously. Bands
  (measured): micro < 32 rows (1-line wordmark, 1-line numbers, SELECTED kept,
  EVENTS dropped), compact 32-39, airy >= 40 (4-row wordmark, 3-row pixel stat
  face, both secondary panels). Previously the board had one fixed shape and
  simply lost panels on a short terminal.
- **Tier boundaries are graduated so a taller terminal never shows fewer
  routes.** The first cut of this fix used a 34-row boundary and produced a real
  inversion — a 30-row screen showed 4 routes while a 28-row screen showed 12,
  because the wider band kept both secondary panels. Measured monotonic within
  every band now.
- **The key legend is one line** (`t=test T=all spc=mark ...`); it was wrapping
  to 3-4 rows and stealing the table's height. The full reference is the `?`
  modal.
- **POOL became a borderless hero strip** — its 2-row border was costing the
  route table 9 visible rows on a 42-row terminal. The strip is pinned to exactly
  number-rows + label-row in every tier (airy 3+1, compact/micro/dense 1+1); no
  filler rows. Density and tier shrink only the digit face — labels stay BELOW
  the numbers in every tier, per Moon's layout law.
- **The tier is self-healing, not event-dependent.** It is re-decided on
  `on_idle` AND on every 2-second poll, repainting only on change. A one-shot
  decision in `on_mount` reads a not-yet-laid-out screen height, and `on_idle`
  can land late: measured flaky at 2-of-3 runs, where a 42-row terminal rendered
  the compact hero. The board now converges regardless of which event wins.

### Added

- **`?` opens the key reference as a MODAL overlay**, not an in-flow panel. An
  in-flow 13-row help cannot coexist with a non-scrolling frame — on a 26-row
  terminal it pushed the key legend below the bottom edge, reintroducing the very
  defect this release fixes. The modal owns its key events, so any key closes it
  and no app-level "help is open" guard is needed. Such a guard is actively
  harmful: the first attempt used one that never cleared, freezing the entire
  keyboard after the modal closed.

### Notes

- **The frame does not scroll.** `overflow-y: auto` makes a container size to its
  content, which breaks `fr` resolution (`1fr` stops meaning "share of the
  viewport"): with it on, EVENTS ballooned to 7 rows on a 24-row screen and sat
  below the bottom edge. Panels that do not fit DROP OUT instead of being pushed
  off-screen.
- SELECTED keeps all four content lines in the airy tier (name / provider ·
  class · model / endpoint · key / status · latency · ok%). No information was
  removed to make the layout fit — the panels that were stealing the space got
  pinned or dropped instead.
- New permanent CI gate: `TestDashboardGeometry` (10 tests) measures rendered
  REGIONS, not widget content — the table always has rows, no visible panel is
  off-screen, SELECTED and the gauges stay pinned, tier bands match the screen
  height, monotonicity within each band, the help modal never displaces the
  layout, and keys still work after the modal closes. Sabotage-verified against
  the 1.2.2 module: 33 composition failures, zero AttributeError, so the gate
  bites for the right reason.

## [1.2.2] — 2026-11-22

### Fixed

- **The POOL tiles now reconcile (Moon's "Where are the rest?" audit).** 1.2.1's
  BLOCKED tile counted only account gates (403/402), so a 21-row pool showing 11
  serving + 1 blocked left nine errored / rate-limited rows invisible in the
  tiles. The tile is renamed DOWN and counts every row that cannot serve right
  now (gated, empty, error, dead, cooldown, stale, parked): SERVING + DOWN +
  UNPROBED == ROWS. The granular per-row states (GATED/EMPTY/COOLDOWN/DEAD/
  STALE/OFF/ERROR) remain in the table's STATUS column — the tile is the
  aggregate, the table is the detail. The curses fallback strip uses the same
  DOWN aggregate. `/api/state` stats gain additive fields (`down`, `unknown`).

## [1.2.1] — 2026-11-21

Fixes to the 1.2.0 Mission Control overhaul, all found by capturing the rendered
board and reading it with fresh eyes before handing it over. PATCH bump — fixes
only, no contract change.

### Fixed

- **Table column order.** The header promised `CW · TREND+LAT · TPS · OK% ·
  STATUS`; the data rendered `CW · TPS · OK% · spark+LAT · STATUS`, so every
  column right of MODEL was misaligned. Data now follows the header order, and
  the spark+LAT block width matches the header cell exactly (5+1+LAT+1).
- **TPS column dead at boot.** `probe()` parsed `usage.completion_tokens` and
  threw it away, so the per-row EMA stayed zero until real client traffic.
  `probe()` now returns tokens (5-tuple), `probe_row`/`/api/test`/`/api/add`
  feed them to `record_state` — the column is live from the boot probe.
  `/api/test` gains a `tokens` field (additive).
- **CW column empty at boot.** Context windows were only learned by the manual
  `f` key / `POST /api/refetch`. The catalog read is now a shared
  `learn_catalogs()` that the boot sequence runs right after the row probes —
  the column fills itself on start. (`/api/refetch` uses the same function.)
- **PROV column garbage.** Numeric loopback hosts rendered `127` (the freebuff
  bridge row), OAuth labels clipped mid-word (`NOUS PORTA`), parenthetical
  labels leaked (`ZCODE (Z`). `prov_short()` now collapses labels to
  alphanumerics (`Nous Portal` -> NOUSPORTAL, `ZCode (Z.ai)` -> ZCODE),
  shortens domains to the first significant label, and falls back to the row
  name's first token for loopback hosts (`freebuff-bridge` -> FREEBUFF).

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

# Development

How to work on MinimalRouter without breaking the things that are hard to see.

## The two copies

| Copy | Path | Role |
|---|---|---|
| Live install | `~/.minimalrouter/minimalrouter.py` | what actually runs; holds personal state |
| Public repo | your checkout of `muntasirrmahdi/minimalrouter` | what strangers install |

Edit the live copy, verify it live, then copy it into the repo and push. Never edit
only the repo copy — the live file is what runs, and it is not the same file. After
every push: `git fetch && git rev-parse HEAD origin/main` must match, and
`sha256sum` of live and repo engine must match.

## Offline tests (no network, stdlib)

```sh
python -m unittest discover -s tests -v
```

The suite covers the pure functions and the regression classes from the 2026-11-19
audit: class derivation and spoof resistance, `token_live` no-expiry semantics,
`ep()` path joining, OAuth error-shape normalisation, `state_for` field contract
(D1), zombie-aware liveness (D3), `oauth add` URL validation (D2, subprocess),
and the failure-envelope upstream body (D4, loopback fixture server). CI runs the
suite on Python 3.11-3.14 (the dashboard tests skip when textual is absent, so the
core job stays zero-dep), an install-smoke job that runs `install.sh` from the
checkout into a throwaway HOME and asserts the curses fallback without textual, and
a `dashboard` job that installs textual and runs the full suite plus a headless
Textual-board smoke.

## The Mission Control dashboard (bundled by the installer)

`minimalrouter_dashboard.py` is the Mission Control board. It is a sibling module the
core imports lazily via `_load_textual_dashboard()`; if it or `textual` is missing, or
it crashes at runtime, the core falls back to its stdlib curses board. The router core
and the request path never depend on it. **The installer bundles Textual into a
dedicated `~/.minimalrouter/.venv` and pins the launcher to it**, so one install gives
the beautiful board out of the box regardless of which `python3` is on the user's PATH
(verified: the launcher renders Textual even in a shell whose `python3` lacks it).
`--curses` skips the venv for a strict zero-dep install.

```sh
sh install.sh                 # one install -> Textual board (venv bundled)
sh install.sh --curses        # zero-dep: curses board only, no venv
python -m unittest discover -s tests -v          # dashboard tests run with textual
MINIMALROUTER_TUI=curses minimalrouter           # force the curses fallback
MINIMALROUTER_TUI=textual minimalrouter          # request the board (loud if absent)
```

Verify it in a real PTY with a true terminal emulator (pyte), not a hand-rolled
escape parser — the naive parser mangles UTF-8 block glyphs and fakes layout bugs.
Theme and density persist to `~/.minimalrouter/dashboard.json`; the live-vs-repo sha
check above covers `minimalrouter.py` AND `minimalrouter_dashboard.py`.

A regression test must bite: to prove one does, run it against the pre-fix engine
(`MR_TEST_ENGINE=/path/to/old/minimalrouter.py python -m unittest ...`) and confirm
it fails there.

## The live verification battery

Logs, config blocks and `/v1/models` are claims, not proof. Answer "does it work"
only from calls made in the current session. State the cost basis first: tiny
probes (`max_tokens` 100-300), keyless/OAuth-free lanes cost nothing, keyed rows
spend fractions of a cent — say so before touching them.

1. Start the daemon if down (`minimalrouter serve`), confirm the startup line.
2. `/health` — rows/models/classes counts (the live pool; `rows.json` alone
   understates it because OAuth profiles add rows at runtime).
3. `/v1/models` — diff against every configured id; probe any missing id.
4. One completion per row family; check the echoed `model` field, not just content
   (upstreams do normalize/alias — record divergences, don't hide them).
5. Streaming (`stream:true`, count `data:` chunks, reassemble), tool-calls
   (`finish_reason: tool_calls`), and a real OpenAI SDK client.
6. Fallback drill: inject a dead head row for a chained model, confirm
   `FAIL ... -> next` in the log and a served completion, restore the file
   byte-exact.
7. Spoof drill: a row with `class:"keyless"` plus a key must show `api` in
   `/api/state`.
8. add/remove round-trip through `/api/add` + `/api/remove`; hand `rows.json` back
   byte-exact.
9. `/api/refresh` honesty: real rotation where a refresh token exists (re-probe a
   completion after a single-use rotation to prove persistence), plain "no refresh
   token" where none exists, honest error for unknown profiles.
10. Error shapes: unknown model 404, malformed JSON 400, `/v1/messages` 404.
11. `stop`: live-kill frees the port and clears the pid file; a stale pid file with
    a dead PID is cleared; a same-named copy elsewhere survives. For the zombie
    edge, start the server as an unreaped child and stop it — the pid file must
    still be cleared.
12. TUI through a real PTY: `t` writes state, `r` on an OAuth row prints a real
    rotation (use application-mode arrows `\x1bOB` — curses keypad mode ignores
    `\x1b[B`), `r` on credential-less rows is honest, Esc cancels the add flow,
    `q` exits 0 and frees the port. Zero tracebacks.
13. Log hygiene: no `Traceback`, no `OAUTH-REFRESH ... failed` lines (a failing
    profile taxes every request, not just its own rows).

Hand the port back when done: stop every instance you started and assert the port
is free before telling anyone to launch the dashboard.

## The stranger test (clean HOME)

```sh
FAKE_HOME="$(mktemp -d)"
HOME="$FAKE_HOME" sh install.sh          # from a checkout: copies local files
# or the public one-liner with HOME="$FAKE_HOME"
```

Assert: installed engine sha == pushed `origin/main` engine sha; registry seeded;
rows seeded with the keyless row first; `bridges/zcode-identity.json` present with
the expected size (3487 bytes / 3014-char stable prefix — an installer that skips
the asset ships a silently broken zcode route); `-h` works; a keyless completion
serves with zero keys; `oauth add` resolves a known tool from the registry and the
device flow issues a real user code. Use a per-process `HOME=` env, never `export
HOME=` in a persistent shell (it leaks into later git operations).

## Multi-op file edits

When one pass applies several replacements to a file: group ops by path, apply them
in order against the running text, assert each anchor's count == 1 BEFORE writing,
write once, then verify per op (new marker present, old marker gone — for extension
ops where the old text is a substring of the new, compare counts instead). A bad op
then leaves the file untouched instead of half-applied.

## Release checklist

1. `python -m py_compile minimalrouter.py`; JSON files parse; `sh -n install.sh`.
2. Offline tests green; ruff pyflakes-class check clean
   (`ruff check --select F minimalrouter.py tests`).
3. Live battery green (above); live == repo sha.
4. Every command lands in `-h`, the module docstring and the README in the same
   pass; scan `-h` for duplicate lines.
5. Provider-specific blockers live in docs/providers.md where users pick a
   provider; a status line expires the moment reality changes — rewrite it in the
   same pass, dated.
6. Commit (Conventional Commits), push, verify `origin/main` moved and matches.
7. Clean-HOME stranger test against the pushed artifact.
8. CI green on the pushed commit (`gh run list` / `gh run view`) — never claim green
   without the live read.

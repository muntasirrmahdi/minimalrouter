# Contributing to MinimalRouter

Small, focused changes are welcome. This project adopts the contribution discipline
of [Hermes Agent](https://github.com/NousResearch/hermes-agent), scaled to a
single-file tool.

## Contribution Priorities

1. Bug fixes with a reproduction (and a regression test that fails without the fix).
2. Registry entries for new tools, with primary-source endpoints and a live-probe
   verdict — one tool per PR.
3. Documentation truth fixes (a status line expires the moment reality changes).
4. Features that preserve the ground rules below.

## Before You Start: Search First

Search [existing issues](https://github.com/muntasirrmahdi/minimalrouter/issues)
and [PRs](https://github.com/muntasirrmahdi/minimalrouter/pulls) before opening
one. Read `docs/providers.md` before reporting a provider failure — most "bugs"
are documented upstream gates or rationed free tiers.

## Ground Rules

- This project is one Python file using only the standard library. A pull request
  that adds a dependency to the core will be closed.
- The server stays bound to `127.0.0.1` and stays unauthenticated as a server.
  Provider logins are OAuth device flows, stored outside the repo.
- No web UI and no desktop wrapper. The dashboard is the terminal.
- OAuth support stays generic in CODE: no provider-specific logic, parsing or
  endpoints hardcoded in the generic engine. `provider-registry.json` is the one
  allowed place where named tools live, because it is data, not logic. Named
  sibling transports (`chat_from_zcode`, `chat_from_omnirush`) implement vendor
  wire contracts and still take their base URL from data.
- Keep the code readable: no clever abstractions for a single-file tool.

## Development Setup

### Prerequisites

- Python 3.8+ to run the tool; Python 3.11+ to run the test suite and CI lint.
- No dependencies to install. There is no virtualenv, no lockfile, no build step.

### Commands

```sh
git clone https://github.com/muntasirrmahdi/minimalrouter
cd minimalrouter
sh install.sh                              # installs from the checkout, offline
python -m unittest discover -s tests -v    # offline test suite
python minimalrouter.py serve              # headless server on 127.0.0.1:8790
curl -s 127.0.0.1:8790/health              # rows / models / classes
python minimalrouter.py stop               # hand the port back when done
```

The live install (`~/.minimalrouter/minimalrouter.py`) and the repo copy are
different files: edit live, verify live, copy into the repo, push, and confirm the
shas match. See `docs/development.md`.

## Project Structure

```
minimalrouter.py            the whole tool (engine + dashboard + lifecycle)
install.sh                  one-liner installer (also installs from a checkout)
provider-registry.json      public name -> base/models seed (data)
rows.example.json           install seed, keyless row first
oauth-providers.example.json  generic profile template
bridges/zcode-identity.json   vendor-inspected identity asset (data)
tests/                      offline stdlib unittest suite
docs/                       architecture, providers, development
.github/                    issue forms, PR template, CI, CODEOWNERS
```

## Architecture Overview

Two invariants shape every review: the core is a single stdlib file (capability
grows through data and small sibling transports), and class is derived from row
shape, never from a label. Full map: `docs/architecture.md`.

## Code Style

- **PEP 8** with practical exceptions; no strict line-length enforcement.
- **Comments**: only when explaining non-obvious intent, trade-offs, or API
  quirks. Don't narrate what the code does.
- **Error handling**: catch specific exceptions. A broad catch that swallows an
  error silently is a defect at an integration boundary.
- **Error messages**: every user-facing error names the actual cause and the
  remediation step — never the proximate symptom. The failure envelope carries a
  snippet of the upstream's own response body for exactly this reason.
- **Cross-platform**: never assume Unix. No `/proc`-only code path without a
  fallback; never `os.kill(pid, 0)` (it terminates a process on Windows); liveness
  reads state and treats a zombie as dead; `stop` targets verified PIDs, never
  `pkill -f`.
- **Single-file exemption**: the engine deliberately exceeds any per-file size
  cap; do not split it into modules. New auxiliary files (tests, bridges) stay
  small and focused.

### Fail loud at integration boundaries

When a configuration value, credential, or user-supplied input is unusable — an
empty required field, a base URL that is not a URL, a placeholder token — reject it
where it is read and name the problem: a clear error at the write path, never a
silent no-op or a raw traceback that turns the confusion into a debugging session
later. A silent default is acceptable only where it is a deliberate, documented
product choice.

## Adding a Provider

One tool at a time, under the standing rules in `docs/providers.md`: probe keyless
first; primary source only (the tool's own code/docs); live-probe the device flow
before adding; login-verified and routable are separate verdicts; ToS-risky
providers stay out of the public seed. A registry entry is data — keep endpoints
out of the engine.

## Verification Before a PR

1. `python -m py_compile minimalrouter.py`; JSON files parse; `sh -n install.sh`.
2. `python -m unittest discover -s tests` green; `ruff check --select F
   minimalrouter.py tests` clean.
3. Run the flow you changed live, including one failure case (bad key, dead host,
   rate-limited row) so fallback still works.
4. Exercise the dashboard keys you touched through a real terminal — boot and quit
   is not enough.
5. For installer/engine changes: the clean-HOME stranger test
   (`docs/development.md`).
6. Every new command lands in `-h`, the module docstring and the README in the
   same pass.

A regression test must bite: prove it fails against the pre-fix engine
(`MR_TEST_ENGINE` override) before submitting.

## Reporting Bugs

Use the bug report form and include: your OS and `python3 -V`, the row you were
using **with the key removed**, the exact error text from
`~/.minimalrouter/minimalrouter.log`, and what you expected instead.

## Security

Never paste a real API key or token into an issue — redact it. Undisclosed
vulnerabilities go to
[Security Advisories](https://github.com/muntasirrmahdi/minimalrouter/security/advisories/new)
first, per `SECURITY.md` scope.

## Pull Request Process

- Branch naming: `fix/...`, `feat/...`, `docs/...`, `ci/...`.
- [Conventional Commits](https://www.conventionalcommits.org/)
  (`fix(scope):`, `feat(scope):`, `docs:`...).
- Fill in the PR template; one concern per PR, no drive-by cleanup.
- CI (offline tests on Python 3.11-3.14 + install smoke) must be green; never
  claim green without the live check.
- Public-artifact rules: no personal names, no private account details, no real
  keys in any committed file. Provider verdicts carry evidence and dates; never
  advertise a login-only tool as a working route.

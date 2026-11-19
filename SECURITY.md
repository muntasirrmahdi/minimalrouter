# MinimalRouter Security Policy

## 1. Reporting a Vulnerability

Report privately via
[GitHub Security Advisories](https://github.com/muntasirrmahdi/minimalrouter/security/advisories/new).
Never open a public issue for an undisclosed vulnerability. Include reproduction
steps and impact; do not include a working exploit against third-party services,
and never paste real API keys or tokens (redact them).

## 2. Trust Model

MinimalRouter is a personal, single-machine tool. Its boundary is the loopback
interface, not an authentication layer.

### 2.1 What is deliberate

- **Loopback only.** The server binds `127.0.0.1`. It is not reachable from the
  network unless someone deliberately rebinds or forwards it.
- **No server authentication, by design.** Any local process that can reach
  `127.0.0.1:8790` can use the pool and read row metadata (`/api/state` exposes row
  names, bases, model ids and key *sources* — never key values). On a shared
  machine, other local users are inside this boundary. This is a documented product
  choice for a personal tool, not an oversight.
- **Provider auth is separate.** API keys and OAuth tokens authenticate to
  upstream providers, not to the router.

### 2.2 Credential handling

- Inline keys and OAuth tokens live in `~/.minimalrouter/rows.json` and
  `~/.minimalrouter/oauth.json`, written mode 600, gitignored, never committed.
- `key_env` rows resolve keys at request time from the process environment (then
  `~/.hermes/.env` if present); the key value never lands in `rows.json`.
- Accounts connect only through the provider's own visible OAuth device flow.
  The router deliberately does **not** auto-import tokens from another app's
  credential store.
- Class is derived from row shape (`row_class()`); a spoofed `class` label can
  never skip auth or borrow a token. This is a tested security invariant.

### 2.3 ToS-sensitive providers

Providers whose terms ban wrappers/relays/automation are wired live-only, marked
`routable:false` with the ban note, and kept out of the public repo seed so no other
user inherits the enforcement risk. Using them remains the operator's own risk.

## 3. Scope

### In scope

- Engine bugs that expose tokens or keys (logs, API responses, error envelopes).
- Binding beyond loopback without an explicit operator action.
- Auth bypass via row/profile labels (class spoofing).
- Installer bugs that write files outside `~/.minimalrouter` / `~/.local/bin`,
  fetch over plain HTTP, or execute unverified remote code beyond the documented
  one-liner.
- `stop` signalling a process it did not verify as its own.

### Out of scope

- Upstream provider policy: client gates, quotas, rate limits, balance walls,
  free-tier decay. These are vendor decisions, reported honestly by the router.
- The operator's own key hygiene (leaked keys, committed state files).
- ToS consequences of running ToS-risky bridges — disclosed, operator's choice.
- Attacks requiring prior local access on a single-user machine (inside the
  documented trust boundary).

## 4. Deployment Hardening

- Never expose port 8790 to a network: there is no server auth. No reverse proxy,
  no `0.0.0.0` bind, no port forward.
- On shared machines, assume other local users can use the pool; do not add rows
  whose keys you would not share with them.
- Keep `~/.minimalrouter/*.json` at mode 600; the installer and write paths enforce
  this — report any path that does not.
- Run `minimalrouter stop` (verified-PID targeting) rather than pattern kills.

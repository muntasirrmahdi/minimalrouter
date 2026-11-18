#!/usr/bin/env python3
"""MinimalRouter — one clean OpenAI-compatible endpoint over N API rows.

Single file, stdlib only. No server authentication, no web UI. Binds 127.0.0.1.
Terminal dashboard (curses) in the same process.

Three connection classes (one engine path; class is derived from row shape, never trusted
from a label):
  1. API row      — key or key_env in rows.json (`minimalrouter`, press a).
  2. OAuth row    — `minimalrouter oauth add` (writes a profile),
                    then `minimalrouter oauth <profile-id>` (device sign-in).
  3. Keyless row  — an API row with no key and no key_env; the upstream serves it
                    anonymously (e.g. kilocode-free). Docs label only.

Usage:
  minimalrouter              server + terminal dashboard
  minimalrouter serve        server only (for tmux / systemd)
  minimalrouter oauth add    add a provider: paste its base URL, endpoints are found interactively
  minimalrouter oauth list   profiles and their connection state
  minimalrouter oauth <id>   device-code sign-in for that profile
  minimalrouter oauth rm <id>  delete a profile and its stored token

Home:   ~/.minimalrouter/
          minimalrouter.py            this file
          rows.json                   API-key rows
          oauth-providers.json        OAuth provider profiles (user-authored)
          oauth.json                  OAuth tokens, chmod 600
          minimalrouter.log           request log
Rows:   [{name, base, model, key_env|key, aliases?}]
Keys:   key_env rows resolve from ~/.hermes/.env at runtime.

OAuth profiles are generic: authorize_url, token_url, optional refresh_url,
base_url, models, optional headers, optional api_mode (chat_completions or
responses). No provider is hardcoded. Tokens are refreshed on demand.

HTTP API:
  POST /v1/chat/completions   stream + non-stream, ordered fallback per model
  GET  /v1/models             union of configured models/aliases
  GET  /health
  GET  /api/state             rows + live per-row state
  GET  /api/oauth/providers   configured OAuth profiles
  GET  /api/oauth/status      per-profile connected state
  POST /api/test              {base, key, model} -> probe result (no save)
  POST /api/fetch_models      {base, key} -> upstream model ids
  POST /api/add               {base, key, model, name?} -> test, then save
  POST /api/remove            {name} -> drop row
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

try:  # single guarded import: every UI helper resolves curses from here
    import curses
except ImportError:  # platform without curses -> main() degrades to serve
    curses = None

HOME = os.path.expanduser("~")
MR_HOME = os.path.join(HOME, ".minimalrouter")
ROWS_PATH = os.path.join(MR_HOME, "rows.json")
ENV_PATH = os.path.join(HOME, ".hermes", ".env")
LOG_PATH = os.path.join(MR_HOME, "minimalrouter.log")
HOST = "127.0.0.1"
PORT = int(os.environ.get("MINIMALROUTER_PORT", "8790"))
TIMEOUT = 90
PROBE_TIMEOUT = 30
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
ROW_LOCK = Lock()
ROW_STATE = {}
# Three connection classes. Class is always DERIVED from row shape, never from a label.
VALID_CLASSES = ("api", "oauth", "keyless")

# Translator constants (data-driven: endpoints live in oauth-providers.json / registry).
OMNIRUSH_GATEWAY_DEFAULT = "https://omnirush.ai/omnirush/v1"
OMNIRUSH_CLIENT_FALLBACK = "2.8.2"
OMNIRUSH_REFRESH_LOCK = Lock()
_OMNIRUSH_VERSION = None
ZCODE_GATEWAY_DEFAULT = "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages"
ZCODE_IDENTITY_PATH = os.path.join(MR_HOME, "bridges", "zcode-identity.json")
# Per-flow client secrets for device flows that authenticate the client with a
# self-minted bearer that must match between init and poll (zcode).
FLOW_SECRETS = {}


def load_env():
    env = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


ENV = load_env()

OAUTH_PATH = os.path.join(MR_HOME, "oauth.json")
OAUTH_PROFILES_PATH = os.path.join(MR_HOME, "oauth-providers.json")
OAUTH_EXAMPLE_PATH = os.path.join(MR_HOME, "oauth-providers.example.json")
# Extensible data file: name -> {label, base_url, models, api_mode?}.
# Not a whitelist — anyone can append an entry here or in the repo. No secrets.
PROVIDER_REGISTRY_PATH = os.path.join(MR_HOME, "provider-registry.json")


def write_oauth_profiles(profiles):
    os.makedirs(MR_HOME, exist_ok=True)
    tmp = OAUTH_PROFILES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"profiles": profiles}, f, indent=2)
        f.write("\n")
    os.replace(tmp, OAUTH_PROFILES_PATH)
    try:
        os.chmod(OAUTH_PROFILES_PATH, 0o600)
    except OSError:
        pass


def read_oauth_profiles():
    """Load generic provider profiles; no provider is bundled by default."""
    try:
        with open(OAUTH_PROFILES_PATH) as f:
            data = json.load(f)
        profiles = data.get("profiles", data) if isinstance(data, dict) else {}
        return profiles if isinstance(profiles, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read_provider_registry():
    """Load the extensible name -> provider map (provider-registry.json).

    This is data, not a whitelist: any user can append an entry here (or in the
    repo) for a new tool. Entries are {label, base_url, models, api_mode?}.
    """
    try:
        with open(PROVIDER_REGISTRY_PATH) as f:
            data = json.load(f)
        providers = data.get("providers", data) if isinstance(data, dict) else {}
        if not isinstance(providers, dict):
            return {}
        # Optional `class` is a display hint only. Validate to the known set;
        # an unknown value is dropped, never allowed to influence a request.
        for entry in providers.values():
            if isinstance(entry, dict) and "class" in entry \
                    and entry.get("class") not in VALID_CLASSES:
                entry.pop("class", None)
        return providers
    except (OSError, json.JSONDecodeError):
        return {}


# Endpoint discovery: no provider list, only conventions. Anything new that
# follows a convention is found automatically; anything exotic falls back to
# manual entry. An authorize candidate counts as found only when the server
# actually returns a device code for it.
DISCOVERY_AUTHORIZE_PATHS = (
    "/device/code", "/v1/device/code", "/oauth/device/code",
    "/oauth/device_authorization", "/v1/oauth/device/code",
    "/device/authorize", "/v1/device/authorize", "/device_authorization",
    "/api/device-auth/codes", "/auth/device/code",
)

DISCOVERY_TOKEN_PATHS = (
    "/device/token", "/v1/device/token", "/oauth/device/token",
    "/oauth/token", "/v1/oauth/token", "/v1/token", "/token",
)

DISCOVERY_REFRESH_PATHS = (
    "/token/refresh", "/v1/token/refresh", "/oauth/token/refresh",
    "/v1/oauth/refresh", "/oauth/refresh", "/refresh",
)


def _origin(url):
    u = urllib.parse.urlsplit(url)
    return "%s://%s" % (u.scheme, u.netloc)


def _post_json(url, body, timeout=20):
    """POST JSON, return (status, parsed_or_empty). Never raises."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


def error_code(payload):
    """Normalise an OAuth error to a lowercase code.

    Handles {'error': 'x'}, {'error': {'code': 'x'}}, {'error': {'type': 'x'}}
    and {'status': 'x'} — servers disagree on all four shapes.
    """
    if not isinstance(payload, dict):
        return ""
    e = payload.get("error")
    if isinstance(e, dict):
        return str(e.get("code") or e.get("type") or e.get("error") or "").lower()
    if isinstance(e, str):
        return e.split(":")[0].strip().lower()
    return str(payload.get("status") or "").lower()


def error_message(payload, fallback="OAuth failed"):
    if not isinstance(payload, dict):
        return fallback
    e = payload.get("error")
    if isinstance(e, dict):
        return str(e.get("message") or e.get("description") or e.get("code") or fallback)
    if isinstance(e, str):
        return e
    return str(payload.get("error_description") or fallback)


PENDING_CODES = ("authorization_pending", "authorization_pending_request",
                 "slow_down", "pending")


def discover_device_endpoints(base_url, log_fn=print):
    """Find device-flow endpoints from a base URL alone.

    Order: OIDC discovery document, then conventional paths. An authorize
    endpoint is accepted only if the server returns a device code for it; a
    token endpoint only if it answers that code with authorization_pending
    (or an access token). Returns a dict, empty on total failure.
    """
    origin = _origin(base_url)
    out = {"authorize_url": "", "token_url": "", "refresh_url": "",
           "base_url": base_url, "source": ""}

    # 1. OIDC discovery document (the standard way anything self-describes).
    status, oidc = _get_json(origin + "/.well-known/openid-configuration")
    if status == 200:
        auth = oidc.get("device_authorization_endpoint") or ""
        tok = oidc.get("token_endpoint") or ""
        if auth and tok:
            out.update({"authorize_url": auth, "token_url": tok,
                        "refresh_url": tok, "source": "oidc"})
            log_fn("  found via OIDC discovery document")
            return out

    # 2. Conventional authorize paths, proven by an actual device code.
    device_code = ""
    for path in DISCOVERY_AUTHORIZE_PATHS:
        status, d = _post_json(origin + path, {}, timeout=8)
        code = d.get("device_code") or d.get("code") or ""
        if code:
            out["authorize_url"] = origin + path
            out["source"] = "probe"
            device_code = code
            break

    if not out["authorize_url"]:
        return {}

    # 3. Conventional token paths, proven by how they answer that code.
    for path in DISCOVERY_TOKEN_PATHS:
        status, d = _post_json(origin + path, {"device_code": device_code}, timeout=8)
        if d.get("access_token") or error_code(d) in PENDING_CODES:
            out["token_url"] = origin + path
            break
        if status == 200 and not d:
            continue

    # 4. Refresh endpoint is best-effort; wrong guesses are harmless because a
    #    refresh is only attempted after an access token has expired.
    for path in DISCOVERY_REFRESH_PATHS:
        status, d = _post_json(origin + path, {"refresh_token": "probe"}, timeout=8)
        if error_code(d) in ("invalid_grant", "invalid_request", "invalid_token") \
                or status in (400, 401):
            out["refresh_url"] = origin + path
            break
    return out


def log(msg):
    try:
        with open(LOG_PATH, "a") as f:
            f.write("[%s] %s\n" % (time.strftime("%F %T"), msg))
    except OSError:
        pass


def read_oauth():
    try:
        with open(OAUTH_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_oauth(data):
    os.makedirs(MR_HOME, exist_ok=True)
    tmp = OAUTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    os.replace(tmp, OAUTH_PATH)
    try:
        os.chmod(OAUTH_PATH, 0o600)
    except OSError:
        pass


def read_rows():
    with open(ROWS_PATH) as f:
        rows = json.load(f)
    for r in rows:
        r["key"] = r.get("key") or ENV.get(r.get("key_env", ""), "")
    return rows


def oauth_access_token(provider):
    """Return an access token and rotate it when a profile supplies refresh_url."""
    auth = read_oauth()
    rec = auth.get(provider) or {}
    token = rec.get("access_token", "")
    expires = float(rec.get("expires_at", 0) or 0)
    if not token or expires > time.time() + 60:
        return token
    spec = read_oauth_profiles().get(provider, {})
    refresh_url = spec.get("refresh_url")
    refresh_token = rec.get("refresh_token", "")
    if not refresh_url or not refresh_token:
        return token
    body = {k: str(v).replace("{refresh_token}", refresh_token)
            for k, v in (spec.get("refresh_body") or
                          {"refresh_token": "{refresh_token}"}).items()}
    try:
        req = urllib.request.Request(refresh_url, data=json.dumps(body).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", UA)
        for k, v in (spec.get("refresh_headers") or {}).items():
            req.add_header(k, str(v).replace("{refresh_token}", refresh_token))
        d = json.loads(urllib.request.urlopen(req, timeout=30).read() or b"{}")
        new_token = d.get("access_token") or d.get("token")
        if not new_token:
            return token
        rec = dict(rec)
        rec["access_token"] = new_token
        if d.get("refresh_token"):
            rec["refresh_token"] = d["refresh_token"]
        rec["expires_at"] = time.time() + float(d.get("expires_in", 3600))
        auth[provider] = rec
        write_oauth(auth)
        return new_token
    except Exception as e:
        log("OAUTH-REFRESH %s failed: %s" % (provider, e))
        return token


def oauth_rows():
    rows = []
    auth = read_oauth()
    for provider, spec in read_oauth_profiles().items():
        token = oauth_access_token(provider)
        for model in spec.get("models", []):
            if isinstance(model, str):
                model, aliases = model, []
            else:
                model, aliases = model.get("model", ""), model.get("aliases", [])
            if not model:
                continue
            rows.append({"name": "%s-%s" % (provider, model.replace("/", "-")),
                         "base": spec["base_url"], "model": model,
                         "aliases": aliases, "key": token,
                         "oauth_provider": provider,
                         "api_mode": spec.get("api_mode", "chat_completions"),
                         "gateway_url": (auth.get(provider) or {}).get("gateway_url", ""),
                         "chat_path": spec.get("chat_path", ""),
                         "org_id": (auth.get(provider) or {}).get("org_id", ""),
                         "oauth_label": spec.get("label", provider)})
    return rows


def all_rows():
    return read_rows() + oauth_rows()


def write_rows(rows):
    os.makedirs(MR_HOME, exist_ok=True)
    tmp = ROWS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=1)
        f.write("\n")
    os.replace(tmp, ROWS_PATH)
    try:
        os.chmod(ROWS_PATH, 0o600)
    except OSError:
        pass


def ep(base, path):
    """Join base+path without doubling a version suffix (/v1)."""
    b = base.rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b + path


def upstream(base, key, path, payload=None, timeout=TIMEOUT, row=None):
    # Per-profile behavior only; no provider is known by name.
    row = row or {}
    provider = row.get("oauth_provider", "")
    api_mode = row.get("api_mode", "chat_completions")
    if api_mode == "responses" and path == "/v1/chat/completions":
        return oauth_chat(row, payload, timeout)
    if path == "/v1/chat/completions" and row.get("chat_path"):
        path = row["chat_path"]
    url = ep(base, path)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 method="POST" if data else "GET")
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        for hk, hv in oauth_headers(provider, key, row.get("org_id", "")).items():
            req.add_header(hk, hv)
    return urllib.request.urlopen(req, timeout=timeout)


def oauth_chat(row, payload, timeout=TIMEOUT):
    """Translate an OpenAI Chat request to a Responses-style upstream.

    Used only when a profile declares "api_mode": "responses".
    """
    base = row["base"].rstrip("/")
    token = row.get("key", "")
    messages = payload.get("messages", [])
    system = "\n".join(str(m.get("content", "")) for m in messages
                        if m.get("role") == "system")
    inp = [{"role": "user" if m.get("role") == "user" else "assistant",
            "content": str(m.get("content", ""))}
           for m in messages if m.get("role") != "system"]
    body = {"model": payload.get("model", ""), "input": inp,
            "stream": bool(payload.get("stream", False))}
    if system:
        body["instructions"] = system
    if payload.get("max_tokens") is not None:
        body["max_output_tokens"] = payload["max_tokens"]
    req = urllib.request.Request(ep(base, "/v1/responses"),
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for hk, hv in oauth_headers(row.get("oauth_provider", ""), token,
                                row.get("org_id", "")).items():
        req.add_header(hk, hv)
    return urllib.request.urlopen(req, timeout=timeout)


def responses_text(raw):
    """Extract assistant text from a Responses-style payload."""
    if not isinstance(raw, dict):
        return ""
    if isinstance(raw.get("output_text"), str):
        return raw["output_text"]
    parts = []
    for item in raw.get("output") or []:
        for c in (item or {}).get("content") or []:
            if isinstance(c, dict) and c.get("text"):
                parts.append(str(c["text"]))
    return "".join(parts)


def chat_from_responses(row, payload):
    """Run a non-streaming Responses call and return plain assistant text."""
    resp = oauth_chat(row, dict(payload, stream=False))
    raw = json.loads(resp.read() or b"{}")
    return responses_text(raw)



# ---------------------------------------------------------------------------
# Translators. These are api_mode values keyed by profile data; no provider name
# is hardcoded in the request path. Each is importable/testable without network.
# ---------------------------------------------------------------------------

def omnirush_client_version(force=False):
    """Resolve the policed CLI version lazily (npm latest), stdlib-only."""
    global _OMNIRUSH_VERSION
    pinned = os.environ.get("OMNIRUSH_CLIENT_VERSION") or ENV.get("OMNIRUSH_CLIENT_VERSION")
    if pinned:
        return pinned
    if _OMNIRUSH_VERSION and not force:
        return _OMNIRUSH_VERSION
    v = OMNIRUSH_CLIENT_FALLBACK
    try:
        req = urllib.request.Request("https://registry.npmjs.org/omnirush/latest",
                                     headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read() or b"{}")
        got = str(j.get("version", ""))
        if re.match(r"^\d+\.\d+\.\d+", got):
            v = got
    except Exception as e:
        log("OMNIRUSH version lookup failed: %s" % e)
    _OMNIRUSH_VERSION = v
    return v


def omnirush_control_plane(gateway_url):
    g = (gateway_url or OMNIRUSH_GATEWAY_DEFAULT).rstrip("/")
    return g[:-3] if g.endswith("/v1") else g


def omnirush_headers(token, version, stream=True):
    return {"Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "Authorization": "Bearer " + token, "User-Agent": "omnirush",
            "X-OmniRush-Client": "cli/" + version}   # no session-id header


def omnirush_access_token(provider):
    """Refresh with rotating token; persist atomically via shared oauth.json."""
    with OMNIRUSH_REFRESH_LOCK:
        auth = read_oauth()
        rec = auth.get(provider) or {}
        tok = rec.get("access_token", "")
        if tok and float(rec.get("expires_at", 0) or 0) > time.time() + 300:
            return tok
        rt = rec.get("refresh_token", "")
        if not rt:
            return tok
        ver = omnirush_client_version()
        profile = read_oauth_profiles().get(provider, {}) or {}
        gw = rec.get("gateway_url") or profile.get("base_url") or OMNIRUSH_GATEWAY_DEFAULT
        url = omnirush_control_plane(gw) + "/device/refresh"
        try:
            req = urllib.request.Request(url, data=json.dumps({"refresh_token": rt}).encode(),
                                         method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "omnirush/%s (linux; x64)" % ver)
            req.add_header("X-OmniRush-Client", "cli/" + ver)
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read() or b"{}")
            new = d.get("access_token") or d.get("token")
            if not new:
                return tok
            rec = dict(rec)
            rec["access_token"] = new
            if d.get("refresh_token"):
                rec["refresh_token"] = d["refresh_token"]
            if d.get("gateway_url"):
                rec["gateway_url"] = d["gateway_url"]
            rec["expires_at"] = time.time() + float(d.get("expires_in", 3600))
            auth[provider] = rec
            write_oauth(auth)
            return new
        except Exception as e:
            log("OMNIRUSH refresh %s failed: %s" % (provider, e))
            return tok


def chat_to_omnirush(row, payload):
    """OpenAI Chat body -> Responses body with forced stream/store (pure)."""
    messages = payload.get("messages", [])
    system = "\n\n".join(str(m.get("content", "")) for m in messages
                         if m.get("role") in ("system", "developer"))
    inp = []
    for m in messages:
        role = m.get("role")
        if role in ("system", "developer"):
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            parts = [{"type": "input_text" if role == "user" else "output_text",
                      "text": content}]
        else:
            parts = []
            for c in content if isinstance(content, list) else []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    parts.append({"type": "input_text" if role == "user" else "output_text",
                                  "text": str(c.get("text", ""))})
                elif c.get("type") == "image_url":
                    parts.append({"type": "input_image",
                                  "image_url": (c.get("image_url") or {}).get("url", "")})
        if role == "tool":
            inp.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""),
                        "output": str(content)})
        else:
            inp.append({"type": "message", "role": "user" if role == "user" else "assistant",
                        "content": parts})
    body = {"model": payload.get("model", ""), "input": inp,
            "stream": True, "store": False}
    if system:
        body["instructions"] = system
    mt = payload.get("max_completion_tokens", payload.get("max_tokens"))
    if mt is not None:
        body["max_output_tokens"] = mt
    for k in ("temperature", "top_p", "reasoning_effort", "parallel_tool_calls", "metadata"):
        if k in payload:
            body[k] = payload[k]
    tools = payload.get("tools")
    if tools:
        body["tools"] = [{"type": "function", "name": t.get("function", {}).get("name", ""),
                          "description": t.get("function", {}).get("description", ""),
                          "parameters": t.get("function", {}).get("parameters", {})}
                         for t in tools if isinstance(t, dict)]
    return body


def omnirush_sse_events(resp):
    """Yield parsed data: payloads from an SSE stream (pure over a line iterable)."""
    for raw in resp:
        try:
            line = raw.decode() if isinstance(raw, bytes) else str(raw)
        except Exception:
            continue
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def omnirush_usage(u):
    """Responses usage -> chat usage (pure)."""
    if not isinstance(u, dict):
        return {}
    out = {"prompt_tokens": u.get("input_tokens", 0),
           "completion_tokens": u.get("output_tokens", 0),
           "total_tokens": u.get("total_tokens", 0)}
    rd = (u.get("output_tokens_details") or {}).get("reasoning_tokens")
    if rd is not None:
        out["completion_tokens_details"] = {"reasoning_tokens": rd}
    return out


def chat_from_omnirush(row, payload):
    """Non-stream bridge: forced upstream SSE, assembled into a chat.completion dict."""
    base = (row.get("gateway_url") or row["base"]).rstrip("/")
    if not base.endswith("/responses"):
        url = base + "/responses"
    else:
        url = base
    token = omnirush_access_token(row.get("oauth_provider", "")) or row.get("key", "")
    ver = omnirush_client_version()
    body = chat_to_omnirush(row, payload)
    st = {"id": "chatcmpl-" + os.urandom(12).hex(), "text": "", "reasoning": "",
          "model": body.get("model", ""), "tool_calls": [], "usage": {}, "finish": "stop"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    for k, v in omnirush_headers(token, ver, stream=True).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        for ev in omnirush_sse_events(resp):
            t = ev.get("type")
            if t == "response.created":
                st["model"] = (ev.get("response") or {}).get("model", st["model"])
            elif t == "response.output_text.delta" and isinstance(ev.get("delta"), str):
                st["text"] += ev["delta"]
            elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta",
                       "response.reasoning.delta") and isinstance(ev.get("delta"), str):
                st["reasoning"] += ev["delta"]
            elif t == "response.completed":
                rp = ev.get("response") or {}
                if rp.get("usage"):
                    st["usage"] = omnirush_usage(rp["usage"])
                if rp.get("model"):
                    st["model"] = rp["model"]
            elif t in ("response.failed", "error"):
                raise RuntimeError("omnirush upstream: %s" % str(ev.get("error", t))[:200])
    return st


def zcode_identity():
    """Load the sanitized public identity asset (data, never code)."""
    try:
        with open(ZCODE_IDENTITY_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def zcode_headers(token, account_uuid, device_id, session_id, request_id, trace_id, query_id):
    ident = zcode_identity()
    return {"Content-Type": "application/json", "Accept": "text/event-stream",
            "Authorization": "Bearer " + token,
            "anthropic-version": ident.get("anthropicVersion", "2023-06-01"),
            "anthropic-beta": ident.get("anthropicBeta", "mid-conversation-system-2026-04-07"),
            "http-referer": "https://zcode.z.ai",
            "User-Agent": ident.get("sdkUserAgent", "ZCode/3.16.3"),
            "x-title": "Z Code@electron", "x-release-channel": "production",
            "x-client-language": "en", "x-client-timezone": "UTC",
            "x-zcode-agent": "glm", "x-platform": "linux-amd64",
            "x-os-category": "linux", "x-os-version": "unknown",
            "x-zcode-app-version": "3.16.3", "x-zcode-session-type": "main",
            "x-zcode-query-source": "main_turn", "x-session-id": session_id,
            "x-request-id": request_id, "x-zcode-trace-id": trace_id,
            "x-query-id": query_id, "x-device-mid": device_id}


def chat_to_zcode(row, payload):
    """OpenAI Chat body -> Anthropic Messages body with 3-block identity (pure)."""
    ident = zcode_identity()
    messages = payload.get("messages", [])
    client_system = "\n".join(str(m.get("content", "")) for m in messages
                              if m.get("role") in ("system", "developer"))
    blocks = [{"type": "text", "text": ident.get("cliPrefix",
               "You are ZCode, an interactive coding agent")}]
    stable = ident.get("stablePrefix", "")
    if stable:
        blocks.append({"type": "text", "text": stable,
                       "cache_control": {"type": "ephemeral"}})
    blocks.append({"type": "text", "text": "\n\n" + client_system,
                   "cache_control": {"type": "ephemeral"}})
    msgs = []
    for m in messages:
        role = m.get("role")
        if role in ("system", "developer"):
            continue
        content = m.get("content", "")
        if role == "tool":
            msgs.append({"role": "user", "content": [{"type": "tool_result",
                         "tool_use_id": m.get("tool_call_id", ""),
                         "content": str(content)}]})
            continue
        parts = []
        for c in (content if isinstance(content, list) else [{"type": "text", "text": str(content)}]):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                parts.append({"type": "text", "text": str(c.get("text", ""))})
            elif c.get("type") == "image_url":
                parts.append({"type": "image", "source": {"type": "url",
                              "url": (c.get("image_url") or {}).get("url", "")}})
        msgs.append({"role": "user" if role == "user" else "assistant", "content": parts})
    rec = read_oauth().get(row.get("oauth_provider", "")) or {}
    device_id = rec.get("zcode_device_id") or str(__import__("uuid").uuid4())
    account_uuid = rec.get("zcode_account_uuid") or rec.get("org_id") or ""
    session_id = rec.get("zcode_session_id") or ("session_" + os.urandom(16).hex())
    request_id = str(__import__("uuid").uuid4())
    trace_id = str(__import__("uuid").uuid4())
    body = {"model": str(payload.get("model", "")).lower(),
            "system": blocks, "messages": msgs,
            "max_tokens": int(payload.get("max_tokens", 8192) or 8192),
            "stream": bool(payload.get("stream", False)),
            "metadata": {"user_id": json.dumps({"device_id": device_id,
                         "account_uuid": account_uuid, "session_id": session_id},
                         separators=(",", ":"))}}
    return body, {"device_id": device_id, "account_uuid": account_uuid,
                  "session_id": session_id, "request_id": request_id,
                  "trace_id": trace_id,
                  "query_id": "01a1" + request_id[4:]}


ZCODE_ERR = {"1005": 429, "1113": 429, "3008": 429, "3009": 429, "3012": 403}


def zcode_status(body):
    """Business error inside HTTP 200 -> HTTP status (pure)."""
    if not isinstance(body, dict):
        return 200
    code = str(body.get("code", ""))
    return ZCODE_ERR.get(code, 200)


def chat_from_zcode(row, payload):
    """Non-stream bridge: Anthropic Messages upstream -> chat.completion dict."""
    base = row.get("base", "").rstrip("/")
    token = row.get("key", "")
    spec = row.get("oauth_provider", "")
    body, ids = chat_to_zcode(row, payload)
    req = urllib.request.Request(base, data=json.dumps(body).encode(), method="POST")
    for k, v in zcode_headers(token, ids["account_uuid"], ids["device_id"],
                              ids["session_id"], ids["request_id"], ids["trace_id"],
                              ids["query_id"]).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    try:
        d = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        d = {}
    st = zcode_status(d)
    if st != 200:
        raise RuntimeError("zcode business error %s: %s" % (d.get("code"), str(d.get("msg"))[:120]))
    if d.get("stream"):
        text = "".join(str(e.get("delta", {}).get("text", "")) for e in
                       [json.loads(l[6:]) for l in raw.decode().splitlines()
                        if l.startswith("data:") and l[6:].strip() not in ("[DONE]", "")]
                       if isinstance(e, dict) and e.get("type") == "content_block_delta")
    else:
        text = "".join(str(b.get("text", "")) for b in d.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "text")
    return {"id": "chatcmpl-" + os.urandom(12).hex(), "text": text,
            "model": body.get("model", ""), "usage": {}, "finish": "stop"}



def probe(base, key, model, row=None):
    """Marker probe. Returns (ok, ms, error, content)."""
    t0 = time.time()
    payload = {"model": model, "max_tokens": 200,
               "messages": [{"role": "user",
                             "content": "Reply with exactly: OK"}]}
    try:
        resp = upstream(base, key, "/v1/chat/completions", payload,
                        timeout=PROBE_TIMEOUT, row=row or {})
        body = json.loads(resp.read() or b"{}")
        ms = (time.time() - t0) * 1000
        if isinstance(body, dict) and "choices" not in body \
                and (body.get("output") or body.get("output_text")):
            body = {"choices": [{"message": {
                "role": "assistant", "content": responses_text(body)}}],
                "usage": body.get("usage", {})}
        if not isinstance(body, dict) or "choices" not in body:
            return False, ms, "unexpected body", ""
        try:
            msg = body["choices"][0].get("message", {}) or {}
            content = msg.get("content") or ""
        except (KeyError, IndexError, TypeError):
            content = ""
        if not content.strip():
            if (body.get("usage", {}) or {}).get("completion_tokens"):
                return True, ms, "", "(reasoning only, no visible text)"
            return False, ms, "empty content", ""
        return True, ms, "", content.strip()[:80]
    except Exception as e:
        return False, (time.time() - t0) * 1000, "%s: %s" % (
            type(e).__name__, e), ""


def fetch_models(base, key):
    resp = upstream(base, key, "/v1/models", timeout=PROBE_TIMEOUT)
    body = json.loads(resp.read() or b"{}")
    data = body.get("data", body if isinstance(body, list) else [])
    ids = []
    for m in data:
        i = m.get("id") if isinstance(m, dict) else m
        if i and i not in ids:
            ids.append(i)
    return ids


def BY_MODEL(rows):
    bm = {}
    for r in rows:
        for a in [r["model"]] + r.get("aliases", []):
            bm.setdefault(a, []).append(r)
    return bm


def row_class(row):
    """Derive the connection class from a row's actual shape.

    API    = file row whose key comes from a key or key_env (pasted/env key).
    OAuth  = row produced by an OAuth profile (has oauth_provider).
    Keyless = file row with no key and no key_env; the upstream serves it anonymously.

    Security: this is DERIVED, never read from a user-supplied `class` field. A row
    cannot claim to be keyless to skip auth, nor oauth to gain a token: shape decides.
    """
    if not isinstance(row, dict):
        return "api"
    if row.get("oauth_provider"):
        return "oauth"
    if row.get("key") or row.get("key_env"):
        return "api"
    return "keyless"


def class_label(cls):
    """Short, stable tag for the dashboard; unknown values collapse to '?'."""
    return {"api": "API", "oauth": "OAUTH", "keyless": "KEYLESS"}.get(cls, "?")


def state_for(rows):
    out = []
    for r in rows:
        st = dict(ROW_STATE.get(r["name"], {"status": "unknown"}))
        cls = row_class(r)
        out.append({"name": r["name"], "model": r["model"], "base": r["base"],
                    "class": cls,
                    "key_src": ("env:" + r["key_env"]) if r.get("key_env")
                    else ("pasted" if r.get("key") else "none"),
                    "aliases": r.get("aliases", []), "state": st})
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "minimalrouter/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (
            json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _chunk(self, data):
        self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")

    def _body(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln) or b"{}")

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/oauth/providers":
            profiles = read_oauth_profiles()
            return self._send(200, {"providers": [{"id": k, "label": v.get("label", k),
                "models": [m if isinstance(m, str) else m.get("model", "")
                           for m in v.get("models", [])]} for k, v in profiles.items()]})
        if p == "/api/oauth/status":
            auth = read_oauth()
            profiles = read_oauth_profiles()
            return self._send(200, {"providers": [{"id": k, "label": v.get("label", k),
                "connected": bool((auth.get(k) or {}).get("access_token")),
                "models": [m if isinstance(m, str) else m.get("model", "")
                           for m in v.get("models", [])]} for k, v in profiles.items()]})
        if p == "/api/oauth/start":
            return self._send(405, {"error": "OAuth is terminal-only. Run: minimalrouter oauth <provider-id>"})
        if p == "/":
            msg = ("MinimalRouter — terminal UI: run `minimalrouter`\n"
                   "API: POST http://%s:%d/v1/chat/completions\n"
                   % (HOST, PORT))
            return self._send(200, raw=msg.encode(), ctype="text/plain")
        if p == "/health":
            rows = all_rows()
            by_class = {}
            for r in rows:
                c = row_class(r)
                by_class[c] = by_class.get(c, 0) + 1
            return self._send(200, {"ok": True, "rows": len(rows),
                                    "models": len(BY_MODEL(rows)),
                                    "classes": by_class,
                                    "port": PORT})
        if p == "/api/state":
            rows = all_rows()
            return self._send(200, {"rows": state_for(rows),
                                    "models": len(BY_MODEL(rows)),
                                    "port": PORT})
        if p in ("/v1/models", "/models"):
            bm = BY_MODEL(all_rows())
            return self._send(200, {"object": "list", "data": [
                {"id": i, "object": "model", "owned_by": "minimalrouter"}
                for i in sorted(bm)]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            body = self._body()
        except json.JSONDecodeError as e:
            return self._send(400, {"error": "bad json: %s" % e})

        if p == "/api/test":
            base, key, model = (body.get("base", ""), body.get("key", ""),
                                body.get("model", ""))
            if not (base and model):
                return self._send(400, {"error": "base + model required"})
            with ROW_LOCK:
                try:
                    known = all_rows()
                except Exception:
                    known = []
            match = None
            for r in known:
                if r["base"].rstrip("/") == base.rstrip("/") \
                        and r["model"] == model:
                    match = r
                    break
            if not key and match:  # dashboard row test -> configured key
                key = match["key"]
            ok, ms, err, content = probe(base, key, model, row=match)
            if match:  # keep the table's STATE / LATENCY / LAST live
                ROW_STATE[match["name"]] = {
                    "status": "ok" if ok else "fail", "ms": ms,
                    "at": time.time(),
                    "error": "" if ok else str(err)[:200]}
            log("TEST %s %s -> %s" % (base, model, "ok" if ok else err))
            return self._send(200 if ok else 400,
                              {"ok": ok, "ms": ms, "error": err,
                               "content": content})

        if p == "/api/fetch_models":
            base, key = body.get("base", ""), body.get("key", "")
            if not base:
                return self._send(400, {"error": "base required"})
            try:
                ids = fetch_models(base, key)
                return self._send(200, {"ok": True, "models": ids,
                                        "total": len(ids)})
            except Exception as e:
                return self._send(400, {"ok": False, "error": "%s: %s"
                                        % (type(e).__name__, e)})

        if p == "/api/add":
            base = body.get("base", "").strip()
            key = body.get("key", "").strip()
            model = body.get("model", "").strip()
            name = body.get("name", "").strip()
            if not (base and model):
                return self._send(400, {"error": "base + model required"})
            if not name:
                host = base.split("//")[-1].split("/")[0][:24]
                slug = "".join(c if c.isalnum() else "-" for c in model
                               )[:40].strip("-")
                name, i = "%s-%s" % (host, slug), 2
            # Class is derived from the row shape (key present or not); a caller
            # cannot pass `class` to claim a different auth path.
            new_row = {"name": name, "base": base, "model": model, "key": key}
            ok, ms, err, content = probe(base, key, model)
            if not ok:
                return self._send(400, {"ok": False,
                                        "error": "test failed: %s" % err})
            with ROW_LOCK:
                rows = read_rows()  # file rows only: never persist OAuth rows
                while any(r["name"] == name for r in rows):
                    name, i = "%s-%d" % (name, i), i + 1
                new_row["name"] = name
                rows.append(new_row)
                write_rows(rows)
            ROW_STATE[name] = {"status": "ok", "ms": ms, "at": time.time()}
            log("ADD %s (%s / %s) %s ok %.0fms" % (
                name, base, model, row_class(new_row), ms))
            return self._send(200, {"ok": True, "name": name})

        if p == "/api/remove":
            name = body.get("name", "")
            with ROW_LOCK:
                oauth_names = {r["name"] for r in oauth_rows()}
                if name in oauth_names:
                    return self._send(400, {
                        "error": "that row comes from an OAuth profile; remove "
                                 "the profile in oauth-providers.json instead"})
                rows = read_rows()
                kept = [r for r in rows if r["name"] != name]
                if len(kept) == len(rows):
                    return self._send(404, {"error": "no such row"})
                write_rows(kept)
            ROW_STATE.pop(name, None)
            log("REMOVE %s" % name)
            return self._send(200, {"ok": True})

        if p in ("/v1/chat/completions", "/chat/completions"):
            bm = BY_MODEL(all_rows())
            model = body.get("model", "")
            stream = bool(body.get("stream", False))
            chain = bm.get(model)
            if not chain:
                return self._send(404, {
                    "error": "model '%s' not in pool" % model,
                    "available": sorted(bm)})
            last_err = None
            for row in chain:
                t0 = time.time()
                try:
                    if row.get("api_mode") == "omnirush":
                        chat = chat_from_omnirush(row, dict(body, stream=False))["text"]
                        ROW_STATE[row["name"]] = {
                            "status": "ok", "ms": (time.time() - t0) * 1000,
                            "at": time.time()}
                        if stream:
                            chunk = {"id": "chatcmpl-omnirush",
                                     "object": "chat.completion.chunk",
                                     "created": int(time.time()),
                                     "model": model,
                                     "choices": [{"index": 0,
                                                  "delta": {"role": "assistant",
                                                            "content": chat},
                                                  "finish_reason": "stop"}]}
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Transfer-Encoding", "chunked")
                            self.end_headers()
                            self._chunk(("data: %s\n\n" % json.dumps(chunk)).encode())
                            self._chunk(b"data: [DONE]\n\n")
                            self._chunk(b"")
                        else:
                            self._send(200, {
                                "id": "chatcmpl-omnirush",
                                "object": "chat.completion",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant",
                                                         "content": chat}}]})
                        log("OK  %s via %s (omnirush mode) %.1fs"
                            % (model, row["name"], time.time() - t0))
                        return
                    if row.get("api_mode") == "zcode":
                        zc = chat_from_zcode(row, dict(body, stream=False))
                        chat = zc["text"]
                        ROW_STATE[row["name"]] = {
                            "status": "ok", "ms": (time.time() - t0) * 1000,
                            "at": time.time()}
                        if stream:
                            chunk = {"id": "chatcmpl-zcode",
                                     "object": "chat.completion.chunk",
                                     "created": int(time.time()),
                                     "model": model,
                                     "choices": [{"index": 0,
                                                  "delta": {"role": "assistant",
                                                            "content": chat},
                                                  "finish_reason": "stop"}]}
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Transfer-Encoding", "chunked")
                            self.end_headers()
                            self._chunk(("data: %s\n\n" % json.dumps(chunk)).encode())
                            self._chunk(b"data: [DONE]\n\n")
                            self._chunk(b"")
                        else:
                            self._send(200, {
                                "id": "chatcmpl-zcode",
                                "object": "chat.completion",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant",
                                                         "content": chat}}]})
                        log("OK  %s via %s (zcode mode) %.1fs"
                            % (model, row["name"], time.time() - t0))
                        return
                    if row.get("api_mode") == "responses":
                        chat = chat_from_responses(row, body)
                        ROW_STATE[row["name"]] = {
                            "status": "ok", "ms": (time.time() - t0) * 1000,
                            "at": time.time()}
                        if stream:
                            chunk = {"id": "chatcmpl-responses",
                                     "object": "chat.completion.chunk",
                                     "created": int(time.time()),
                                     "model": model,
                                     "choices": [{"index": 0,
                                                  "delta": {"role": "assistant",
                                                            "content": chat},
                                                  "finish_reason": "stop"}]}
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Transfer-Encoding", "chunked")
                            self.end_headers()
                            self._chunk(("data: %s\n\n" % json.dumps(chunk)).encode())
                            self._chunk(b"data: [DONE]\n\n")
                            self._chunk(b"")
                        else:
                            self._send(200, {
                                "id": "chatcmpl-responses",
                                "object": "chat.completion",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant",
                                                         "content": chat}}]})
                        log("OK  %s via %s (responses mode) %.1fs"
                            % (model, row["name"], time.time() - t0))
                        return
                    resp = upstream(row["base"], row["key"],
                                    "/v1/chat/completions", body, row=row)
                    if stream:
                        self.send_response(200)
                        self.send_header("Content-Type",
                                         "text/event-stream")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        try:
                            while True:
                                ch = resp.read(4096)
                                if not ch:
                                    break
                                self._chunk(ch)
                            self._chunk(b"")
                        except (BrokenPipeError, ConnectionResetError):
                            log("stream client disconnected mid-row %s"
                                % row["name"])
                        ROW_STATE[row["name"]] = {
                            "status": "ok", "ms": (time.time() - t0) * 1000,
                            "at": time.time()}
                        log("OK  %s via %s stream %.1fs"
                            % (model, row["name"], time.time() - t0))
                    else:
                        data = resp.read()
                        ROW_STATE[row["name"]] = {
                            "status": "ok", "ms": (time.time() - t0) * 1000,
                            "at": time.time()}
                        self._send(200, raw=data)
                        log("OK  %s via %s %.1fs"
                            % (model, row["name"], time.time() - t0))
                    return
                except Exception as e:
                    last_err = "%s: %s" % (row["name"], e)
                    ROW_STATE[row["name"]] = {
                        "status": "fail",
                        "ms": (time.time() - t0) * 1000,
                        "at": time.time(), "error": str(e)[:200]}
                    log("FAIL %s via %s: %s (%.1fs) -> next"
                        % (model, row["name"], e, time.time() - t0))
            return self._send(502, {"error": "all rows failed for '%s'"
                                           % model, "last": last_err})

        return self._send(404, {"error": "not found"})


# ---------------------------------------------------------------- HTTP client
def api_get(path):
    with urllib.request.urlopen("http://%s:%d%s" % (HOST, PORT, path),
                                timeout=10) as r:
        return json.loads(r.read() or b"{}")


def api_post(path, obj):
    req = urllib.request.Request(
        "http://%s:%d%s" % (HOST, PORT, path),
        data=json.dumps(obj).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT + 10) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except Exception:
            return {"ok": False, "error": "HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- terminal UI
def _tail_events(n):
    try:
        size = os.path.getsize(LOG_PATH)
        with open(LOG_PATH, "rb") as f:
            f.seek(max(0, size - 8192))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return [ln for ln in lines if ln.strip()][-n:]
    except OSError:
        return []


def _putw(win, y, text, w, attr=0):
    """One guarded write: never raises at the screen edge."""
    if y < 0 or w < 2:
        return
    try:
        win.addnstr(y, 0, text, w - 1, attr)
    except Exception:
        pass


def _panel(win, y, w, g, title):
    left = g["lt"] + g["h"]
    mid = (" " + title + " ") if title else ""
    body = max(0, (w - 2) - len(left) - len(mid))
    _putw(win, y, left + mid + g["h"] * body + g["rt"], w, curses.A_DIM)


def _age(ts):
    if not ts:
        return "-"
    d = time.time() - ts
    if d < 90:
        return "%ds" % d
    if d < 5400:
        return "%dm" % (d // 60)
    return "%dh" % (d // 3600)


def oauth_headers(provider, token, org_id=""):
    spec = read_oauth_profiles().get(provider, {})
    h = {"Authorization": "Bearer " + token, "User-Agent": UA}
    for k, v in (spec.get("headers") or {}).items():
        h[k] = str(v).replace("{token}", token)
    if org_id:
        h["X-OAuth-Organization"] = org_id
    return h


def oauth_post(url, provider, token, payload, org_id="", timeout=TIMEOUT):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for k, v in oauth_headers(provider, token, org_id).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


def oauth_device_start(provider):
    spec = read_oauth_profiles().get(provider)
    if not spec:
        raise ValueError("unknown OAuth provider (add it to ~/.minimalrouter/oauth-providers.json first)")
    body = spec.get("authorize_body", {})
    # Device-code issuance is unauthenticated. Some gateways (inference.sh) reject an
    # explicit "Authorization: Bearer " with an empty token; omit it at start.
    form = bool(spec.get("authorize_form"))
    payload = (urllib.parse.urlencode(body).encode() if form
               else json.dumps(body).encode())
    req = urllib.request.Request(spec["authorize_url"], data=payload, method="POST")
    req.add_header("Content-Type",
                   "application/x-www-form-urlencoded" if form else "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    if spec.get("client_secret_mode") == "random_hex":
        secret = os.urandom(32).hex()
        FLOW_SECRETS[provider] = secret
        req.add_header("Authorization", "Bearer " + secret)
    for k, v in (spec.get("authorize_headers") or {}).items():
        req.add_header(k, str(v))
    res = urllib.request.urlopen(req, timeout=TIMEOUT)
    out = json.loads(res.read() or b"{}")
    # Some providers wrap the device payload in {code,msg,data:{flow_id,...}}.
    if isinstance(out.get("data"), dict):
        inner = dict(out["data"])
        if not inner.get("device_code") and inner.get("flow_id"):
            inner["device_code"] = inner["flow_id"]
        return inner
    return out


def oauth_start(provider):
    try:
        d = oauth_device_start(provider)
        url = (d.get("verification_uri_complete") or d.get("verification_uri")
               or d.get("verificationUrl") or d.get("authorize_url")
               or d.get("authorizeUrl") or "")
        if url.startswith("/"):
            spec = read_oauth_profiles().get(provider, {})
            url = _origin(spec.get("authorize_url", "")) + url
        return {"device_code": d.get("device_code") or d.get("code"),
                "user_code": d.get("user_code") or d.get("code", ""),
                "url": url,
                "interval": d.get("interval", 5), "expires": d.get("expires_in", 600)}
    except Exception as e:
        raise RuntimeError("OAuth start failed for %s: %s" % (provider, e))


def _render_body(body, device_code):
    """Substitute {device_code} in static request bodies from a profile."""
    out = {}
    for k, v in (body or {}).items():
        out[k] = (v.replace("{device_code}", str(device_code))
                  if isinstance(v, str) else v)
    return out


def oauth_poll(provider, flow):
    """Poll the token endpoint until approval, honouring pending/400/202 shapes."""
    spec = read_oauth_profiles().get(provider, {})
    deadline = time.time() + float(flow.get("expires", 600))
    poll_url = spec.get("token_url", "")
    method = spec.get("poll_method", "POST").upper()
    body = _render_body(spec.get("token_body") or
                        {"device_code": flow["device_code"]}, flow["device_code"])
    interval = float(flow.get("interval", 5) or 5)
    last_msg = ""
    while time.time() < deadline:
        time.sleep(interval)
        status = 0
        d = {}
        try:
            if method == "GET":
                # Some providers put {device_code} in token_url (inference.sh);
                # others expose a bare poll path and expect the code appended (kilocode).
                if "{device_code}" in poll_url:
                    url = poll_url.replace("{device_code}",
                                           urllib.parse.quote(flow["device_code"], safe=""))
                else:
                    url = poll_url.rstrip("/") + "/" + urllib.parse.quote(
                        flow["device_code"], safe="")
                hdrs = {"User-Agent": UA, "Accept": "application/json"}
                secret = FLOW_SECRETS.get(provider)
                if secret:
                    hdrs["Authorization"] = "Bearer " + secret
                req = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(req, timeout=30) as r:
                    status = r.status
                    d = json.loads(r.read() or b"{}")
                if isinstance(d.get("data"), dict):  # nested token/pending payload
                    d = dict(d["data"], **{k: v for k, v in d.items() if k != "data"})
                    if not d.get("access_token") and d.get("token"):
                        d["access_token"] = d["token"]
            elif spec.get("token_form"):
                data = urllib.parse.urlencode(body).encode()
                req = urllib.request.Request(poll_url, data=data, method="POST")
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
                req.add_header("Accept", "application/json")
                req.add_header("User-Agent", UA)
                for k, v in (spec.get("token_headers") or {}).items():
                    req.add_header(k, str(v).replace("{token}", "").replace(
                        "{device_code}", flow["device_code"]))
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        status, d = r.status, json.loads(r.read() or b"{}")
                except urllib.error.HTTPError as e:
                    status, d = e.code, json.loads(e.read() or b"{}")
            else:
                status, d = _post_json(poll_url, body, timeout=30)
        except Exception as e:  # transport hiccup: keep waiting until deadline
            last_msg = str(e)
            continue

        code = error_code(d)
        # Providers disagree on the credential field. inference.sh returns
        # "session_token" (token_kind=session) or "api_key" (legacy/default);
        # others use "access_token" or "token".
        access = (d.get("access_token") or d.get("token") or
                  d.get("session_token") or d.get("api_key"))
        if access:
            out = {"access_token": access,
                   "refresh_token": d.get("refresh_token", "")}
            if d.get("expires_in"):
                out["expires_at"] = time.time() + float(d["expires_in"])
            elif d.get("expiresAt"):
                ea = float(d["expiresAt"])
                out["expires_at"] = ea / 1000.0 if ea > 1e12 else ea
            else:
                out["expires_at"] = time.time() + 3600
            for k in ("org_id", "organization_id"):
                if d.get(k):
                    out["org_id"] = d[k]
                    break
            return out

        if code in PENDING_CODES:
            last_msg = error_message(d, code)
            # Servers may ask for a slower cadence; respect it.
            bump = None
            if isinstance(d.get("error"), dict) and d["error"].get("interval"):
                bump = d["error"]["interval"]
            elif d.get("interval"):
                bump = d["interval"]
            if code == "slow_down" and bump:
                interval = float(bump)
            continue

        if code in ("access_denied", "expired_token", "invalid_grant",
                    "invalid_request", "unauthorized"):
            raise RuntimeError(error_message(d, code))
        if status in (202, 204, 409, 428):
            continue
        if status >= 500:
            last_msg = "server error %d" % status
            continue
        if not d:
            last_msg = "empty response (HTTP %s)" % status
            continue
        raise RuntimeError(error_message(d, "OAuth failed (HTTP %s)" % status))
    raise RuntimeError(last_msg or "OAuth expired before approval")


def _curses_main(stdscr, port):
    try:
        curses.use_default_colors()
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        HAS_COLOR = True
    except Exception:
        HAS_COLOR = False
    curses.curs_set(0)
    stdscr.nodelay(True)
    try:
        stdscr.addstr(0, 0, "\u2500")
        G = {"tl": "\u250c", "tr": "\u2510", "bl": "\u2514", "br": "\u2518",
             "h": "\u2500", "v": "\u2502", "lt": "\u251c", "rt": "\u2524",
             "sel": "\u25b6", "ok": "\u25cf", "bad": "\u25cf",
             "unk": "\u25cb"}
    except Exception:
        G = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
             "lt": "+", "rt": "+", "sel": ">", "ok": "*", "bad": "x",
             "unk": "."}
    sel = 0
    started = time.time()
    status = "ready - press a to add a row, t to test the selected one"
    COLOR = {"ok": curses.color_pair(1) if HAS_COLOR else 0,
             "fail": curses.color_pair(2) if HAS_COLOR else 0}
    LABEL = curses.A_DIM
    while True:
        online = True
        try:
            st = api_get("/api/state")
            rows = st.get("rows", [])
            nmodels = st.get("models", 0)
        except Exception:
            rows, nmodels, online = [], 0, False
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        if sel >= len(rows):
            sel = max(0, len(rows) - 1)

        # ---- header -----------------------------------------------------
        up = int(time.time() - started)
        head = (" MINIMALROUTER  |  http://%s:%d/v1  |  %d rows  %d models  |  "
                "up %dm%02ds  |  %s" % (
                    HOST, port, len(rows), nmodels,
                    up // 60, up % 60,
                    "no server auth / localhost only" if online
                    else "SERVER OFFLINE"))
        _putw(stdscr, 0, head, w, curses.A_REVERSE | curses.A_BOLD)
        if h < 16 or w < 60:  # tiny terminal: rows + legend only
            maxr = max(1, h - 3)
            top = max(0, sel - maxr + 1) if sel >= maxr else 0
            for i, row in enumerate(rows[top:top + maxr]):
                s = row.get("state", {}) or {}
                stt = s.get("status", "unknown")
                line = " %s %-18s %-22s %7s %5s" % (
                    {"ok": G["ok"], "fail": G["bad"]}.get(stt, G["unk"]),
                    row["name"][:18], row["model"][:22],
                    ("%dms" % int(s.get("ms") or 0))
                    if stt != "unknown" else "-", _age(s.get("at")))
                _putw(stdscr, 1 + i, line, w,
                      curses.A_REVERSE if i + top == sel else 0)
            _putw(stdscr, h - 2, " " + status, w, curses.A_BOLD)
            _putw(stdscr, h - 1,
                  " up/down · t test · a add · d delete · r refresh · q quit",
                  w, curses.A_DIM)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch == -1:
                time.sleep(0.4)
                continue
            if ch in (ord("q"), ord("Q")):
                return
            if ch == curses.KEY_DOWN:
                sel = min(sel + 1, max(0, len(rows) - 1))
            elif ch == curses.KEY_UP:
                sel = max(0, sel - 1)
            continue

        # ---- column header ---------------------------------------------
        name_w = 26 if w >= 110 else (20 if w >= 92 else 15)
        rest = w - (12 + name_w + 8 + 6 + 8)  # space left for model+base (CLASS col added)
        model_w = max(16, int(rest * 0.62))
        base_w = max(10, rest - model_w - 1)
        cols = "   %-7s %-*s %-*s %-*s %7s %5s" % ("CLASS", name_w, "NAME",
                                             model_w, "MODEL", base_w, "HOST",
                                             "LAT", "LAST")
        _putw(stdscr, 1, cols, w, curses.A_BOLD)
        _panel(stdscr, 2, w, G, "")

        # ---- layout budget ---------------------------------------------
        detail_h = 8
        table_rows = min(len(rows), max(3, h - (3 + detail_h + 3 + 4)))
        top = max(0, sel - table_rows + 1) if sel >= table_rows else 0
        y = 3
        for i, row in enumerate(rows[top:top + table_rows]):
            idx = i + top
            s = row.get("state", {}) or {}
            stt = s.get("status", "unknown")
            lat = ("%dms" % int(s.get("ms") or 0)) if stt != "unknown" else "-"
            host = urllib.parse.urlparse(row["base"]).netloc[:base_w]
            mark = {"ok": G["ok"], "fail": G["bad"]}.get(stt, G["unk"])
            line = " %s %s %-7s %-*s %-*s %-*s %7s %5s" % (
                G["sel"] if idx == sel else " ", mark,
                class_label(row.get("class", row_class(row))),
                name_w, row["name"][:name_w],
                model_w, row["model"][:model_w],
                base_w, host, lat, _age(s.get("at")))
            _putw(stdscr, y + i, line, w,
                  curses.A_REVERSE if idx == sel else 0)
            if idx != sel and HAS_COLOR and stt in COLOR:
                try:
                    stdscr.addnstr(y + i, 3, mark, 1, COLOR[stt])
                except Exception:
                    pass
        y = 3 + table_rows

        # ---- selected row details --------------------------------------
        _panel(stdscr, y, w, G, "SELECTED")
        y += 1
        if rows:
            row = rows[sel]
            s = row.get("state", {}) or {}
            stt = s.get("status", "unknown")
            pairs = [
                ("name", row["name"]),
                ("class", class_label(row.get("class", row_class(row)))),
                ("model", row["model"]),
                ("base", row["base"]),
                ("key", row.get("key_src", "-")),
                ("state", "%s  %s  last %s" % (
                    stt.upper(),
                    ("%dms" % int(s.get("ms") or 0))
                    if stt != "unknown" else "-", _age(s.get("at")))),
                ("error", (s.get("error") or "-")),
            ]
            for lbl, val in pairs:
                try:
                    stdscr.addnstr(y, 2, lbl, 6, LABEL)
                    stdscr.addnstr(y, 9, str(val), max(1, w - 11),
                                   COLOR[stt] if (lbl == "state" and
                                                  stt in COLOR) else 0)
                except Exception:
                    pass
                y += 1
        else:
            _putw(stdscr, y, "  no rows - press a to add one", w, LABEL)
            y += detail_h - 1

        # ---- live events from the log ----------------------------------
        _panel(stdscr, y, w, G, "EVENTS")
        y += 1
        room = max(1, (h - 2) - y)
        ev = _tail_events(room)
        if not ev:
            _putw(stdscr, y, "  (no traffic yet)", w, LABEL)
        for j, ln in enumerate(ev):
            body = ln.split("] ", 1)[-1]
            _putw(stdscr, y + j, "  " + body, w,
                  COLOR["ok"] if " OK " in ln else
                  (COLOR["fail"] if "FAIL" in ln else LABEL))

        # ---- status + legend -------------------------------------------
        _putw(stdscr, h - 2, " " + status, w, curses.A_BOLD)
        _putw(stdscr, h - 1,
              " up/down select  t test  a add  d delete  r refresh  "
              "q quit (stops server)  Esc cancel", w, curses.A_DIM)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == -1:
            time.sleep(0.4)
            continue
        if ch in (ord("q"), ord("Q")):
            return
        if ch == curses.KEY_DOWN:
            sel = min(sel + 1, max(0, len(rows) - 1))
        elif ch == curses.KEY_UP:
            sel = max(0, sel - 1)
        elif ch in (ord("r"), ord("R")):
            status = "refreshed %s" % time.strftime("%H:%M:%S")
        elif ch in (ord("t"), ord("T")):
            if rows:
                row = rows[sel]
                status = "testing %s ..." % row["name"]
                _draw_status(stdscr, status, w, h)
                r = api_post("/api/test", {"base": row["base"],
                                           "key": "",
                                           "model": row["model"]})
                status = (("%s: OK %dms %s" % (row["name"], r.get("ms", 0),
                                               r.get("content", "")))
                          if r.get("ok") else
                          ("%s: %s" % (row["name"], r.get("error", "fail"))))
        elif ch in (ord("a"), ord("A")):
            res = _add_flow(stdscr)
            status = res or "add cancelled"
        elif ch in (ord("d"), ord("D")):
            if rows:
                row = rows[sel]
                conf = _input_line(stdscr, 'delete "%s"? (y/n) '
                                   % row["name"])
                if conf and conf.lower().startswith("y"):
                    r = api_post("/api/remove", {"name": row["name"]})
                    status = ("removed %s" % row["name"]) if r.get("ok") \
                        else r.get("error", "remove failed")
        stdscr.nodelay(True)


def _draw_status(stdscr, msg, w, h):
    stdscr.addnstr(h - 2, 0, (" " * (w - 1)), w - 1)
    stdscr.addnstr(h - 2, 0, msg[:w - 1], w - 1, curses.A_BOLD)
    stdscr.refresh()


def _input_line(stdscr, label):
    h, w = stdscr.getmaxyx()
    buf = ""
    stdscr.nodelay(False)
    try:
        while True:
            stdscr.addnstr(h - 2, 0, " " * (w - 1), w - 1)
            stdscr.addnstr(h - 2, 0, label + buf, w - 1, curses.A_BOLD)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (10, 13):
                break
            if ch == 27:
                buf = None
                break
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
            elif 32 <= ch < 127:
                buf += chr(ch)
    finally:
        stdscr.nodelay(True)
    return None if buf is None else buf.strip()


def _pick_list(stdscr, items, title):
    idx, flt = 0, ""
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        head = title
        if flt:
            head += "   filter: " + flt
        stdscr.addnstr(0, 0, head, w - 1, curses.A_BOLD | curses.A_REVERSE)
        shown = [m for m in items if flt.lower() in m.lower()]
        maxr = h - 4
        if len(shown) > maxr and idx >= maxr:
            top = idx - maxr + 1
        else:
            top = 0
        for i, m in enumerate(shown[top:top + maxr]):
            attr = curses.A_REVERSE if i + top == idx else curses.A_NORMAL
            stdscr.addnstr(2 + i, 1, " " + m[:w - 4], w - 2, attr)
        stdscr.addnstr(h - 2, 0,
                       "type to filter · Enter pick · Esc cancel · %d models"
                       % len(shown), w - 1, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13):
            return shown[idx] if shown else None
        if ch == 27:
            return None
        if ch == curses.KEY_DOWN:
            idx = min(idx + 1, max(0, len(shown) - 1))
        elif ch == curses.KEY_UP:
            idx = max(0, idx - 1)
        elif ch == curses.KEY_NPAGE:
            idx = min(idx + maxr, max(0, len(shown) - 1))
        elif ch == curses.KEY_PPAGE:
            idx = max(0, idx - maxr)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            flt, idx = flt[:-1], 0
        elif 32 <= ch < 127:
            flt, idx = flt + chr(ch), 0


def _add_flow(stdscr):
    base = _input_line(stdscr, "Base URL (https://.../v1): ")
    if not base:
        return None
    key = _input_line(stdscr, "API key (blank if none): ")
    if key is None:
        return None
    _draw_status(stdscr, "fetching model list ...", *stdscr.getmaxyx()[::-1])
    r = api_post("/api/fetch_models", {"base": base, "key": key})
    model = None
    if r.get("ok") and r.get("models"):
        model = _pick_list(stdscr, r["models"], "pick a model")
    if not model:
        model = _input_line(stdscr, "model id: ")
    if not model:
        return None
    _draw_status(stdscr, "testing %s ..." % model, *stdscr.getmaxyx()[::-1])
    t = api_post("/api/test", {"base": base, "key": key, "model": model})
    if not t.get("ok"):
        return "test failed: %s" % t.get("error", "?")
    a = api_post("/api/add", {"base": base, "key": key, "model": model})
    if a.get("ok"):
        return "added %s (test OK %dms)" % (a.get("name"),
                                            t.get("ms", 0))
    return "add failed: %s" % a.get("error", "?")


def oauth_refresh(provider, timeout=30):
    """Force a token refresh for one provider. Returns (ok, message).

    Handles three shapes: generic header/body refresh, Nous' header-carried secret,
    and omnirush's dedicated control-plane refresh with rotating token + gateway_url.
    Never prints the token.
    """
    profiles = read_oauth_profiles()
    if provider not in profiles:
        return False, "unknown provider %r (see: minimalrouter oauth list)" % provider
    spec = profiles[provider] or {}
    auth = read_oauth()
    rec = auth.get(provider) or {}
    rt = rec.get("refresh_token", "")
    if not rt:
        return False, "%s: no refresh token — re-login with: minimalrouter oauth %s" % (
            provider, provider)
    if spec.get("api_mode") == "omnirush":
        # single-flight and atomic persist live inside omnirush_access_token
        tok = omnirush_access_token(provider)
        return (True, "refreshed") if tok else (False, "refresh failed (see log)")
    url = spec.get("refresh_url")
    if not url:
        return False, "%s: no refresh_url — re-login with: minimalrouter oauth %s" % (
            provider, provider)
    body = {k: str(v).replace("{refresh_token}", rt)
            for k, v in (spec.get("refresh_body") or
                         {"refresh_token": "{refresh_token}"}).items()}
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", UA)
        for k, v in (spec.get("refresh_headers") or {}).items():
            req.add_header(k, str(v).replace("{refresh_token}", rt))
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")
    except Exception as e:
        return False, "refresh request failed: %s" % e
    new = d.get("access_token") or d.get("token")
    if not new:
        return False, "no token in refresh response: %s" % str(d)[:120]
    rec = dict(rec)
    rec["access_token"] = new
    if d.get("refresh_token"):
        rec["refresh_token"] = d["refresh_token"]
    rec["expires_at"] = time.time() + float(d.get("expires_in", 3600))
    auth[provider] = rec
    write_oauth(auth)
    return True, "refreshed (new expiry in %s s)" % int(float(d.get("expires_in", 3600)))


def stop_server():
    """Stop MinimalRouter instances owned by this user, by verified PID only.

    Scans /proc for processes whose cmdline contains minimalrouter.py, skips self,
    verifies the path is our script, and signals each PID by number. Never uses
    pkill -f (which self-matches and can kill the caller).
    """
    me = os.getpid()
    ours = os.path.realpath(__file__)
    targets = []
    try:
        entries = os.listdir("/proc")
    except OSError as e:
        return 0, "cannot read /proc: %s" % e
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                parts = [x.decode("utf-8", "replace") for x in f.read().split(b"\0") if x]
        except (OSError, ValueError):
            continue
        if not any("minimalrouter.py" in x for x in parts):
            continue
        if not any(os.path.realpath(x) == ours for x in parts if x.endswith(".py")):
            continue
        targets.append((pid, parts))
    if not targets:
        return 0, "no MinimalRouter process running"
    stopped = []
    for pid, parts in targets:
        try:
            os.kill(pid, 15)  # SIGTERM
            stopped.append(str(pid))
        except ProcessLookupError:
            pass
        except OSError as e:
            return len(stopped), "PID %s: %s" % (pid, e)
    return len(stopped), "stopped PID(s): %s" % ", ".join(stopped)


def run_server():
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


class _Srv(ThreadingHTTPServer):
    pass


def usage():
    return """MinimalRouter - one OpenAI-compatible endpoint over free API rows.

Usage:
  minimalrouter              terminal dashboard + server (one process)
  minimalrouter serve        server only (tmux / systemd / background)
  minimalrouter oauth add    create an OAuth provider profile
  minimalrouter oauth list   show profiles and connection state
  minimalrouter oauth <id>   sign in to that provider (device code)
  minimalrouter oauth rm <id>  delete a profile and its token
  minimalrouter oauth list   profiles with class + connection state
  minimalrouter refresh [id] refresh OAuth token(s) now (all, or one provider)
  minimalrouter stop         stop the running server/dashboard by verified PID
  minimalrouter -h|--help    show this help
  minimalrouter -v           show version

Endpoint: http://127.0.0.1:%d/v1     (env MINIMALROUTER_PORT overrides the port)
No server authentication. Localhost only. Three connection classes, one engine path:
  API     - a key or key_env in rows.json (dashboard: press a, paste URL + key).
  OAUTH   - a device-flow profile: `oauth add` then `oauth <id>`.
  KEYLESS - an API row with no key; the upstream serves it with no Authorization header.
Class is derived from the row's real shape, never from a label; a row cannot claim a class.
The dashboard and GET /api/state show the class of every row. Client format is OpenAI Chat
Completions.

Dashboard keys:
  up/down  select row     t  re-test row      a  add a row
  d        delete row     r  refresh          q  quit (stops the server)
  Esc      cancel a prompt

Add a row: run `minimalrouter`, press a, paste base URL + API key,
pick a model from the fetched list; the row is saved only if the test passes.

Add an OAuth provider: `minimalrouter oauth add`, then `minimalrouter oauth <id>`.

Files:
  ~/.minimalrouter/rows.json           rows: {name, base, model, key_env|key}
  ~/.minimalrouter/oauth-providers.json optional generic device-flow profiles
  ~/.minimalrouter/oauth.json           local OAuth tokens (chmod 600)
  ~/.minimalrouter/minimalrouter.log   per-request OK/FAIL log
""" % PORT


def main():
    os.makedirs(MR_HOME, exist_ok=True)
    if not os.path.exists(ROWS_PATH):
        write_rows([])
    args = sys.argv[1:]
    if args and args[0].lower() in ("-h", "--help", "-help", "help"):
        print(usage())
        return 0
    if args and args[0].lower() in ("-v", "--version", "version"):
        print("minimalrouter 1.0")
        return 0
    if args and args[0].lower() == "oauth":
        profiles = read_oauth_profiles()
        sub = args[1].lower() if len(args) > 1 else ""

        if sub == "list":
            auth = read_oauth()
            if not profiles:
                print("No OAuth profiles yet. Add one: minimalrouter oauth add")
                return 0
            for pid, spec in sorted(profiles.items()):
                state = "connected" if (auth.get(pid) or {}).get("access_token") else "not connected"
                hint = spec.get("class") if spec.get("class") in VALID_CLASSES else "oauth"
                print("%-14s %-24s %-12s class: %-7s models: %s"
                      % (pid, spec.get("label", pid), state, hint,
                         ", ".join(m if isinstance(m, str) else m.get("model", "")
                                   for m in spec.get("models", []))))
            return 0

        if sub == "rm":
            if len(args) != 3 or args[2] not in profiles:
                print("Usage: minimalrouter oauth rm <profile-id>")
                return 2
            pid = args[2]
            profiles.pop(pid, None)
            write_oauth_profiles(profiles)
            auth = read_oauth()
            if pid in auth:
                auth.pop(pid)
                write_oauth(auth)
            print("removed profile %s" % pid)
            return 0

        if sub == "add":
            def ask(prompt, default=""):
                try:
                    v = input(prompt).strip()
                except EOFError:
                    return ""
                return v or default
            print("New OAuth provider profile.\n")
            seed = ask("tool name (e.g. myprovider) or base URL: ").strip()
            if not seed:
                print("cancelled")
                return 2
            registry = read_provider_registry()
            entry = {}
            if seed.startswith("http"):
                base_url = seed.rstrip("/")
                pid = ask("profile id (letters, e.g. myprovider): ").strip().lower()
            else:
                pid = seed.lower()
                entry = registry.get(pid) or {}
                if entry.get("base_url"):
                    print("  known provider: %s -> %s"
                          % (entry.get("label", pid), entry["base_url"]))
                    if entry.get("models"):
                        print("  models: %s"
                              % ", ".join(m if isinstance(m, str)
                                          else m.get("model", "")
                                          for m in entry["models"]))
                    base_url = entry["base_url"]
                    edit = ask("  use this base URL? [Y/n] ", "y").lower()
                    if edit.startswith("n"):
                        base_url = ask("  base URL: ").strip()
                else:
                    print("  %s is not in provider-registry.json yet." % pid)
                    print("  give its base URL once — I store it under this name"
                          " so next time it is just `oauth add %s`." % pid)
                    base_url = ask("  base URL (e.g. https://host/v1): ").strip()
            if not pid:
                print("cancelled: profile id is required")
                return 2
            if not pid.replace("-", "").replace("_", "").isalnum():
                print("cancelled: use letters, digits, - or _ only")
                return 2
            if pid in profiles:
                print("profile %r already exists; use another id" % pid)
                return 2
            if not base_url:
                print("cancelled: base URL is required")
                return 2
            base_url = base_url.rstrip("/")
            label = entry.get("label") or ask("display label: ", pid)

            print("  discovering device login on %s ..." % _origin(base_url))
            found = {}
            # Curated registry values are authoritative; discovery fills gaps.
            for k in ("authorize_url", "token_url", "refresh_url"):
                if entry.get(k):
                    found[k] = entry[k]
            missing = [k for k in ("authorize_url", "token_url") if not found.get(k)]
            if missing:
                found2 = discover_device_endpoints(base_url)
                for k in missing:
                    if found2.get(k):
                        found[k] = found2[k]
                found2.get("source") and print("  (discovered: %s)" % found2["source"])
            found = found or {}
            authorize_url = found.get("authorize_url", "")
            token_url = found.get("token_url", "")
            refresh_url = found.get("refresh_url", "")
            if authorize_url:
                print("  authorize : %s" % authorize_url)
                if token_url:
                    print("  token     : %s" % token_url)
                if refresh_url:
                    print("  refresh   : %s" % refresh_url)
                if authorize_url == token_url and entry.get("poll_method"):
                    print("  poll      : %s <code>" % entry.get("poll_method"))
                use = ask("  use the resolved endpoints? [Y/n] ", "y").lower()
                if use.startswith("n"):
                    authorize_url = token_url = refresh_url = ""
            if not token_url:
                if authorize_url:
                    print("  token endpoint not identified — enter it (source: the"
                          " tool's login code or docs)")
                else:
                    print("  no standard device endpoint answered on that host —"
                          " enter them")
                authorize_url = authorize_url or ask("  authorize URL (device code endpoint): ")
                token_url = ask("  token URL (poll endpoint): ")
                refresh_url = refresh_url or ask("  refresh URL (blank if none): ")
            if not (authorize_url and token_url):
                print("cancelled: authorize and token URLs are required")
                return 2

            models = ""
            reg_default = ",".join(m if isinstance(m, str) else m.get("model", "")
                                   for m in (entry.get("models") or []))
            status, listing = _get_json(base_url + "/models")
            ids = [m.get("id", "") for m in (listing.get("data") or [])
                   if isinstance(m, dict) and m.get("id")] if status == 200 else []
            if ids:
                print("  models found: %s" % ", ".join(ids[:8]))
                models = ask("  models to expose (comma separated, Enter = all): ",
                             ",".join(ids))
            elif reg_default:
                models = ask("  models, comma separated: ", reg_default)
            else:
                models = ask("  models, comma separated: ")
            if not models:
                print("cancelled: at least one model id is required")
                return 2

            api_mode = ask("  api_mode [chat_completions/responses] ",
                           entry.get("api_mode", "chat_completions"))

            spec = {
                "label": label,
                "authorize_url": authorize_url,
                "token_url": token_url,
                "base_url": base_url,
                "models": [m.strip() for m in models.split(",") if m.strip()],
                "authorize_body": {},
                "refresh_body": {"refresh_token": "{refresh_token}"},
            }
            if refresh_url:
                spec["refresh_url"] = refresh_url
            if api_mode:
                spec["api_mode"] = api_mode
            if entry.get("poll_method"):
                spec["poll_method"] = entry["poll_method"]
            for k in ("token_body", "authorize_body", "headers",
                      "authorize_form", "token_form", "token_headers",
                      "refresh_headers", "refresh_body"):
                if entry.get(k):
                    spec[k] = entry[k]
            profiles[pid] = spec
            write_oauth_profiles(profiles)
            # Remember name -> endpoints so the next add is a single word.
            registry[pid] = {"label": label, "base_url": base_url,
                             "models": spec["models"]}
            if spec.get("api_mode"):
                registry[pid]["api_mode"] = spec["api_mode"]
            try:
                tmp = PROVIDER_REGISTRY_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"providers": registry}, f, indent=2)
                    f.write("\n")
                os.replace(tmp, PROVIDER_REGISTRY_PATH)
                print("  remembered in provider-registry.json as %r" % pid)
            except OSError:
                pass
            print("\nprofile %s saved" % pid)
            print("next: minimalrouter oauth %s" % pid)
            return 0

        if len(args) != 2 or args[1].lower() not in profiles:
            print("Usage: minimalrouter oauth <profile-id> | add | list | rm <id>")
            print("Profiles in %s: %s"
                  % (OAUTH_PROFILES_PATH, ", ".join(sorted(profiles)) or "(none)"))
            return 2
        provider = args[1].lower()
        print("Starting %s OAuth device flow..." % profiles[provider].get("label", provider))
        try:
            flow = oauth_start(provider)
            print("Open this URL in your browser:\n  %s" % flow["url"])
            if flow.get("user_code"):
                print("Code: %s" % flow["user_code"])
            print("Waiting for approval (tokens are stored only in ~/.minimalrouter/oauth.json)...")
            token = oauth_poll(provider, flow)
            auth = read_oauth()
            auth[provider] = token
            write_oauth(auth)
            print("OAuth connected: %s" % profiles[provider].get("label", provider))
            print("Models are now exposed through MinimalRouter; start `minimalrouter serve`.")
            return 0
        except Exception as e:
            print("OAuth failed: %s" % e)
            return 1
    if args and args[0].lower() == "stop":
        n, msg = stop_server()
        print("MinimalRouter: " + msg)
        return 0 if n or "no MinimalRouter" in msg else 1
    if args and args[0].lower() == "refresh":
        profiles = read_oauth_profiles()
        ids = [args[1].lower()] if len(args) > 1 else sorted(profiles)
        if not ids:
            print("No OAuth profiles. Add one: minimalrouter oauth add")
            return 0
        bad = 0
        for pid in ids:
            ok, msg = oauth_refresh(pid)
            print("%-12s %s" % (pid, ("OK " if ok else "FAIL ") + msg))
            bad += 0 if ok else 1
        return 1 if bad else 0
    mode = args[0] if args else "tui"
    if mode not in ("tui", "serve"):
        print("minimalrouter: unknown command %r\n" % mode)
        print(usage())
        return 2
    try:
        srv = _Srv((HOST, PORT), Handler)
    except OSError as e:
        print("MinimalRouter: cannot bind %s:%d (%s)" % (HOST, PORT, e))
        print("Another instance is likely running. Stop it or set "
              "MINIMALROUTER_PORT.")
        return 1
    log("MinimalRouter starting on %s:%d mode=%s" % (HOST, PORT, mode))
    if mode == "serve":
        print("MinimalRouter serving on http://%s:%d/v1  (dashboard: "
              "run `minimalrouter`)" % (HOST, PORT))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    t = Thread(target=srv.serve_forever, daemon=True)
    t.start()
    if curses is None:
        log("curses unavailable; server-only")
        print("curses not available on this Python — server running on "
              "http://%s:%d/v1 (Ctrl-C to stop)" % (HOST, PORT))
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
    else:
        try:
            curses.wrapper(_curses_main, PORT)
        except KeyboardInterrupt:
            pass
    srv.shutdown()
    log("MinimalRouter stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""MinimalRouter — one clean OpenAI-compatible endpoint over N API rows.

Single file, stdlib only. No server authentication, no web UI. Binds 127.0.0.1.
Terminal dashboard (curses) in the same process.

Two ways to add a provider:
  1. API key row  — dashboard `a`, paste base URL + key, pick a model.
  2. OAuth row    — `minimalrouter oauth add` (writes a provider profile),
                    then `minimalrouter oauth <profile-id>` (device sign-in).

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
import secrets
import uuid
import sys
import time
import datetime
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread

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
        return providers if isinstance(providers, dict) else {}
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


def _dig(d, path):
    for part in (path or "").split("."):
        if not part or not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _envelope(payload, spec):
    if isinstance(payload, dict) and spec.get("envelope"):
        inner = _dig(payload, spec["envelope"])
        if isinstance(inner, dict):
            return inner
    return payload


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


def _curl_requests():
    try:
        from curl_cffi import requests
    except ImportError:
        runtime = os.path.join(MR_HOME, 'python-runtime')
        if runtime not in sys.path:
            sys.path.insert(0, runtime)
        try:
            from curl_cffi import requests
        except ImportError:
            raise RuntimeError('OpenCode Free requires curl_cffi; install with python3 -m pip install --target ~/.minimalrouter/python-runtime curl_cffi') from None
    return requests


OPENCODE_SESSIONS = {}


class _CurlStream:
    def __init__(self, response, session):
        self.response = response
        self.session = session
        self.chunks = iter(response.iter_content())
        self.closed = False

    def read(self, n=-1):
        if self.closed:
            return b''
        if n < 0:
            try:
                return b''.join(self.chunks)
            finally:
                self.close()
        try:
            return next(self.chunks)
        except StopIteration:
            self.close()
            return b''
        except Exception:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            self.response.close()
            self.session.close()


def opencode_chat(row, payload, spec, timeout=TIMEOUT):
    requests = _curl_requests()
    from curl_cffi import CurlOpt
    import io
    with ROW_LOCK:
        identity = row.get('provider_id') or row['base']
        sid = OPENCODE_SESSIONS.get(identity)
        if not sid:
            sid = 'ses_' + format((~(int(time.time() * 1000) * 4096 + 1)) & ((1 << 48) - 1), '012x') + secrets.token_hex(7)
            OPENCODE_SESSIONS[identity] = sid
    body = json.loads(json.dumps(payload))
    body['model'] = row['model']
    body['stream'] = True
    tools = body.setdefault('tools', [])
    names = {t.get('function', {}).get('name') for t in tools}
    for tool in spec['required_tools']:
        if tool['function']['name'] not in names:
            tools.append(tool)
    body.setdefault('messages', []).insert(0, {'role': 'system', 'content': spec['system_prompt']})
    headers = {k: v.replace('{session}', sid) for k, v in spec['headers'].items()}
    session = requests.Session(default_headers=False, curl_options={CurlOpt.SSL_CIPHER_LIST: spec['cipher_list']})
    try:
        response = session.post(ep(row['base'], '/v1/chat/completions'), headers=headers, json=body, stream=True, timeout=timeout)
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise RuntimeError('OpenCode Free returned HTTP %d' % status)
    except Exception:
        session.close()
        raise
    stream = _CurlStream(response, session)
    if payload.get('stream'):
        return stream
    result = {'id': '', 'object': 'chat.completion', 'created': int(time.time()), 'model': payload['model'], 'choices': []}
    message = {'role': 'assistant', 'content': ''}
    calls = {}
    finish = 'stop'
    try:
        for line in response.iter_lines():
            if not line.startswith(b'data: '):
                continue
            if line[6:] == b'[DONE]':
                break
            event = json.loads(line[6:])
            result['id'] = event.get('id', result['id'])
            if event.get('usage'):
                result['usage'] = event['usage']
            for choice in event.get('choices', []):
                delta = choice.get('delta') or {}
                message['content'] += delta.get('content') or ''
                for call in delta.get('tool_calls') or []:
                    target = calls.setdefault(call['index'], {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                    if call.get('id'): target['id'] = call['id']
                    for key in ('name', 'arguments'):
                        target['function'][key] += (call.get('function') or {}).get(key) or ''
                finish = choice.get('finish_reason') or finish
    finally:
        stream.close()
    if calls:
        message['tool_calls'] = [calls[k] for k in sorted(calls)]
    result['choices'] = [{'index': 0, 'message': message, 'finish_reason': finish}]
    return io.BytesIO(json.dumps(result).encode())


def upstream(base, key, path, payload=None, timeout=TIMEOUT, row=None):
    # Per-profile behavior only; no provider is known by name.
    row = row or {}
    for spec in read_provider_registry().values():
        if spec.get('transport') == 'curl_cffi' and spec.get('base_url', '').rstrip('/') == base.rstrip('/') and path == '/v1/chat/completions':
            return opencode_chat(row, payload, spec, timeout)
    provider = row.get("oauth_provider", "")
    api_mode = row.get("api_mode", "chat_completions")
    if api_mode == "responses" and path == "/v1/chat/completions":
        return oauth_chat(row, payload, timeout)
    if api_mode == "anthropic" and path == "/v1/chat/completions":
        return anthropic_chat(row, payload, timeout)
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


ANTHROPIC_DEFAULT_SYSTEM = [{"type": "text", "text": "{client_system}"}]


def _content_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                out.append({"type": "text", "text": str(c.get("text", ""))})
        return out
    return []


def anthropic_chat(row, payload, timeout=TIMEOUT):
    spec = read_oauth_profiles().get(row.get("oauth_provider", ""), {})
    token = row.get("key", "")
    messages = payload.get("messages", [])
    system = "\n\n".join(str(m.get("content", "")) for m in messages
                         if m.get("role") == "system")
    blocks = [dict(b) for b in (spec.get("system_blocks")
                                or ANTHROPIC_DEFAULT_SYSTEM)]
    for b in blocks:
        b["text"] = str(b.get("text", "")).replace("{client_system}",
                                                   system or "n/a")
    if spec.get("append_client_system") and system:
        blocks[-1]["text"] += "\n\n# Client instructions\n" + system
    msgs = [{"role": m.get("role", "user"),
             "content": _content_blocks(m.get("content", ""))}
            for m in messages if m.get("role") in ("user", "assistant")]
    model = row.get("model") or (spec.get("model_map") or {}).get(
        payload.get("model", ""), payload.get("model", ""))
    body = {"model": model, "max_tokens": payload.get("max_tokens") or 8192,
            "system": blocks, "messages": msgs, "stream": False}
    if msgs and msgs[-1]["content"]:
        msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    if spec.get("user_metadata"):
        body["metadata"] = {"user_id": json.dumps({
            "device_id": str(uuid.uuid4()), "account_uuid": "",
            "session_id": "sess_" + str(uuid.uuid4())})}
    url = row["base"].rstrip("/")
    if not url.endswith("/messages"):
        url += "/v1/messages"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for hk, hv in oauth_headers(row.get("oauth_provider", ""), token,
                                row.get("org_id", "")).items():
        req.add_header(hk, hv)
    for hk, hv in (spec.get("request_headers") or {}).items():
        req.add_header(hk, str(hv).replace("{uuid}", str(uuid.uuid4())))
    return urllib.request.urlopen(req, timeout=timeout)


def anthropic_text(raw):
    parts = []
    for c in (raw or {}).get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text", "")))
    return "".join(parts)


class _AnthropicResponse:
    def __init__(self, raw):
        self._raw = raw

    def read(self, n=-1):
        if isinstance(self._raw, (bytes, bytearray)):
            return bytes(self._raw)
        return json.dumps(self._raw).encode()


def chat_from_anthropic(row, payload):
    resp = anthropic_chat(row, dict(payload, stream=False))
    raw = json.loads(resp.read() or b"{}")
    if raw.get("code") or raw.get("error"):
        raise RuntimeError(raw.get("msg") or str(raw.get("error")))
    text = anthropic_text(raw)
    openai = {"id": raw.get("id", "chatcmpl-anthropic"),
              "object": "chat.completion",
              "created": int(time.time()),
              "model": payload.get("model", ""),
              "choices": [{"index": 0, "finish_reason": "stop",
                           "message": {"role": "assistant", "content": text}}],
              "usage": {"prompt_tokens": (raw.get("usage") or {}).get("input_tokens", 0),
                        "completion_tokens": (raw.get("usage") or {}).get("output_tokens", 0),
                        "total_tokens": ((raw.get("usage") or {}).get("input_tokens", 0)
                                         + (raw.get("usage") or {}).get("output_tokens", 0))}}
    return _AnthropicResponse(openai)


def responses_text(raw):
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


def probe(base, key, model, row=None):
    """Marker probe. Returns (ok, ms, error, content)."""
    t0 = time.time()
    payload = {"model": model, "max_tokens": 200,
               "messages": [{"role": "user",
                             "content": "Reply with exactly: OK"}]}
    try:
        if (row or {}).get("api_mode") == "anthropic":
            resp = chat_from_anthropic(row, dict(payload, stream=False))
            body = json.loads(resp.read() or b"{}")
        else:
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
    data = body if isinstance(body, list) else body.get("data", [])
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


def state_for(rows):
    out = []
    for r in rows:
        st = dict(ROW_STATE.get(r["name"], {"status": "unknown"}))
        out.append({"name": r["name"], "model": r["model"], "base": r["base"],
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
            return self._send(200, {"ok": True, "rows": len(rows),
                                    "models": len(BY_MODEL(rows)),
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
            ok, ms, err, content = probe(base, key, model)
            if not ok:
                return self._send(400, {"ok": False,
                                        "error": "test failed: %s" % err})
            with ROW_LOCK:
                rows = read_rows()  # file rows only: never persist OAuth rows
                while any(r["name"] == name for r in rows):
                    name, i = "%s-%d" % (name, i), i + 1
                rows.append({"name": name, "base": base, "model": model,
                             "key": key})
                write_rows(rows)
            ROW_STATE[name] = {"status": "ok", "ms": ms, "at": time.time()}
            log("ADD %s (%s / %s) ok %.0fms" % (name, base, model, ms))
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
                    if row.get("api_mode") == "anthropic":
                        resp = chat_from_anthropic(row, body)
                        data = resp.read()
                        ROW_STATE[row["name"]] = {
                            "status": "ok", "ms": (time.time() - t0) * 1000,
                            "at": time.time()}
                        if stream:
                            parsed = json.loads(data)
                            chunk = {"id": parsed.get("id", "chatcmpl-anthropic"),
                                     "object": "chat.completion.chunk",
                                     "created": int(time.time()),
                                     "model": model,
                                     "choices": [{"index": 0,
                                                  "delta": {"role": "assistant",
                                                            "content": (parsed.get("choices") or [{}])[0]
                                                                       .get("message", {}).get("content", "")},
                                                  "finish_reason": "stop"}]}
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Transfer-Encoding", "chunked")
                            self.end_headers()
                            self._chunk(("data: %s\n\n" % json.dumps(chunk)).encode())
                            self._chunk(b"data: [DONE]\n\n")
                            self._chunk(b"")
                        else:
                            self._send(200, raw=data)
                        log("OK  %s via %s (anthropic mode) %.1fs"
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
                        finally:
                            close = getattr(resp, 'close', None)
                            if close:
                                close()
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


def oauth_device_start(provider, options=None):
    spec = read_oauth_profiles().get(provider)
    if not spec:
        raise ValueError("unknown OAuth provider (add it to ~/.minimalrouter/oauth-providers.json first)")
    body = dict(spec.get("authorize_body", {}))
    if options:
        body.update(options)
    token = ""
    if spec.get("client_poll_token"):
        token = secrets.token_hex(32)
    res = oauth_post(spec["authorize_url"], provider, token, body)
    return json.loads(res.read() or b"{}"), token


def oauth_start(provider, options=None):
    try:
        raw, poll_token = oauth_device_start(provider, options)
        spec = read_oauth_profiles().get(provider, {})
        d = _envelope(raw, spec)
        url = str(d.get("verification_uri_complete") or d.get("verification_uri")
                  or d.get("verificationUrl")
                  or _dig(d, spec.get("verify_url_key", "")) or "")
        if url.startswith("/"):
            url = _origin(spec.get("authorize_url", "")) + url
        interval = d.get("interval", spec.get("poll_interval", 5))
        expires = d.get("expires_in", spec.get("expires_in", 600))
        if spec.get("expires_at_key"):
            ea = _dig(raw, spec["expires_at_key"])
            ea = _dig(raw, "data." + spec["expires_at_key"]) if ea is None else ea
            target = None
            if isinstance(ea, (int, float)):
                target = ea / 1000.0 if ea > 1e12 else ea
            elif isinstance(ea, str):
                try:
                    target = datetime.datetime.fromisoformat(
                        ea.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    target = None
            if target:
                expires = max(1, int(target - time.time()))
        out = {"device_code": d.get("device_code") or d.get("code")
               or d.get("flow_id") or "",
                "user_code": d.get("user_code") or d.get("code", ""),
                "url": url,
                "interval": interval, "expires": expires}
        if poll_token:
            out["poll_token"] = poll_token
        return out
    except Exception as e:
        raise RuntimeError("OAuth start failed for %s: %s" % (provider, e))


def _render_body(body, device_code):
    """Substitute {device_code} in static request bodies from a profile."""
    out = {}
    for k, v in (body or {}).items():
        out[k] = (v.replace("{device_code}", str(device_code))
                  if isinstance(v, str) else v)
    return out


def oauth_poll(provider, flow, cancel_event=None):
    """Poll the token endpoint until approval, honouring pending/400/202 shapes."""
    spec = read_oauth_profiles().get(provider, {})
    deadline = time.time() + min(360, float(flow.get("expires", 360)))
    poll_url = spec.get("token_url", "")
    method = spec.get("poll_method", "POST").upper()
    body = _render_body(spec.get("token_body") or
                        {"device_code": flow["device_code"]}, flow["device_code"])
    poll_token = flow.get("poll_token", "")
    token_key = spec.get("token_key", "access_token")
    status_key = spec.get("status_key", "")
    pending_status = spec.get("pending_status", "")
    failed_status = spec.get("failed_status", "")
    interval = float(flow.get("interval", 5) or 5)
    last_msg = ""
    while time.time() < deadline:
        if cancel_event is not None:
            if cancel_event.wait(interval):
                return None
        else:
            time.sleep(interval)
        status = 0
        d = {}
        try:
            if method == "GET":
                url = poll_url.rstrip("/") + "/" + urllib.parse.quote(
                    flow["device_code"], safe="")
                headers = {"User-Agent": UA, "Accept": "application/json"}
                if poll_token:
                    headers["Authorization"] = "Bearer " + poll_token
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as r:
                    status = r.status
                    d = json.loads(r.read() or b"{}")
            else:
                status, d = _post_json(poll_url, body, timeout=30)
        except Exception as e:
            last_msg = str(e)
            continue

        d = _envelope(d, spec)
        st = str(_dig(d, status_key) or "") if status_key else ""
        if status_key and pending_status and st == pending_status:
            last_msg = "authorization pending"
            continue
        if status_key and failed_status and st == failed_status:
            raise RuntimeError("authorization failed or cancelled")
        code = error_code(d)
        access = _dig(d, token_key) or d.get("access_token") or d.get("token")
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


API_PROVIDERS_PATH = os.path.join(MR_HOME, "api-providers.json")


def _raw_rows():
    try:
        with open(ROWS_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def _api_providers(data=None):
    if data is None:
        try:
            with open(API_PROVIDERS_PATH) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    os.makedirs(MR_HOME, exist_ok=True)
    tmp = API_PROVIDERS_PATH + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, API_PROVIDERS_PATH)
    return data


def _provider_id(row):
    explicit = row.get('provider_id') or row.get('provider')
    return 'api:' + str(explicit) if explicit else 'base:' + row['base'].rstrip('/')


def _provider_label(row):
    base = row['base'].rstrip('/')
    for pid, spec in read_provider_registry().items():
        if spec.get('base_url', '').rstrip('/') == base:
            return spec.get('label', pid)
    host = urllib.parse.urlsplit(base).hostname or base
    return (host[4:] if host.startswith('api.') else host).split('.')[0].title()


def provider_groups():
    groups = {}
    for pid, spec in _api_providers().items():
        groups['api:' + pid] = {'id': 'api:' + pid, 'label': spec.get('label', pid),
                               'base': spec['base'], 'kind': 'API', 'rows': []}
    for row in _raw_rows():
        pid = _provider_id(row)
        group = groups.setdefault(pid, {'id': pid, 'label': row.get('provider_label') or
                                  row.get('provider_id') or row.get('provider') or
                                  _provider_label(row), 'base': row['base'],
                                  'kind': 'API', 'rows': []})
        group['rows'].append({'name': row['name'], 'model': row['model'],
                              'state': dict(ROW_STATE.get(row['name'], {}))})
    auth = read_oauth()
    for pid, spec in read_oauth_profiles().items():
        rows = []
        for item in spec.get('models', []):
            model = item if isinstance(item, str) else item.get('model', '')
            name = '%s-%s' % (pid, model.replace('/', '-'))
            rows.append({'name': name, 'model': model, 'state': dict(ROW_STATE.get(name, {}))})
        rec = auth.get(pid) or {}
        connected = bool(rec.get('access_token'))
        state = 'connected' if connected else 'login required'
        if connected and rec.get('expires_at') and rec['expires_at'] < time.time():
            state = 'expired / refresh on use' if spec.get('refresh_url') else 'login expired'
        groups['oauth:' + pid] = {'id': 'oauth:' + pid, 'label': spec.get('label', pid),
                                  'base': spec['base_url'], 'kind': 'OAuth',
                                  'connection': state, 'rows': rows}
    registry = read_provider_registry()
    for group in groups.values():
        if group['kind'] == 'API':
            for pid, spec in registry.items():
                if spec.get('no_auth') and spec.get('base_url', '').rstrip('/') == group['base'].rstrip('/'):
                    group['connection'] = 'No auth required'
                if (spec.get('base_url', '').rstrip('/') == group['base'].rstrip('/')
                        and spec.get('authorize_url') and spec.get('token_url')):
                    group['oauth_available'] = pid
                    group['connection'] = 'OAuth available / current API or keyless connection; L to log in'
                    break
    for group in list(groups.values()):
        target = groups.get('oauth:' + group.get('oauth_available', ''))
        if group['kind'] == 'API' and target:
            target.setdefault('api_groups', []).append(dict(group))
            for row in group['rows']:
                target['rows'].append(dict(row, owner=group))
            groups.pop(group['id'])
    return list(groups.values())


def _save_api_provider(label, base, key):
    u = urllib.parse.urlsplit(base)
    if u.scheme not in ('https', 'http') or not u.netloc or u.username or u.password or u.query:
        raise ValueError('Use an HTTP(S) base URL without credentials or query parameters')
    pid = uuid.uuid4().hex
    with ROW_LOCK:
        providers = _api_providers()
        providers[pid] = {'label': label, 'base': base.rstrip('/'), 'key': key}
        _api_providers(providers)
    return 'api:' + pid


def _provider_template(group):
    pid = group['id']
    if pid.startswith('api:'):
        spec = _api_providers().get(pid[4:])
        if spec:
            return {'base': spec['base'], 'key': spec.get('key', ''),
                    'provider_id': pid[4:], 'provider_label': spec.get('label', pid[4:])}
    return dict(next(r for r in _raw_rows() if _provider_id(r) == pid))


def _model_change(group, model, remove=False):
    with ROW_LOCK:
        if group['kind'] == 'OAuth':
            profiles = read_oauth_profiles()
            spec = profiles[group['id'][6:]]
            models = spec.setdefault('models', [])
            ids = [x if isinstance(x, str) else x.get('model') for x in models]
            if remove:
                spec['models'] = [x for x, mid in zip(models, ids) if mid != model]
            elif model not in ids:
                models.append(model)
            write_oauth_profiles(profiles)
        else:
            rows = _raw_rows()
            if remove:
                rows = [r for r in rows if not (_provider_id(r) == group['id'] and r['model'] == model)]
            elif not any(_provider_id(r) == group['id'] and r['model'] == model for r in rows):
                row = _provider_template(group)
                row.pop('aliases', None)
                stem = group.get('label') or row.get('provider_label') or 'provider'
                stem = '-'.join(stem.lower().split()) + '-' + model.replace('/', '-')
                name = stem
                suffix = 2
                while any(r['name'] == name for r in rows):
                    name = '%s-%d' % (stem, suffix)
                    suffix += 1
                row.update(name=name, model=model)
                rows.append(row)
            write_rows(rows)


def _delete_provider(group):
    for child in group.get('api_groups', []):
        _delete_provider(child)
    with ROW_LOCK:
        if group['kind'] == 'OAuth':
            pid = group['id'][6:]
            profiles = read_oauth_profiles()
            profiles.pop(pid, None)
            write_oauth_profiles(profiles)
            auth = read_oauth()
            auth.pop(pid, None)
            write_oauth(auth)
        else:
            write_rows([r for r in _raw_rows() if _provider_id(r) != group['id']])
            providers = _api_providers()
            providers.pop(group['id'][4:], None)
            _api_providers(providers)


def _input_line(stdscr, label, secret=False, default=''):
    buf = default
    stdscr.timeout(100)
    while True:
        h, w = stdscr.getmaxyx()
        shown = '*' * len(buf) if secret else buf
        _putw(stdscr, h - 2, ' ' * (w - 1), w)
        _putw(stdscr, h - 2, label + ' ' + shown, w, curses.A_BOLD)
        _putw(stdscr, h - 1, 'Enter save / Esc cancel / Ctrl-U clear'.ljust(w - 1), w, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return buf.strip()
        if ch == 27:
            return None
        if ch == 21:
            buf = ''
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
        elif 32 <= ch < 127:
            buf += chr(ch)


def _pick_list(stdscr, items, title):
    idx, flt = 0, ''
    stdscr.timeout(100)
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        shown = [m for m in items if flt.lower() in m.lower()]
        idx = min(idx, max(0, len(shown) - 1))
        compact = h < 9
        start = 2 if compact else 4
        room = max(1, h - start - 2)
        top = max(0, idx - room + 1)
        _putw(stdscr, 0, '  ' + title.upper(), w, curses.A_BOLD)
        if not compact:
            _putw(stdscr, 1, '  Choose an option below. Type to filter.', w, curses.A_DIM)
            _putw(stdscr, 2, '  ' + '─' * max(0, w - 5), w, curses.A_DIM)
            if flt:
                _putw(stdscr, 3, '  Filter: ' + flt, w)
        for i, text in enumerate(shown[top:top + room]):
            selected = top + i == idx
            line = ('  ▶ ' if selected else '    ') + text
            _putw(stdscr, start + i, line.ljust(max(0, w - 2)), w,
                  curses.A_REVERSE | curses.A_BOLD if selected else 0)
        if not shown:
            _putw(stdscr, start, '  No matches. BACKSPACE edits the filter.', w, curses.A_DIM)
        _putw(stdscr, h - 2, '  %d of %d options%s' %
              (idx + 1 if shown else 0, len(shown), ' | Filter: ' + flt if compact and flt else ''), w, curses.A_DIM)
        _putw(stdscr, h - 1, '  ↑↓ select | ENTER continue | ESC back | Type to filter', w, curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return shown[idx] if shown else None
        if ch == 27:
            return None
        if ch in (curses.KEY_DOWN, curses.KEY_NPAGE):
            idx = min(idx + (room if ch == curses.KEY_NPAGE else 1), max(0, len(shown) - 1))
        elif ch in (curses.KEY_UP, curses.KEY_PPAGE):
            idx = max(0, idx - (room if ch == curses.KEY_PPAGE else 1))
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            flt, idx = flt[:-1], 0
        elif 32 <= ch < 127:
            flt, idx = flt + chr(ch), 0


def _open_browser(url):
    try:
        subprocess.Popen(
            [sys.executable, '-c', 'import sys, webbrowser; webbrowser.open(sys.argv[1])', url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def _work(stdscr, title, fn, lines=(), browser_url='', cancel=None):
    done = Event()
    result = {}
    def run():
        try:
            result['value'] = fn()
        except Exception as exc:
            result['error'] = exc
        finally:
            done.set()
    Thread(target=run, daemon=True).start()
    offset = 0
    stdscr.timeout(100)
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        _putw(stdscr, 0, title, w, curses.A_REVERSE)
        wrapped = []
        for line in lines:
            width = max(1, w - 1)
            wrapped.extend(str(line)[i:i + width] for i in range(0, len(str(line)), width))
        for i, line in enumerate(wrapped[offset:offset + max(1, h - 3)]):
            _putw(stdscr, i + 1, line, w)
        _putw(stdscr, h - 1, 'Esc cancel / arrows scroll' + (' / o open browser' if browser_url else ''), w)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (27, ord('q')):
            if cancel:
                cancel.set()
            return None
        if ch == curses.KEY_DOWN:
            offset = min(offset + 1, max(0, len(wrapped) - 1))
        elif ch == curses.KEY_UP:
            offset = max(0, offset - 1)
        elif ch == ord('o') and browser_url:
            _open_browser(browser_url)
        if done.is_set():
            if 'error' in result:
                raise result['error']
            return result.get('value')


def _login_flow(stdscr, group):
    pid = group['id'][6:]
    try:
        options = {}
        field = read_oauth_profiles().get(pid, {}).get('referral_field')
        if field:
            referral = _input_line(stdscr, 'Referral code (optional, ENTER skips):')
            if referral is None:
                return 'Login cancelled'
            if referral:
                options[field] = referral
        flow = _work(stdscr, 'Starting OAuth login', lambda: oauth_start(pid, options))
        if flow is None:
            return 'Login cancelled'
        url = flow.get('url', '')
        if not flow.get('device_code') or urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
            return 'Login failed: provider did not return a valid verification URL and device code'
        _open_browser(url)
        cancel = Event()
        token = _work(stdscr, 'Waiting for browser approval (up to 6 minutes; Esc cancels)',
                      lambda: oauth_poll(pid, flow, cancel_event=cancel),
                      ['Open this URL:', url, 'Code: ' + str(flow.get('user_code', ''))], url, cancel)
        if token is None:
            return 'Login cancelled; no tokens saved'
        with ROW_LOCK:
            if pid not in read_oauth_profiles():
                return 'Profile removed; login discarded'
            auth = read_oauth()
            auth[pid] = token
            write_oauth(auth)
        return 'OAuth connected'
    except Exception:
        return 'OAuth login failed or expired; retry with l'


def _setup_oauth(stdscr, pid, spec):
    with ROW_LOCK:
        profiles = read_oauth_profiles()
        created = pid not in profiles
        if created:
            profiles[pid] = spec
            write_oauth_profiles(profiles)
    result = 'Login cancelled'
    try:
        result = _login_flow(stdscr, {'id': 'oauth:' + pid, 'kind': 'OAuth'})
        return ('OAuth provider saved. ' if created and result == 'OAuth connected' else '') + result
    finally:
        if created and result != 'OAuth connected':
            with ROW_LOCK:
                profiles = read_oauth_profiles()
                if profiles.get(pid) == spec:
                    profiles.pop(pid, None)
                    write_oauth_profiles(profiles)


def _add_flow(stdscr):
    kind = _pick_list(stdscr, ['API key provider', 'OAuth provider', 'No-auth provider'], 'Add provider')
    if kind is None:
        return 'Add cancelled'
    if kind.startswith('API') or kind == 'No-auth provider':
        presets = {spec.get('label', pid): spec for pid, spec in read_provider_registry().items()
                   if (spec.get('no_auth') if kind == 'No-auth provider' else
                       not spec.get('no_auth') and (spec.get('api_key_supported') or not spec.get('authorize_url')))}
        selected = _pick_list(stdscr, sorted(presets) + ['Custom API provider'], 'API provider')
        if selected is None:
            return 'Add cancelled'
        preset = presets.get(selected)
        if preset:
            label, base = selected, preset['base_url']
        else:
            label = _input_line(stdscr, 'Provider name:')
            if not label:
                return 'Add cancelled'
            base = _input_line(stdscr, 'Base URL (https://host/v1):')
            if not base:
                return 'Add cancelled'
        key = '' if preset and preset.get('no_auth') else _input_line(stdscr, 'API key (blank for keyless):', secret=True)
        if key is None:
            return 'Add cancelled'
        pid = _save_api_provider(label, base, key)
        if preset:
            group = {'id': pid, 'kind': 'API', 'base': base}
            for item in preset.get('models', []):
                model = item if isinstance(item, str) else item.get('model', '')
                if model:
                    _model_change(group, model)
        return 'Provider saved; Enter opens models, a adds models'
    registry = read_provider_registry()
    options = {pid + ' - ' + spec.get('label', pid): (pid, spec)
               for pid, spec in registry.items() if spec.get('authorize_url') and spec.get('token_url')}
    selected = _pick_list(stdscr, list(options) + ['Custom OAuth'], 'OAuth provider')
    if not selected:
        return 'Add cancelled'
    seed, entry = options.get(selected, ('', {}))
    pid = seed or _input_line(stdscr, 'Profile ID (letters, digits, - or _):')
    if not pid:
        return 'Add cancelled'
    if not pid.replace('-', '').replace('_', '').isalnum():
        return 'Invalid profile ID'
    if pid in read_oauth_profiles():
        return _login_flow(stdscr, {'id': 'oauth:' + pid, 'kind': 'OAuth'})
    spec = json.loads(json.dumps(entry))
    if not entry:
        for key, label in [('label', 'Display name:'), ('base_url', 'Chat base URL:'),
                           ('authorize_url', 'Device authorization URL:'),
                           ('token_url', 'Token polling URL:'), ('refresh_url', 'Refresh URL (optional):')]:
            value = _input_line(stdscr, label)
            if value is None or (not value and key != 'refresh_url'):
                return 'Add cancelled'
            if key.endswith('url') and value:
                parsed = urllib.parse.urlsplit(value)
                if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username:
                    return 'Invalid HTTP(S) URL'
            spec[key] = value
        client = _input_line(stdscr, 'OAuth client ID (optional):')
        if client is None:
            return 'Add cancelled'
        spec['authorize_body'] = {'client_id': client} if client else {}
        spec['token_body'] = {'device_code': '{device_code}', 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'}
        if client:
            spec['token_body']['client_id'] = client
        spec['api_mode'] = _pick_list(stdscr, ['chat_completions', 'responses', 'anthropic'], 'Upstream format')
        if not spec['api_mode']:
            return 'Add cancelled'
        spec['poll_method'] = _pick_list(stdscr, ['POST', 'GET'], 'Token polling method')
        if not spec['poll_method']:
            return 'Add cancelled'
        models = _input_line(stdscr, 'Model IDs, comma separated (optional):')
        if models is None:
            return 'Add cancelled'
        spec['models'] = list(dict.fromkeys(x.strip() for x in models.split(',') if x.strip()))
    return _setup_oauth(stdscr, pid, spec)


def _add_model_flow(stdscr, group):
    choice = _pick_list(stdscr, ['Enter model ID', 'Fetch model list'], 'Add model')
    if choice is None:
        return 'Add cancelled'
    if choice == 'Fetch model list':
        def fetch():
            if group['kind'] == 'OAuth':
                pid = group['id'][6:]
                spec = read_oauth_profiles()[pid]
                curated = read_provider_registry().get(pid, {}).get('models', [])
                if curated:
                    return [x if isinstance(x, str) else x['model'] for x in curated]
                token = oauth_access_token(pid)
                row = {'oauth_provider': pid}
                response = upstream(group['base'], token, '/v1/models', row=row, timeout=PROBE_TIMEOUT)
                body = json.loads(response.read())
                data = body if isinstance(body, list) else body.get('data', [])
                return [x if isinstance(x, str) else x['id'] for x in data]
            row = _provider_template(group)
            key = row.get('key') or ENV.get(row.get('key_env', ''), '')
            return fetch_models(row['base'], key)
        try:
            ids = _work(stdscr, 'Fetching models', fetch)
        except Exception:
            ids = []
        if ids is None:
            return 'Fetch cancelled'
        if ids:
            model = _pick_list(stdscr, ids, 'Select model')
        else:
            model = _input_line(stdscr, 'No model list available; enter model ID:')
    else:
        model = _input_line(stdscr, 'Model ID:')
    if not model:
        return 'Add cancelled'
    _model_change(group, model)
    return 'Model saved; t tests it (quota failures do not remove it)'


def _test_model(group, model):
    if group['kind'] == 'OAuth':
        row = next(r for r in oauth_rows() if r['oauth_provider'] == group['id'][6:] and r['model'] == model)
    else:
        row = next(r for r in read_rows() if _provider_id(r) == group['id'] and r['model'] == model)
    ok, ms, error, content = probe(row['base'], row.get('key', ''), model, row=row)
    ROW_STATE[row['name']] = {'status': 'ok' if ok else 'fail', 'ms': ms, 'at': time.time()}
    return 'Test OK (%dms)' % ms if ok else 'Test failed; check provider access, quota or model ID'


QUOTA_STATE = {}
QUOTA_LOCK = Lock()


def _quota_spec(group):
    if not group or group['kind'] != 'OAuth':
        return None
    pid = group['id'][6:]
    profile = read_oauth_profiles().get(pid, {})
    if profile.get('quota'):
        return profile['quota']
    for spec in read_provider_registry().values():
        if spec.get('base_url', '').rstrip('/') == group['base'].rstrip('/'):
            return spec.get('quota')
    return None


def _quota_refresh(group):
    spec = _quota_spec(group)
    if not spec:
        return
    key = group['id']
    with QUOTA_LOCK:
        previous = QUOTA_STATE.get(key, {})
        if previous.get('loading'):
            return
        QUOTA_STATE[key] = dict(previous, loading=True, error='')
    def fetch():
        try:
            token = oauth_access_token(key[6:])
            if not token:
                raise ValueError('Login required')
            headers = {k: str(v).replace('{uuid}', str(uuid.uuid4()))
                       for k, v in spec.get('headers', {}).items()}
            headers['Authorization'] = 'Bearer ' + token
            req = urllib.request.Request(spec['url'], headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read())
            details = []
            if spec.get('format') == 'weekly_budgets':
                allowance = payload.get('allowance') or {}
                budgets = payload.get('budgets') or allowance.get('budgets') or {}
                reset = allowance.get('resetsAt')
                if isinstance(reset, str) and reset:
                    reset = datetime.datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp()
                items = []
                for family, budget in budgets.items():
                    cap = budget.get('usdPerWeek')
                    used = budget.get('spentUsd', 0)
                    items.append({'show_name': 'GPT-6' if family == 'gpt6' else family.title(),
                                  'total_units': cap or 0, 'used_units': used,
                                  'period_end': reset, 'unit': 'USD', 'unlimited': cap is None,
                                  'usage_tokens': budget.get('tokensThisWeek')})
                if not items and 'tokensPerWeek' in allowance:
                    items.append({'show_name': 'Weekly allowance', 'total_units': allowance['tokensPerWeek'],
                                  'used_units': allowance.get('usedThisWeek', 0), 'period_end': reset})
                if not items:
                    raise ValueError('No weekly budget available')
                details.append('Plan: ' + str(payload.get('plan', 'unknown')).title())
                referral = payload.get('referral') or {}
                if referral:
                    details.append('Invite bonus: +%s / +%s tokens weekly | %s joined, %s active' % (
                        format(referral.get('bonusTokensPerWeek', 0), ',.0f'),
                        format(referral.get('bonusCapPerWeek', 0), ',.0f'),
                        referral.get('joined', 0), referral.get('activeThisWeek', 0)))
                payload = {'code': 0, 'data': {'balances': items}}
            if spec.get('format') == 'shared_daily':
                if not isinstance(payload.get('freeTokenLimit'), (int, float)) or not isinstance(payload.get('freeTokensToday'), (int, float)):
                    raise ValueError('Missing shared quota balance')
                reset = (payload.get('resetTimes') or {}).get('dailyFree')
                if isinstance(reset, str):
                    reset = datetime.datetime.fromisoformat(reset.replace('Z', '+00:00')).timestamp()
                payload = {'code': 0, 'data': {'balances': [{
                    'show_name': 'Daily Free Tokens (shared)',
                    'total_units': payload['freeTokenLimit'],
                    'used_units': payload['freeTokensToday'],
                    'period_end': reset}]}}
            if payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
                raise ValueError('Quota unavailable; check login or plan')
            balances = []
            for item in payload['data'].get('balances', []):
                total = float(item.get('total_units') or 0)
                used = float(item.get('used_units') or 0)
                remaining = float(item.get('remaining_units', max(0, total - used)))
                balances.append({'name': item.get('show_name') or item.get('entitlement_id') or 'Model',
                                 'total': total, 'remaining': remaining,
                                 'percent': min(100, max(0, remaining / total * 100)) if total else 0,
                                 'reset': item.get('period_end'), 'unit': item.get('unit', 'tokens'),
                                 'used': used, 'unlimited': item.get('unlimited', False),
                                 'usage_tokens': item.get('usage_tokens')})
            with QUOTA_LOCK:
                QUOTA_STATE[key] = {'loading': False, 'at': time.time(), 'balances': balances, 'details': details}
        except urllib.error.HTTPError as exc:
            message = 'Quota request failed (HTTP %d); R retry' % exc.code
            if exc.code == 401:
                message = 'Quota authorization rejected (401); L login / R retry'
            elif exc.code == 403:
                message = 'Quota access blocked by provider (403); R retry'
            with QUOTA_LOCK:
                QUOTA_STATE[key] = dict(previous, loading=False, error=message)
        except Exception:
            with QUOTA_LOCK:
                QUOTA_STATE[key] = dict(previous, loading=False, error='Quota fetch failed; R retry')
    Thread(target=fetch, daemon=True).start()


def _draw_dashboard(stdscr, port, groups, group, entries, sel, status, started, G, COLOR):
    LABEL = curses.A_DIM
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    rows = []
    for item in entries:
        states = [r.get("state", {}) for r in item.get("rows", [])]
        state = item.get("state") or max(states, key=lambda x: x.get("at", 0), default={})
        rows.append({"name": item["model"] if group else item["label"],
                     "model": item["name"] if group else "%d %s / %s" % (len(item["rows"]), "model" if len(item["rows"]) == 1 else "models", 'API+OAuth' if item.get('oauth_available') else item["kind"]),
                     "model_id": item.get("model", item.get("label", "")), "row_name": item.get("name", item.get("label", "")),
                     "base": group["base"] if group else item["base"],
                     "state": state, "key_src": (group or item).get("connection", "API provider")})
    up = int(time.time() - started)
    head = " MINIMALROUTER  |  http://%s:%d/v1  |  %d providers  %d models  |  up %dm%02ds  |  no server auth / localhost only" % (HOST, port, len(groups), sum(len(g["rows"]) for g in groups), up // 60, up % 60)
    _putw(stdscr, 0, head, w, curses.A_REVERSE | curses.A_BOLD)
    if h < 16 or w < 60:
        room = max(1, h - 4)
        top = max(0, sel - room + 1)
        _putw(stdscr, 1, "PROVIDERS > " + group["label"] if group else "PROVIDERS", w, curses.A_BOLD)
        for i, row in enumerate(rows[top:top + room]):
            text = row.get("model_id", row["model"]) if group else row["name"]
            _putw(stdscr, i + 2, " " + text, w, curses.A_REVERSE if top + i == sel else 0)
        _putw(stdscr, h - 2, status, w)
        _putw(stdscr, h - 1, "Enter models / a add / l login / Esc back / q quit", w)
        stdscr.refresh()
        return
    HAS_COLOR = bool(COLOR.get("ok"))
    name_w = max(26, min(44, w // 3)) if group else (26 if w >= 110 else (20 if w >= 92 else 15))
    rest = w - (4 + name_w + 8 + 6 + 8)   # space left for model+base
    model_w = max(16, int(rest * 0.62))
    base_w = max(10, rest - model_w - 1)
    cols = "   %-*s %-*s %-*s %7s %5s" % (name_w, "MODEL" if group else "PROVIDER", model_w,
                                             "ROW" if group else "MODELS / AUTH", base_w, "HOST",
                                         "LAT", "LAST")
    _putw(stdscr, 1, cols, w, curses.A_BOLD)
    _panel(stdscr, 2, w, G, "PROVIDERS > " + group["label"] if group else "PROVIDERS")

    detail_h = 7
    quota = QUOTA_STATE.get(group['id'], {}) if group and _quota_spec(group) else {}
    quota_height = 2 + len(quota.get('details', [])) + 3 * len(quota.get('balances', [])) if group and _quota_spec(group) else 4
    table_rows = min(len(rows), max(1, h - (3 + detail_h + 2 + quota_height)))
    top = max(0, sel - table_rows + 1) if sel >= table_rows else 0
    y = 3
    for i, row in enumerate(rows[top:top + table_rows]):
        idx = i + top
        s = row.get("state", {}) or {}
        stt = s.get("status", "unknown")
        lat = ("%dms" % int(s.get("ms") or 0)) if stt != "unknown" else "-"
        host = urllib.parse.urlparse(row["base"]).netloc[:base_w]
        mark = {"ok": G["ok"], "fail": G["bad"]}.get(stt, G["unk"])
        line = " %s %s %-*s %-*s %-*s %7s %5s" % (
            G["sel"] if idx == sel else " ", mark,
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

    _panel(stdscr, y, w, G, "SELECTED")
    y += 1
    if rows:
        row = rows[sel]
        s = row.get("state", {}) or {}
        stt = s.get("status", "unknown")
        if group:
            pairs = [
                ("model", row.get("model_id", row["model"])),
                ("row", row.get("row_name", row["name"])),
                ("base", row["base"]),
                ("key", row.get("key_src", "-")),
            ]
        else:
            pairs = [
                ("name", row["name"]),
                ("models", "%d / %s" % (len(entries[sel]["rows"]), "API+OAuth" if entries[sel].get("oauth_available") else entries[sel]["kind"])),
                ("base", row["base"]),
                ("auth", row.get("key_src", "-")),
            ]
        pairs += [
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

    if group and _quota_spec(group):
        _panel(stdscr, y, w, G, "QUOTA  [R refresh]")
        y += 1
        snapshot = QUOTA_STATE.get(group['id'], {})
        message = ('Refreshing...' if snapshot.get('loading') else snapshot.get('error') or
                   'Updated ' + _age(snapshot.get('at')) if snapshot else 'R refresh quota')
        for detail in snapshot.get('details', []):
            _putw(stdscr, y, '  ' + detail, w, LABEL)
            y += 1
        _putw(stdscr, y, '  ' + message, w, LABEL)
        y += 1
        balances = snapshot.get('balances', [])
        detailed = h - 2 - y >= len(balances) * 3
        for balance in balances:
            if y >= h - 2:
                break
            pct = max(0, min(100, balance['percent']))
            blocks = min(24, max(8, w // 5))
            filled = round(pct * blocks / 100)
            bar = '█' * filled + '░' * (blocks - filled)
            color = COLOR['ok'] if pct > 20 else COLOR.get('warn', COLOR['fail']) if pct > 0 else COLOR['fail']
            remaining = format(balance['remaining'], ',.0f')
            total = format(balance['total'], ',.0f')
            used = format(max(0, balance['total'] - balance['remaining']), ',.0f')
            unit = balance.get('unit', 'tokens')
            if unit == 'USD':
                remaining = '$%.2f' % balance['remaining']
                total = 'Unlimited' if balance.get('unlimited') else '$%.2f' % balance['total']
                used = '$%.2f' % balance.get('used', 0)
            usage_tokens = balance.get('usage_tokens')
            usage_text = (' | ' + format(usage_tokens, ',.0f') + ' tokens') if usage_tokens is not None else ''
            reset = time.strftime('%b %d %H:%M %Z', time.localtime(balance['reset'])) if balance.get('reset') else 'Unknown'
            if detailed:
                _putw(stdscr, y, '  ' + balance['name'], w, curses.A_BOLD)
                _putw(stdscr, y + 1, '  %s  %5.1f%% remaining  |  %s %s left%s' % (bar, pct, remaining, unit, usage_text), w, color)
                _putw(stdscr, y + 2, '  Used %s / %s %s  |  Resets %s' % (used, total, unit, reset), w, LABEL)
                y += 3
            else:
                text = '  %s  %s  %.0f%%  |  %s / %s left' % (balance['name'], bar, pct, remaining, total)
                _putw(stdscr, y, text, w, color)
                y += 1
    if not group:
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

    _putw(stdscr, h - 2, " " + status, w, curses.A_BOLD)
    _putw(stdscr, h - 1,
          (" ↑↓ select | A add | L login | T test | D delete | R refresh | ESC back | Q quit"
           if group else
           " ↑↓ select | ENTER models | A add | L login | D delete | R refresh | Q quit"), w, curses.A_DIM)
    stdscr.refresh()



def _curses_main(stdscr, port):
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        HAS_COLOR = True
    except Exception:
        HAS_COLOR = False
    try:
        curses.curs_set(0)
    except Exception:
        pass
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
    COLOR = {"ok": curses.color_pair(1) if HAS_COLOR else 0,
             "fail": curses.color_pair(2) if HAS_COLOR else 0}
    LABEL = curses.A_DIM
    started = time.time()
    stdscr.keypad(True)
    stdscr.timeout(200)
    selected, model_sel, current = 0, 0, None
    status = 'Enter opens provider models; a adds a provider'
    while True:
        try:
            groups = provider_groups()
        except Exception:
            groups = []
            status = 'Could not read provider configuration'
        selected = min(selected, max(0, len(groups) - 1))
        group = next((g for g in groups if g['id'] == current), None)
        if current and group is None:
            current = None
        entries = group['rows'] if group else groups
        model_sel = min(model_sel, max(0, len(entries) - 1))
        idx = model_sel if group else selected
        _draw_dashboard(stdscr, port, groups, group, entries, idx, status, started, G, COLOR)
        ch = stdscr.getch()
        if ord('A') <= ch <= ord('Z'):
            ch = ord(chr(ch).lower())
        if ch in (ord('q'), ord('Q')):
            return
        if ch in (27, curses.KEY_LEFT, curses.KEY_BACKSPACE, 127):
            current = None
        elif ch in (curses.KEY_DOWN, ord('j')):
            if group: model_sel = min(idx + 1, max(0, len(entries) - 1))
            else: selected = min(idx + 1, max(0, len(entries) - 1))
        elif ch in (curses.KEY_UP, ord('k')):
            if group: model_sel = max(0, idx - 1)
            else: selected = max(0, idx - 1)
        else:
            try:
                if ch in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT) and not group and groups:
                    current, model_sel = groups[selected]['id'], 0
                    if _quota_spec(groups[selected]):
                        cached = QUOTA_STATE.get(current, {})
                        if time.time() - cached.get('at', 0) > 30:
                            _quota_refresh(groups[selected])
                    status = 'a add model / t test / d remove model / Esc providers'
                elif ch == ord('a'):
                    status = _add_model_flow(stdscr, group) if group else _add_flow(stdscr)
                elif ch == ord('l'):
                    active = group or (groups[selected] if groups else None)
                    if active and active.get('oauth_available'):
                        pid = active['oauth_available']
                        spec = json.loads(json.dumps(read_provider_registry()[pid]))
                        status = _setup_oauth(stdscr, pid, spec)
                    else:
                        status = _login_flow(stdscr, active) if active and active['kind'] == 'OAuth' else 'No OAuth endpoints configured for this provider'
                elif ch == ord('t') and group and entries:
                    item = entries[idx]
                    owner = item.get('owner', group)
                    status = _work(stdscr, 'Testing model', lambda: _test_model(owner, item['model'])) or 'Test cancelled'
                elif ch == ord('d') and entries:
                    target = entries[idx]['model'] if group else entries[idx]['label']
                    answer = _input_line(stdscr, 'Delete %s%s? Type yes:' % (target, '' if group else ' and all models / saved credentials'))
                    if answer == 'yes':
                        if group: _model_change(entries[idx].get('owner', group), target, remove=True)
                        else: _delete_provider(entries[idx])
                        status = 'Deleted'
                    else:
                        status = 'Delete cancelled'
                elif ch == ord('r'):
                    if group and _quota_spec(group):
                        _quota_refresh(group)
                        status = 'Refreshing quota...'
                    else:
                        status = 'Refreshed'
            except ValueError as exc:
                status = str(exc) if str(exc).startswith('Use an HTTP') else 'Invalid input'
            except Exception:
                status = 'Action failed; check configuration and retry'
        stdscr.timeout(200)



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
  minimalrouter -h|--help    show this help
  minimalrouter -v           show version

Endpoint: http://127.0.0.1:%d/v1     (env MINIMALROUTER_PORT overrides the port)
No server authentication. Localhost only. OAuth provider logins are supported
through profiles; the client-facing format is OpenAI Chat Completions.

Dashboard keys:
  up/down  select provider/model   Enter  open provider models
  a        add provider/model      l      OAuth login (o opens browser)
  t        test selected model     d      delete with confirmation
  Esc      back/cancel             q      quit (stops the server)

Run `minimalrouter`, press a to add an API or OAuth provider. Enter opens
its models; a adds models manually or from discovery. Testing is optional.

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
                print("%-14s %-24s %-12s models: %s"
                      % (pid, spec.get("label", pid), state,
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
            for k in ("referral_field", "refresh_body", "quota", "token_body", "authorize_body", "headers", "envelope",
                      "client_poll_token", "verify_url_key", "expires_at_key",
                      "token_key", "status_key", "pending_status",
                      "failed_status", "poll_interval", "expires_in",
                      "system_blocks", "model_map", "user_metadata",
                      "request_headers", "append_client_system"):
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
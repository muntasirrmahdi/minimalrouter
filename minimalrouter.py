#!/usr/bin/env python3
"""MinimalRouter — one clean OpenAI-compatible endpoint over N free API rows.

Single file, stdlib only. No auth, no web UI. Binds 127.0.0.1 only.
Terminal dashboard (curses) in the same process.

Usage:
  minimalrouter          server + terminal dashboard
  minimalrouter serve    server only (for tmux / systemd)

Home:   ~/.minimalrouter/   (minimalrouter.py, rows.json, minimalrouter.log)
Rows:   [{name, base, model, key_env|key, aliases?}]
Keys:   key_env rows resolve from ~/.hermes/.env at runtime.

HTTP API:
  POST /v1/chat/completions   stream + non-stream, ordered fallback per model
  GET  /v1/models             union of configured models/aliases
  GET  /health
  POST /api/test              {base, key, model} -> probe result (no save)
  POST /api/fetch_models      {base, key} -> upstream model ids
  POST /api/add               {base, key, model, name?} -> test, then save
  POST /api/remove            {name} -> drop row
  GET  /api/state             rows + live per-row state
"""
import json
import os
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


def read_oauth_profiles():
    """Load generic provider profiles; no provider is bundled by default."""
    try:
        with open(OAUTH_PROFILES_PATH) as f:
            data = json.load(f)
        profiles = data.get("profiles", data) if isinstance(data, dict) else {}
        return profiles if isinstance(profiles, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


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


def upstream(base, key, path, payload=None, timeout=TIMEOUT, row=None):
    # Per-profile behavior only; no provider is known by name.
    row = row or {}
    provider = row.get("oauth_provider", "")
    api_mode = row.get("api_mode", "chat_completions")
    if api_mode == "responses" and path == "/v1/chat/completions":
        return oauth_chat(row, payload, timeout)
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
    res = oauth_post(spec["authorize_url"], provider, "", body)
    return json.loads(res.read() or b"{}")


def oauth_start(provider):
    try:
        d = oauth_device_start(provider)
        return {"device_code": d.get("device_code") or d.get("code"),
                "user_code": d.get("user_code") or d.get("code", ""),
                "url": d.get("verification_uri_complete") or d.get("verification_uri") or d.get("verificationUrl"),
                "interval": d.get("interval", 5), "expires": d.get("expires_in", 600)}
    except Exception as e:
        raise RuntimeError("OAuth start failed for %s: %s" % (provider, e))


def oauth_poll(provider, flow):
    spec = read_oauth_profiles().get(provider, {})
    deadline = time.time() + flow["expires"]
    poll_url = spec.get("token_url", "")
    method = spec.get("poll_method", "POST").upper()
    while time.time() < deadline:
        time.sleep(flow["interval"])
        try:
            payload = {"device_code": flow["device_code"]}
            if method == "GET":
                url = poll_url.rstrip("/") + "/" + urllib.parse.quote(flow["device_code"], safe="")
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                res = urllib.request.urlopen(req, timeout=30)
            else:
                res = oauth_post(poll_url, provider, "", payload, timeout=30)
            d = json.loads(res.read() or b"{}")
            access = d.get("access_token") or d.get("token")
            if access:
                out = {"access_token": access,
                       "refresh_token": d.get("refresh_token", "")}
                if d.get("org_id"):
                    out["org_id"] = d["org_id"]
                return out
            if d.get("error") not in ("authorization_pending", "slow_down") and d.get("status") not in ("pending", "authorization_pending"):
                raise RuntimeError(d.get("error_description", d.get("error", "OAuth failed")))
        except urllib.error.HTTPError as e:
            if e.code in (202, 403, 410):
                if e.code == 403: raise RuntimeError("OAuth denied")
                continue
            if e.code >= 500: continue
            raise
    raise RuntimeError("OAuth expired before approval")


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
                    "no auth / localhost only" if online
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
        rest = w - (4 + name_w + 8 + 6 + 8)   # space left for model+base
        model_w = max(16, int(rest * 0.62))
        base_w = max(10, rest - model_w - 1)
        cols = "   %-*s %-*s %-*s %7s %5s" % (name_w, "NAME", model_w,
                                             "MODEL", base_w, "HOST",
                                             "LAT", "LAST")
        _putw(stdscr, 1, cols, w, curses.A_BOLD)
        _panel(stdscr, 2, w, G, "")

        # ---- layout budget ---------------------------------------------
        detail_h = 7
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

        # ---- selected row details --------------------------------------
        _panel(stdscr, y, w, G, "SELECTED")
        y += 1
        if rows:
            row = rows[sel]
            s = row.get("state", {}) or {}
            stt = s.get("status", "unknown")
            pairs = [
                ("name", row["name"]),
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


def run_server():
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


class _Srv(ThreadingHTTPServer):
    pass


def usage():
    return """MinimalRouter - one OpenAI-compatible endpoint over free API rows.

Usage:
  minimalrouter              terminal dashboard + server (one process)
  minimalrouter serve        server only (tmux / systemd / background)
  minimalrouter -h|--help    show this help
  minimalrouter -v           show version

Endpoint: http://127.0.0.1:%d/v1     (env MINIMALROUTER_PORT overrides the port)
No auth. Localhost only. OpenAI format only (/v1/messages is not served).

Dashboard keys:
  up/down  select row     t  re-test row      a  add a row
  d        delete row     r  refresh          q  quit (stops the server)
  Esc      cancel a prompt

Add a row: run `minimalrouter`, press a, paste base URL + API key,
pick a model from the fetched list; the row is saved only if the test passes.

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
        if len(args) != 2 or args[1] not in profiles:
            print("Usage: minimalrouter oauth <provider>")
            print("Providers configured in ~/.minimalrouter/oauth-providers.json:",
                  ", ".join(sorted(profiles)) or "(none)")
            return 2
        provider = args[1]
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
"""Offline unit tests for MinimalRouter — stdlib unittest, zero network.

Run: python -m unittest discover -s tests -v
Sabotage check (a regression test must fail pre-fix):
    MR_TEST_ENGINE=/path/to/pre-fix/minimalrouter.py python -m unittest discover -s tests

Regression tests carry the defect id they bite (D1-D4, full audit 2026-11-19).
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ENGINE = Path(os.environ.get("MR_TEST_ENGINE") or ROOT / "minimalrouter.py")

_spec = importlib.util.spec_from_file_location("minimalrouter_under_test", ENGINE)
assert _spec is not None and _spec.loader is not None
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

# SESSION-WIDE LOG ISOLATION. Per-class patching of LOG_PATH cannot work, because
# engine functions call log() INTERNALLY: probe_row(), api_disable() and the
# translators all write the module global on their own code path, so a test that
# never mentions LOG_PATH still appends to the real file. Found by
# `minimalrouter doctor`, which read the live log tail and displayed
# "FAIL m via self-row ... 127.0.0.1:9322" — pollution written by
# test_probe_of_self_row_fails_honestly — as if it were a real outage.
#
# Redirect once, here, so the wrong path is unavailable rather than opt-in. No
# engine test reads the log (verified: only the doctor class references LOG_PATH
# at all), and a diagnostic that reports the estate's own test noise is worse
# than no diagnostic.
_LOG_TMP = tempfile.TemporaryDirectory(prefix="mr-tests-log-")
setattr(mr, "LOG_PATH", os.path.join(_LOG_TMP.name, "minimalrouter.log"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestRowClass(unittest.TestCase):
    def test_api_by_key(self):
        self.assertEqual(mr.row_class({"name": "x", "base": "b", "model": "m", "key": "K"}), "api")

    def test_api_by_key_env(self):
        self.assertEqual(mr.row_class({"name": "x", "base": "b", "model": "m", "key_env": "E"}), "api")

    def test_keyless_empty(self):
        self.assertEqual(mr.row_class({"name": "x", "base": "b", "model": "m", "key": "", "key_env": ""}), "keyless")

    def test_oauth_by_provider(self):
        self.assertEqual(mr.row_class({"name": "x", "base": "b", "model": "m", "oauth_provider": "dl"}), "oauth")

    def test_spoof_keyless_label_with_key_is_api(self):
        row = {"name": "x", "base": "b", "model": "m", "key": "SECRET", "class": "keyless"}
        self.assertEqual(mr.row_class(row), "api")

    def test_spoof_oauth_label_without_provider_is_not_oauth(self):
        row = {"name": "x", "base": "b", "model": "m", "key": "K", "class": "oauth"}
        self.assertEqual(mr.row_class(row), "api")

    def test_valid_classes(self):
        self.assertEqual(set(mr.VALID_CLASSES), {"api", "oauth", "keyless"})


class TestTokenLive(unittest.TestCase):
    def test_no_expiry_jwt_never_expires(self):
        self.assertTrue(mr.token_live({"access_token": "t", "expires_at": 0}))

    def test_past_is_dead(self):
        self.assertFalse(mr.token_live({"access_token": "t", "expires_at": time.time() - 10}))

    def test_future_is_live(self):
        self.assertTrue(mr.token_live({"access_token": "t", "expires_at": time.time() + 3600}))

    def test_no_token_is_dead(self):
        self.assertFalse(mr.token_live({"expires_at": 0}))


class TestEp(unittest.TestCase):
    def test_no_double_v1(self):
        self.assertEqual(mr.ep("https://x.com/v1", "/v1/chat/completions"),
                         "https://x.com/v1/chat/completions")

    def test_trailing_slash(self):
        self.assertEqual(mr.ep("https://x.com/v1/", "/v1/models"), "https://x.com/v1/models")

    def test_base_with_path_chat_path_override(self):
        self.assertEqual(mr.ep("https://x.com/models/openai", "/chat/completions"),
                         "https://x.com/models/openai/chat/completions")


class TestErrorCode(unittest.TestCase):
    def test_nested_code(self):
        self.assertEqual(mr.error_code({"error": {"code": "authorization_pending"}}), "authorization_pending")

    def test_nested_type(self):
        self.assertEqual(mr.error_code({"error": {"type": "slow_down"}}), "slow_down")

    def test_flat_string(self):
        self.assertEqual(mr.error_code({"error": "authorization_pending"}), "authorization_pending")

    def test_non_dict(self):
        self.assertEqual(mr.error_code("nope"), "")


class TestStateFor(unittest.TestCase):
    """D1 regression: /api/state rows must carry oauth_provider so the TUI r-key
    can resolve the selected row's provider. Pre-fix state_for dropped it."""

    def test_carries_class_and_oauth_provider(self):
        rows = [
            {"name": "file-row", "base": "https://a/v1", "model": "m", "key": "K"},
            {"name": "dl-m", "base": "https://d/v1", "model": "m", "oauth_provider": "dl", "key": "T"},
            {"name": "keyless-row", "base": "https://k/v1", "model": "m", "key": "", "key_env": ""},
        ]
        st = {r["name"]: r for r in mr.state_for(rows)}
        self.assertEqual(st["dl-m"]["class"], "oauth")
        self.assertEqual(st["dl-m"].get("oauth_provider"), "dl")
        self.assertEqual(st["file-row"]["class"], "api")
        self.assertEqual(st["file-row"].get("oauth_provider", ""), "")
        self.assertEqual(st["keyless-row"]["class"], "keyless")
        self.assertEqual(st["file-row"]["key_src"], "pasted")

    def test_spoofed_label_ignored_in_state(self):
        rows = [{"name": "spoof", "base": "b", "model": "m", "key": "K", "class": "keyless"}]
        self.assertEqual(mr.state_for(rows)[0]["class"], "api")


class TestByModel(unittest.TestCase):
    def test_chain_order_and_aliases(self):
        r1 = {"name": "a", "base": "b", "model": "m", "aliases": ["alias1"]}
        r2 = {"name": "b", "base": "b", "model": "m"}
        r3 = {"name": "c", "base": "b", "model": "alias1"}
        bm = mr.BY_MODEL([r1, r2, r3])
        self.assertEqual([r["name"] for r in bm["m"]], ["a", "b"])
        self.assertEqual([r["name"] for r in bm["alias1"]], ["a", "c"])


class TestZcodeStatus(unittest.TestCase):
    def test_business_error_map(self):
        for code, status in [("1005", 429), ("1113", 429), ("3008", 429), ("3009", 429), ("3012", 403)]:
            self.assertEqual(mr.zcode_status({"code": int(code), "msg": "x"}), status)

    def test_success_and_unknown(self):
        self.assertEqual(mr.zcode_status({"code": 0}), 200)
        self.assertEqual(mr.zcode_status({}), 200)
        self.assertEqual(mr.zcode_status("not a dict"), 200)


class TestChatToZcode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = (mr.MR_HOME, mr.ZCODE_IDENTITY_PATH, getattr(mr, "OAUTH_PATH", None))
        setattr(mr, "MR_HOME", self._tmp.name)
        setattr(mr, "ZCODE_IDENTITY_PATH", str(ROOT / "bridges" / "zcode-identity.json"))
        if hasattr(mr, "OAUTH_PATH"):
            setattr(mr, "OAUTH_PATH", os.path.join(self._tmp.name, "oauth.json"))

    def tearDown(self):
        home, ident, op = self._old
        setattr(mr, "MR_HOME", home)
        setattr(mr, "ZCODE_IDENTITY_PATH", ident)
        if op is not None:
            setattr(mr, "OAUTH_PATH", op)
        self._tmp.cleanup()

    def test_body_shape(self):
        asset = json.loads((ROOT / "bridges" / "zcode-identity.json").read_text())
        row = {"name": "z", "base": "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages",
               "model": "GLM-5.3-Flash", "key": "T", "oauth_provider": "zcode"}
        payload = {"model": "GLM-5.3-Flash", "max_tokens": 100,
                   "messages": [{"role": "system", "content": "be nice"},
                                {"role": "user", "content": "hi"}]}
        body, ids = mr.chat_to_zcode(row, payload)
        self.assertEqual(body["model"], "glm-5.3-flash")
        self.assertEqual(len(body["system"]), 3)
        self.assertEqual(body["system"][1]["text"], asset["stablePrefix"])
        self.assertEqual(body["system"][1]["cache_control"], {"type": "ephemeral"})
        self.assertTrue(body["system"][2]["text"].startswith("\n\nbe nice"))
        uid = json.loads(body["metadata"]["user_id"])
        self.assertEqual(set(uid), {"device_id", "account_uuid", "session_id"})
        self.assertEqual(body["messages"], [{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        for k in ("device_id", "account_uuid", "session_id", "request_id", "trace_id", "query_id"):
            self.assertIn(k, ids)


class TestChatToOmnirush(unittest.TestCase):
    def test_forced_flags_and_typed_input(self):
        row = {"name": "o", "base": "https://omnirush.ai/omnirush/v1", "model": "gpt-6-astra",
               "key": "T", "oauth_provider": "omnirush"}
        payload = {"model": "gpt-6-astra", "stream": False,
                   "messages": [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "hi"}]}
        body = mr.chat_to_omnirush(row, payload)
        self.assertIs(body["stream"], True)
        self.assertIs(body["store"], False)
        self.assertEqual(body["instructions"], "sys")
        self.assertIsInstance(body["input"], list)
        self.assertEqual(body["input"][0]["type"], "message")
        self.assertEqual(body["input"][0]["content"][0]["type"], "input_text")


class TestPidHelpers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = mr.PID_PATH
        setattr(mr, "PID_PATH", os.path.join(self._tmp.name, "minimalrouter.pid"))

    def tearDown(self):
        setattr(mr, "PID_PATH", self._old)
        self._tmp.cleanup()

    def test_write_read_remove_roundtrip(self):
        mr.write_pidfile()
        pid, _port = mr.read_pidfile()
        self.assertEqual(pid, os.getpid())
        mr.remove_pidfile(force=True)
        self.assertFalse(os.path.exists(mr.PID_PATH))

    def test_pid_alive_self(self):
        self.assertTrue(mr.pid_alive(os.getpid()))

    def test_pid_alive_impossible_pid(self):
        self.assertFalse(mr.pid_alive(9999999))

    @unittest.skipUnless(hasattr(os, "fork") and os.path.isdir("/proc"), "needs /proc + fork")
    def test_zombie_is_dead(self):
        """D3 regression: an unreaped zombie must count as dead, else stop strands
        the pid file. Pre-fix pid_alive used /proc directory existence (True for Z)."""
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            time.sleep(0.4)  # child exits; parent has NOT reaped -> state Z
            state = open("/proc/%d/stat" % pid).read().rsplit(") ", 1)[1].split()[0]
            self.assertEqual(state, "Z", "test precondition: child must be a zombie")
            self.assertFalse(mr.pid_alive(pid))
        finally:
            os.waitpid(pid, 0)


class TestOAuthAddValidation(unittest.TestCase):
    """D2 regression: a non-URL base must cancel cleanly, not crash discovery.
    Runs the CLI in a throwaway HOME; validation fires before any network call."""

    def _run(self, answers):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, HOME=tmp)
        p = subprocess.run([sys.executable, str(ENGINE), "oauth", "add"],
                           input=answers, capture_output=True, text=True,
                           timeout=30, env=env, cwd=tmp)
        return p

    def test_garbage_base_cancels_cleanly(self):
        p = self._run("zztest\nnot-a-url\n")
        self.assertEqual(p.returncode, 2)
        self.assertIn("cancelled: base URL must start with", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_empty_base_cancels_cleanly(self):
        p = self._run("zztest2\n\n")
        self.assertEqual(p.returncode, 2)
        self.assertIn("cancelled: base URL is required", p.stdout)
        self.assertNotIn("Traceback", p.stderr)


class TestEnvelopeCarriesUpstreamBody(unittest.TestCase):
    """D4 regression: the all-rows-failed envelope must carry the upstream's own
    error body. Fixture: a loopback server answering 502 with an actionable body,
    one row pointing at it, real router subprocess in a throwaway HOME."""

    MARKER = "not logged in - run: bridge --login"

    def test_envelope(self):
        marker = self.MARKER

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps({"error": marker}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        fixture = HTTPServer(("127.0.0.1", 0), H)
        fport = fixture.server_address[1]
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        tmp = tempfile.mkdtemp()
        rport = free_port()
        with open(os.path.join(tmp, ".minimalrouter-setup"), "w") as f:
            f.write("")
        os.makedirs(os.path.join(tmp, ".minimalrouter"), exist_ok=True)
        with open(os.path.join(tmp, ".minimalrouter", "rows.json"), "w") as f:
            json.dump([{"name": "fixture", "base": "http://127.0.0.1:%d/v1" % fport,
                        "model": "m", "key": "k"}], f)
        env = dict(os.environ, HOME=tmp, MINIMALROUTER_PORT=str(rport))
        srv = subprocess.Popen([sys.executable, str(ENGINE), "serve"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            base = "http://127.0.0.1:%d" % rport
            up = False
            for _ in range(40):
                try:
                    with urllib.request.urlopen(base + "/health", timeout=2) as r:
                        up = r.status == 200
                    if up:
                        break
                except Exception:
                    time.sleep(0.25)
            self.assertTrue(up, "router subprocess did not come up")
            rq = urllib.request.Request(
                base + "/v1/chat/completions",
                data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                 "max_tokens": 10}).encode(),
                method="POST", headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(rq, timeout=20) as r:
                    self.fail("expected 502, got %s" % r.status)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 502)
                body = e.read().decode()
                self.assertIn(marker, body)
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=8)
            except subprocess.TimeoutExpired:
                srv.kill()
            fixture.shutdown()


class TestClassifyStatus(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(mr.classify_status({"status": "ok"}), "ok")

    def test_unknown(self):
        self.assertEqual(mr.classify_status({}), "unknown")
        self.assertEqual(mr.classify_status({"status": "unknown"}), "unknown")

    def test_gated_403(self):
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "HTTP Error 403: Forbidden | {\"code\":\"dl_only\"}"}), "gated")

    def test_empty_402(self):
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "HTTP Error 402: Payment Required"}), "empty")

    def test_other_fail_is_error(self):
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "HTTP Error 530: <none>"}), "error")
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "URLError: connection refused"}), "error")


class TestSparkChars(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(mr.spark_chars([]), "")

    def test_default_width_is_five(self):
        self.assertEqual(len(mr.spark_chars(list(range(10)))), 5)

    def test_length_capped(self):
        self.assertEqual(len(mr.spark_chars(list(range(10)), width=5)), 5)

    def test_flat_history(self):
        self.assertEqual(len(mr.spark_chars([5, 5, 5])), 3)  # no div-by-zero
        self.assertEqual(mr.spark_chars([5, 5, 5]), "▄▄▄")  # steady mid, not bleed

    def test_monotonic_uses_full_range(self):
        s = mr.spark_chars([1, 10])
        self.assertEqual(s[0], "▁")
        self.assertEqual(s[-1], "█")


class TestRecordState(unittest.TestCase):
    def setUp(self):
        self._saved = dict(mr.ROW_STATE)
        mr.ROW_STATE.clear()

    def tearDown(self):
        mr.ROW_STATE.clear()
        mr.ROW_STATE.update(self._saved)

    def test_hist_bounded_and_json_safe(self):
        for i in range(12):
            mr.record_state("r", "ok", ms=100 + i)
        st = mr.ROW_STATE["r"]
        self.assertEqual(len(st["hist"]), mr.HIST_MAX)
        self.assertIsInstance(st["hist"], list)
        json.dumps(st)  # must not raise (a deque would)

    def test_hist_is_last_n(self):
        for i in range(8):
            mr.record_state("r", "ok", ms=i)
        self.assertEqual(mr.ROW_STATE["r"]["hist"], [3, 4, 5, 6, 7])

    def test_state_fields_preserved(self):
        mr.record_state("r", "fail", ms=50, error="HTTP Error 403: x")
        st = mr.ROW_STATE["r"]
        self.assertEqual(st["status"], "fail")
        self.assertEqual(st["error"], "HTTP Error 403: x")
        self.assertIn("at", st)


class TestPoolStatsReconcile(unittest.TestCase):
    """v1.2.2: the health tiles are a partition — SERVING + DOWN + UNKNOWN == ROWS.
    A row that is not serving is visible in DOWN (errored, rate-limited, gated,
    or parked); only never-probed rows sit outside both (boot window)."""

    def test_partition_covers_every_row(self):
        rows = [{"state": {"status": "ok"}},
                {"state": {"status": "fail", "error": "HTTP Error 403: x"}},
                {"state": {"status": "fail", "error": "HTTP Error 402: x"}},
                {"state": {"status": "fail", "error": "HTTP Error 429: x"}},
                {"state": {"status": "fail", "error": "HTTP Error 530: x",
                           "fail_streak": 3}},
                {"state": {"status": "fail", "error": "URLError: refused"}},
                {},
                {"state": {"status": "ok"}, "disabled": True}]
        s = mr.pool_stats(rows)
        self.assertEqual(s["rows"], 8)
        self.assertEqual(s["serving"], 1)
        self.assertEqual(s["down"], 6)     # 403+402+429+dead+error+parked-ok
        self.assertEqual(s["unknown"], 1)  # never probed
        self.assertEqual(s["serving"] + s["down"] + s["unknown"], s["rows"])
        self.assertEqual(s["blocked"], 2)  # granular fields still reported
        self.assertEqual(s["cooldown"], 1)
        self.assertEqual(s["dead"], 1)
        self.assertEqual(s["disabled"], 1)

    def test_parked_row_counts_down_not_serving(self):
        rows = [{"state": {"status": "ok"}, "disabled": True}]
        s = mr.pool_stats(rows)
        self.assertEqual(s["serving"], 0)
        self.assertEqual(s["down"], 1)


class TestPoolStats(unittest.TestCase):
    def test_counts(self):
        rows = [{"state": {"status": "ok"}},
                {"state": {"status": "fail", "error": "HTTP Error 403: x"}},
                {"state": {"status": "fail", "error": "HTTP Error 402: x"}},
                {"state": {"status": "fail", "error": "HTTP Error 530: x"}},
                {}]
        s = mr.pool_stats(rows)
        self.assertEqual(s["rows"], 5)
        self.assertEqual(s["serving"], 1)
        self.assertEqual(s["blocked"], 2)  # gated + empty


class TestTextualDashboardLoader(unittest.TestCase):
    """The optional-Textual integration must never break the stdlib core:
    absent -> curses fallback, curses pref -> forced fallback, present -> runner."""

    def setUp(self):
        self._env = os.environ.get("MINIMALROUTER_TUI")
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

    def tearDown(self):
        if self._env is None:
            os.environ.pop("MINIMALROUTER_TUI", None)
        else:
            os.environ["MINIMALROUTER_TUI"] = self._env

    def test_curses_pref_forces_fallback(self):
        os.environ["MINIMALROUTER_TUI"] = "curses"
        self.assertIsNone(mr._load_textual_dashboard())

    def test_absent_module_returns_none(self):
        os.environ["MINIMALROUTER_TUI"] = "auto"
        # sys.modules[name] = None makes `import name` raise ImportError,
        # simulating "textual / dashboard not installed" in any environment.
        with mock.patch.dict(sys.modules, {"minimalrouter_dashboard": None}):
            self.assertIsNone(mr._load_textual_dashboard())

    def test_textual_pref_absent_prints_hint_and_falls_back(self):
        os.environ["MINIMALROUTER_TUI"] = "textual"
        with mock.patch.dict(sys.modules, {"minimalrouter_dashboard": None}):
            self.assertIsNone(mr._load_textual_dashboard())

    def test_present_module_returns_runner(self):
        os.environ["MINIMALROUTER_TUI"] = "auto"
        fake = types.ModuleType("minimalrouter_dashboard")
        setattr(fake, "run_dashboard", lambda port=8790: None)
        with mock.patch.dict(sys.modules, {"minimalrouter_dashboard": fake}):
            self.assertIs(mr._load_textual_dashboard(), getattr(fake, "run_dashboard"))

    def test_broken_module_returns_none_not_crash(self):
        os.environ["MINIMALROUTER_TUI"] = "auto"
        broken = types.ModuleType("minimalrouter_dashboard")
        # no run_dashboard attribute -> loader must not raise
        with mock.patch.dict(sys.modules, {"minimalrouter_dashboard": broken}):
            try:
                got = mr._load_textual_dashboard()
            except Exception as e:
                self.fail("loader raised on a broken module: %r" % e)
            self.assertIsNone(got)


class TestVersion(unittest.TestCase):
    """__version__ is the single source; every surface must derive from it (semver)."""

    def test_version_is_semver(self):
        self.assertRegex(mr.__version__, r"^\d+\.\d+\.\d+$")

    def test_server_version_derives_from_version(self):
        self.assertEqual(mr.Handler.server_version, "minimalrouter/" + mr.__version__)

    def test_dash_v_prints_version(self):
        out = subprocess.run(
            [sys.executable, str(ENGINE), "-v"], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "minimalrouter " + mr.__version__)


if __name__ == "__main__":
    unittest.main()
# ---- v1.2.0 regression tests (Mission Control overhaul) ----

class TestClassifyV12(unittest.TestCase):
    """Richer STATUS vocabulary: cooldown (429), dead (streak), stale (old ok)."""

    def test_cooldown_429_single_fail(self):
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "HTTP Error 429: Too Many Requests",
             "fail_streak": 1}), "cooldown")

    def test_dead_after_streak(self):
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "HTTP Error 530: x",
             "fail_streak": mr.FAIL_STREAK_DEAD}), "dead")

    def test_gated_outranks_dead(self):
        # 403 is an account state, not flapping — priority over streak death.
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "HTTP Error 403: Forbidden",
             "fail_streak": 9}), "gated")

    def test_429_streak_is_dead(self):
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "HTTP Error 429: x",
             "fail_streak": 5}), "dead")

    def test_stale_old_ok_row(self):
        now = time.time()
        self.assertEqual(mr.classify_status(
            {"status": "ok", "at": now - mr.STALE_AFTER - 1}, now), "stale")
        self.assertEqual(mr.classify_status(
            {"status": "ok", "at": now - 60}, now), "ok")

    def test_ok_without_at_is_ok(self):
        self.assertEqual(mr.classify_status({"status": "ok"}), "ok")


class TestRecordStateV12(unittest.TestCase):
    def setUp(self):
        self._saved = dict(mr.ROW_STATE)
        mr.ROW_STATE.clear()

    def tearDown(self):
        mr.ROW_STATE.clear()
        mr.ROW_STATE.update(self._saved)

    def test_fail_streak_increments_and_resets(self):
        mr.record_state("r", "fail", 10, error="HTTP Error 500: x")
        mr.record_state("r", "fail", 10, error="HTTP Error 500: x")
        self.assertEqual(mr.ROW_STATE["r"]["fail_streak"], 2)
        mr.record_state("r", "ok", 10)
        self.assertEqual(mr.ROW_STATE["r"]["fail_streak"], 0)

    def test_recent_bounded_for_ok_pct(self):
        for i in range(25):
            mr.record_state("r", "ok" if i % 2 else "fail", 10,
                            error="" if i % 2 else "HTTP Error 500: x")
        recent = mr.ROW_STATE["r"]["recent"]
        self.assertEqual(len(recent), mr.RECENT_MAX)
        self.assertEqual(sum(recent), 5)

    def test_tps_ema_only_with_usage(self):
        mr.record_state("r", "ok", 1000, tokens=100)   # 100 tok/s
        self.assertAlmostEqual(mr.ROW_STATE["r"]["tps"], 100.0, places=1)
        mr.record_state("r", "ok", 1000, tokens=200)   # 200 tok/s -> EMA 0.7/0.3
        self.assertAlmostEqual(mr.ROW_STATE["r"]["tps"], 130.0, places=1)
        mr.record_state("r", "ok", 1000)               # no usage -> tps held
        self.assertAlmostEqual(mr.ROW_STATE["r"]["tps"], 130.0, places=1)


class TestNoteReqAndPoolStatsV12(unittest.TestCase):
    def setUp(self):
        self._saved_log = list(mr.REQLOG)
        self._saved_boot = mr.BOOT_TIME
        mr.REQLOG.clear()

    def tearDown(self):
        mr.REQLOG.clear()
        mr.REQLOG.extend(self._saved_log)
        mr.BOOT_TIME = self._saved_boot

    def test_req_window_and_err_pct(self):
        now = time.time()
        mr.BOOT_TIME = now - 3600
        rows = [{"state": {"status": "ok", "at": now}},
                {"state": {"status": "fail", "error": "HTTP Error 402: x"},
                 "disabled": True}]
        # 3 requests inside the 60s window (1 failed), 1 outside.
        with mr.STAT_LOCK:
            mr.REQLOG.extend([(now - 10, True, 120, 900),
                              (now - 5, True, 60, 500),
                              (now - 2, False, 0, 100),
                              (now - 120, True, 999, 800)])
        s = mr.pool_stats(rows, now)
        self.assertEqual(s["req_min"], 3)
        self.assertEqual(s["tok_s"], round((120 + 60 + 0) / 60.0, 1))
        self.assertEqual(s["err_pct"], 25)  # 1 of the last 100 requests (4 logged)
        self.assertEqual(s["rows"], 2)
        self.assertEqual(s["disabled"], 1)
        self.assertEqual(s["blocked"], 1)
        self.assertEqual(s["uptime"], 3600)

    def test_note_req_bounded(self):
        for i in range(mr.REQLOG_MAX + 50):
            mr.note_req(True, 1, 1)
        self.assertEqual(len(mr.REQLOG), mr.REQLOG_MAX)


class TestResolveCw(unittest.TestCase):
    def setUp(self):
        self._saved_rt = dict(mr.RUNTIME_CW)

    def tearDown(self):
        mr.RUNTIME_CW.clear()
        mr.RUNTIME_CW.update(self._saved_rt)

    def test_override_wins(self):
        mr.RUNTIME_CW["m"] = 64000
        self.assertEqual(mr.resolve_cw({"model": "m", "context_length": 32000}),
                         32000)

    def test_runtime_beats_curated(self):
        mr.RUNTIME_CW["gpt-oss-20b"] = 999
        self.assertEqual(mr.resolve_cw({"model": "gpt-oss-20b"}), 999)

    def test_curated_fallback_case_insensitive(self):
        self.assertEqual(mr.resolve_cw({"model": "GPT-OSS-20B"}), 131072)

    def test_unknown_is_none_never_guessed(self):
        self.assertIsNone(mr.resolve_cw({"model": "totally-unknown-model-xyz"}))


class TestProbeTokens(unittest.TestCase):
    """probe() carries usage.completion_tokens so the boot probe seeds the
    per-row TPS EMA (the board's TPS column is live from boot, not only after
    real client traffic). v1.2.0 polish, found by the captured board grid."""

    def test_probe_returns_tokens(self):
        class FakeResp:
            def read(self, *a):
                return json.dumps({
                    "choices": [{"message": {"role": "assistant",
                                             "content": "OK"}}],
                    "usage": {"completion_tokens": 42}}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        old = mr.upstream
        mr.upstream = lambda *a, **k: FakeResp()
        _saved = dict(mr.ROW_STATE)
        mr.ROW_STATE.clear()
        try:
            ok, ms, err, content, tokens = mr.probe("https://x/v1", "k", "m")
            self.assertTrue(ok)
            self.assertEqual(tokens, 42)
            mr.probe_row({"name": "tr", "base": "https://x/v1",
                          "model": "m", "key": "k"})
            # the fake call's ms is ~0 so the EMA value is not meaningful —
            # the contract is that tokens SEED the tps field (non-zero)
            self.assertGreater(mr.ROW_STATE["tr"]["tps"], 0)
        finally:
            mr.upstream = old
            mr.ROW_STATE.clear()
            mr.ROW_STATE.update(_saved)

    def test_probe_no_usage_is_zero_floor(self):
        class FakeResp:
            def read(self, *a):
                return json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": "OK"}}]}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        old = mr.upstream
        mr.upstream = lambda *a, **k: FakeResp()
        try:
            ok, ms, err, content, tokens = mr.probe("https://x/v1", "k", "m")
            self.assertTrue(ok)
            self.assertEqual(tokens, 0)  # honest floor, never an estimate
        finally:
            mr.upstream = old

    def test_learn_catalogs_shared_with_boot(self):
        # learn_catalogs exists and is the same code path /api/refetch uses
        self.assertTrue(callable(getattr(mr, "learn_catalogs", None)))


class TestFetchModelsCw(unittest.TestCase):
    def setUp(self):
        self._saved_rt = dict(mr.RUNTIME_CW)

    def tearDown(self):
        mr.RUNTIME_CW.clear()
        mr.RUNTIME_CW.update(self._saved_rt)

    def test_context_length_learned(self):
        body = {"data": [{"id": "foo-x", "context_length": 262144},
                         {"id": "bar-y", "max_input_tokens": 8192},
                         {"id": "baz-z"}]}

        class FakeResp:
            def read(self, *a):
                return json.dumps(body).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        old = mr.upstream
        mr.upstream = lambda base, key, path, payload=None, timeout=0, row=None: FakeResp()
        try:
            ids = mr.fetch_models("https://x/v1", "K")
        finally:
            mr.upstream = old
        self.assertEqual(ids, ["foo-x", "bar-y", "baz-z"])
        self.assertEqual(mr.RUNTIME_CW["foo-x"], 262144)
        self.assertEqual(mr.RUNTIME_CW["bar-y"], 8192)
        self.assertNotIn("baz-z", mr.RUNTIME_CW)


class TestDisabledRouting(unittest.TestCase):
    def test_enabled_rows_excludes_disabled(self):
        old = mr.all_rows
        mr.all_rows = lambda: [
            {"name": "a", "base": "https://a/v1", "model": "m1", "key": "K"},
            {"name": "b", "base": "https://b/v1", "model": "m2", "key": "K",
             "disabled": True},
        ]
        try:
            names = [r["name"] for r in mr.enabled_rows()]
            self.assertEqual(names, ["a"])
            bm = mr.BY_MODEL(mr.enabled_rows())
            self.assertIn("m1", bm)
            self.assertNotIn("m2", bm)  # a disabled row can never serve
        finally:
            mr.all_rows = old

    def test_state_for_carries_new_fields(self):
        rows = [{"name": "r", "base": "https://api.groq.com/v1", "model": "m",
                 "key": "K", "disabled": True}]
        st = mr.state_for(rows)[0]
        self.assertTrue(st["disabled"])
        self.assertEqual(st["provider"], "api.groq.com")
        self.assertIn("cw", st)
        self.assertIsNone(st["ok_pct"])  # no recent outcomes yet


class TestDisableEndpoint(unittest.TestCase):
    """Live /api/disable: parks a file row, routing + listings drop it, enable
    restores it. OAuth profile disable is covered by shape, not network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rows = os.path.join(self.tmp.name, "rows.json")
        with open(rows, "w") as f:
            json.dump([
                {"name": "r1", "base": "https://x1/v1", "model": "m1", "key": "K"},
                {"name": "r2", "base": "https://x2/v1", "model": "m2", "key": ""},
            ], f)
        self._saved = (mr.ROWS_PATH, mr.OAUTH_PROFILES_PATH, mr.OAUTH_PATH)
        mr.ROWS_PATH = rows
        mr.OAUTH_PROFILES_PATH = os.path.join(self.tmp.name, "none.json")
        mr.OAUTH_PATH = os.path.join(self.tmp.name, "none2.json")
        self.srv = HTTPServer(("127.0.0.1", 0), mr.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        (mr.ROWS_PATH, mr.OAUTH_PROFILES_PATH, mr.OAUTH_PATH) = self._saved
        self.tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path),
                                    timeout=10) as r:
            return json.loads(r.read() or b"{}")

    def _post(self, path, obj):
        rq = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(rq, timeout=10) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}")

    def test_disable_roundtrip(self):
        models = [d["id"] for d in self._get("/v1/models")["data"]]
        self.assertIn("m1", models)
        res = self._post("/api/disable", {"name": "r1", "disabled": True})
        self.assertTrue(res.get("ok"))
        models = [d["id"] for d in self._get("/v1/models")["data"]]
        self.assertNotIn("m1", models)
        self.assertIn("m2", models)
        st = self._get("/api/state")
        by = {r["name"]: r for r in st["rows"]}
        self.assertTrue(by["r1"]["disabled"])
        self.assertFalse(by["r2"]["disabled"])
        self.assertEqual(st["stats"]["disabled"], 1)
        h = self._get("/health")
        self.assertEqual(h["disabled"], 1)
        # a disabled model 404s at the chat endpoint (never served, never 502)
        rq = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % self.port,
            data=json.dumps({"model": "m1", "messages": []}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(rq, timeout=10)
            self.fail("disabled row served a request")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
        res = self._post("/api/disable", {"name": "r1", "disabled": False})
        self.assertTrue(res.get("ok"))
        models = [d["id"] for d in self._get("/v1/models")["data"]]
        self.assertIn("m1", models)
        with open(mr.ROWS_PATH) as f:
            self.assertNotIn("disabled", json.load(f)[0])

    def test_disable_unknown_row(self):
        res = self._post("/api/disable", {"name": "nope", "disabled": True})
        self.assertIn("error", res)


class TestApiStateV12(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rows = os.path.join(self.tmp.name, "rows.json")
        with open(rows, "w") as f:
            json.dump([{"name": "r1", "base": "https://x1/v1", "model": "gpt-oss-20b",
                        "key": "K"}], f)
        self._saved = (mr.ROWS_PATH, mr.OAUTH_PROFILES_PATH, mr.OAUTH_PATH)
        mr.ROWS_PATH = rows
        mr.OAUTH_PROFILES_PATH = os.path.join(self.tmp.name, "none.json")
        mr.OAUTH_PATH = os.path.join(self.tmp.name, "none2.json")
        self.srv = HTTPServer(("127.0.0.1", 0), mr.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        (mr.ROWS_PATH, mr.OAUTH_PROFILES_PATH, mr.OAUTH_PATH) = self._saved
        self.tmp.cleanup()

    def test_state_carries_stats_probe_version_cw(self):
        # live state must reach the stats projection: an ok row reads SERVING=1
        # (regression: /api/state ran pool_stats over RAW rows, which carry no
        # "state" -> every tile read zero; found by the v1.2.0 live battery)
        _saved = dict(mr.ROW_STATE)
        mr.ROW_STATE.clear()
        try:
            mr.record_state("r1", "ok", 12)
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/state" % self.port, timeout=10) as r:
                st = json.loads(r.read() or b"{}")
            self.assertEqual(st["stats"]["serving"], 1)
            self.assertEqual(st["stats"]["rows"], 1)
        finally:
            mr.ROW_STATE.clear()
            mr.ROW_STATE.update(_saved)
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/state" % self.port, timeout=10) as r:
            st = json.loads(r.read() or b"{}")
        self.assertIn("stats", st)
        for k in ("rows", "serving", "blocked", "req_min", "tok_s", "err_pct",
                  "uptime"):
            self.assertIn(k, st["stats"])
        self.assertIn("probe", st)
        self.assertEqual(st["version"], mr.__version__)
        row = st["rows"][0]
        self.assertEqual(row["cw"], 131072)  # curated gpt-oss-20b, resolved server-side
        self.assertEqual(row["provider"], "x1")  # host shortening happens board-side;
        # engine carries the raw host
# ---- v1.2.0 self-base guard tests ----

class TestSelfBaseGuard(unittest.TestCase):
    """A row pointing at the router's own listener must never be called."""

    def test_detects_own_port_loopback(self):
        self.assertTrue(mr._is_self_base("http://127.0.0.1:%d/v1" % mr.PORT))
        self.assertTrue(mr._is_self_base("http://localhost:%d/v1" % mr.PORT))

    def test_sidecar_port_is_not_self(self):
        self.assertFalse(mr._is_self_base("http://127.0.0.1:%d/v1" % (mr.PORT + 1)))

    def test_external_host_same_port_is_not_self(self):
        # the router binds loopback only; an external host:PORT is someone else's
        self.assertFalse(mr._is_self_base("https://api.example.com:%d/v1" % mr.PORT))

    def test_upstream_refuses_before_socket(self):
        old = mr.PORT
        try:
            mr.PORT = 9321
            try:
                mr.upstream("http://127.0.0.1:9321/v1", "k",
                            "/v1/chat/completions", {})
                self.fail("self-referential base was called")
            except ValueError as e:
                self.assertIn("self-referential", str(e))
        finally:
            mr.PORT = old

    def test_probe_of_self_row_fails_honestly(self):
        old = mr.PORT
        _saved = dict(mr.ROW_STATE)
        mr.ROW_STATE.clear()
        try:
            mr.PORT = 9322
            ok, ms, err = mr.probe_row({"name": "self-row", "base":
                                        "http://127.0.0.1:9322/v1",
                                        "model": "m", "key": "k"})
            self.assertFalse(ok)
            self.assertIn("self-referential", str(err))
            self.assertEqual(mr.ROW_STATE["self-row"]["status"], "fail")
        finally:
            mr.PORT = old
            mr.ROW_STATE.clear()
            mr.ROW_STATE.update(_saved)


# ---- v1.3.0 doctor tests (the install's silent failures, made loud) ----
#
# The installer's failure modes are silent by construction: a venv that cannot
# be built, or a pip that cannot reach the network, still produces a working
# router — with a plain curses board and no message anywhere saying why. These
# tests pin the findings doctor exists to produce, and one of them is new
# knowledge rather than a restatement: load_env() reads ~/.hermes/.env and never
# merges os.environ, so an exported key is invisible to the engine.


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = self._tmp.name
        self.home = home
        self._saved = {}
        for attr in ("MR_HOME", "ROWS_PATH", "OAUTH_PATH", "OAUTH_PROFILES_PATH",
                     "PROVIDER_REGISTRY_PATH", "ZCODE_IDENTITY_PATH", "PID_PATH",
                     "LOG_PATH", "ENV", "PORT", "HOME"):
            self._saved[attr] = getattr(mr, attr)
        mr.MR_HOME = home
        mr.ROWS_PATH = os.path.join(home, "rows.json")
        mr.OAUTH_PATH = os.path.join(home, "oauth.json")
        mr.OAUTH_PROFILES_PATH = os.path.join(home, "oauth-providers.json")
        mr.PROVIDER_REGISTRY_PATH = os.path.join(home, "provider-registry.json")
        mr.ZCODE_IDENTITY_PATH = os.path.join(home, "bridges",
                                              "zcode-identity.json")
        mr.PID_PATH = os.path.join(home, "minimalrouter.pid")
        mr.LOG_PATH = os.path.join(home, "minimalrouter.log")
        mr.ENV = {}
        mr.HOME = home
        mr.PORT = free_port()          # nothing listens -> the free branch
        self.healthy_install()

    def tearDown(self):
        for attr, val in self._saved.items():
            setattr(mr, attr, val)
        self._tmp.cleanup()

    # ---------------- fixtures ----------------
    def healthy_install(self, rows=None):
        json.dump(rows if rows is not None else [
            {"name": "groq-qwen", "base": "https://api.groq.com/openai/v1",
             "model": "openai/gpt-oss-120b", "key": "sk-inline"}],
            open(mr.ROWS_PATH, "w"))
        os.chmod(mr.ROWS_PATH, 0o600)
        json.dump({}, open(mr.OAUTH_PATH, "w"))
        json.dump({}, open(mr.OAUTH_PROFILES_PATH, "w"))
        os.chmod(mr.OAUTH_PATH, 0o600)
        json.dump({"providers": {
            "nous": {"label": "Nous", "base_url": "https://portal.nousresearch.com",
                     "models": ["x"], "authorize_url": "https://a",
                     "token_url": "https://t"}}},
            open(mr.PROVIDER_REGISTRY_PATH, "w"))
        json.dump({"theme": "midnight", "dense": False, "motion": True},
                  open(os.path.join(mr.MR_HOME, "dashboard.json"), "w"))
        open(mr.LOG_PATH, "w").close()

    def run_doctor(self, fix=False):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mr.doctor(fix=fix, as_json=True)
        data = json.loads(buf.getvalue())
        return code, data

    def findings(self, data, level=None):
        out = [c for c in data["checks"] if level is None or c["level"] == level]
        return out

    def names(self, data, level):
        return [c["name"] for c in self.findings(data, level)]

    def oauth(self, data, level):
        """OAuth findings only. The full WARN list legitimately carries a PATH
        line under a fake HOME, so an assertion on the whole list measures the
        fixture, not the credential logic."""
        return [c for c in self.findings(data, level)
                if c["name"].startswith("oauth:")]

    def text(self, data, level):
        return " | ".join(c["name"] + ": " + c["detail"] + " " + c["fix"]
                          for c in self.findings(data, level))

    # ---------------- the contract ----------------
    def test_healthy_install_reports_zero_failures(self):
        code, data = self.run_doctor()
        self.assertEqual(code, 0, "exit code must be 0 with nothing failing")
        self.assertTrue(data["healthy"])
        self.assertEqual(data["fail"], 0,
                         "unexpected FAILs: %s" % self.text(data, "FAIL"))

    def test_json_output_is_machine_readable(self):
        code, data = self.run_doctor()
        for k in ("version", "port", "home", "ok", "warn", "fail", "healthy",
                  "checks"):
            self.assertIn(k, data)
        self.assertTrue(data["checks"])
        for c in data["checks"]:
            self.assertIn(c["level"], ("OK", "WARN", "FAIL"))
            for k in ("name", "detail", "fix"):
                self.assertIsInstance(c[k], str)

    def test_exit_code_is_one_when_anything_fails(self):
        json.dump([{"name": "broken", "base": "not-a-url", "model": ""}],
                  open(mr.ROWS_PATH, "w"))
        code, data = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertFalse(data["healthy"])
        self.assertGreater(data["fail"], 0)

    def test_it_never_spends_a_token(self):
        """The wallet-disclosure guarantee: doctor must not issue an inference
        request, so it is safe to run against a wallet-backed pool."""
        seen = []

        def spy(url, *a, **k):
            u = url if isinstance(url, str) else getattr(url, "full_url", str(url))
            seen.append(u)
            raise mr.urllib.error.URLError("doctor test: no network")
        with mock.patch.object(mr.urllib.request, "urlopen", spy):
            code, data = self.run_doctor()
        for u in seen:
            self.assertIn("127.0.0.1", u, "doctor touched a non-loopback host: %s" % u)
            self.assertNotIn("chat/completions", u)
            self.assertNotIn("completions", u)
        self.assertEqual(code, 0, "an unreachable server must not be a failure")

    def test_a_dead_server_is_not_reported_as_broken(self):
        """doctor is called BEFORE anything runs, most of the time."""
        code, data = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertIn("port %d" % mr.PORT, self.names(data, "OK"))

    # ---------------- the rows findings ----------------
    def test_key_set_in_the_shell_but_not_in_env_file_is_a_failure(self):
        """THE new finding. load_env() reads ~/.hermes/.env only and never
        merges os.environ — so an `export` looks set everywhere except in the
        engine that has to use it."""
        self.healthy_install(rows=[
            {"name": "groq-qwen", "base": "https://api.groq.com/openai/v1",
             "model": "openai/gpt-oss-120b", "key_env": "MR_DOCTOR_TEST_KEY"}])
        with mock.patch.dict(os.environ, {"MR_DOCTOR_TEST_KEY": "sk-secret"}):
            code, data = self.run_doctor()
        blob = self.text(data, "FAIL")
        self.assertIn("key resolution", self.names(data, "FAIL"))
        self.assertIn("shell", blob, "must name the shell-vs-env-file split")
        self.assertIn(".env", blob, "must say where the variable has to live")
        self.assertEqual(code, 1)

    def test_missing_key_variable_is_a_failure(self):
        self.healthy_install(rows=[
            {"name": "groq-qwen", "base": "https://api.groq.com/openai/v1",
             "model": "openai/gpt-oss-120b", "key_env": "MR_NOWHERE_SET"}])
        code, data = self.run_doctor()
        blob = self.text(data, "FAIL")
        self.assertIn("MR_NOWHERE_SET", blob)
        self.assertIn("NEVER serve", blob)

    def test_key_present_in_the_env_file_passes(self):
        self.healthy_install(rows=[
            {"name": "groq-qwen", "base": "https://api.groq.com/openai/v1",
             "model": "openai/gpt-oss-120b", "key_env": "MR_IN_ENV_FILE"}])
        mr.ENV = {"MR_IN_ENV_FILE": "sk-real"}
        code, data = self.run_doctor()
        self.assertNotIn("key resolution", self.names(data, "FAIL"),
                         "the engine's own resolution rule must be reused")
        self.assertEqual(code, 0)

    def test_keyless_row_is_not_flagged(self):
        """A row with no key and no key_env is a legitimate KEYLESS row —
        doctor must not invent a failure for it."""
        self.healthy_install(rows=[
            {"name": "kilo-free", "base": "https://api.kilo.ai/api/openrouter",
             "model": "kilo-auto/free"}])
        code, data = self.run_doctor()
        self.assertEqual(self.findings(data, "FAIL"), [],
                         "a keyless row is legitimate: %s"
                         % self.text(data, "FAIL"))
        self.assertEqual(mr.row_class({"name": "kilo-free", "base": "b",
                                       "model": "m"}), "keyless",
                         "the engine itself must agree the row is keyless")

    def test_self_referential_row_is_a_failure(self):
        """The freebuff-bridge incident: a row pointing at our own port makes
        the router call itself in an unbounded loop."""
        self.healthy_install(rows=[
            {"name": "loop", "base": "http://127.0.0.1:%d/v1" % mr.PORT,
             "model": "m", "key": "k"}])
        code, data = self.run_doctor()
        self.assertIn("self-recursion", self.names(data, "FAIL"))
        self.assertEqual(code, 1)

    def test_duplicate_row_names_are_a_failure(self):
        """Row state is keyed by name, so a duplicate silently overwrites the
        other's health and both look broken."""
        self.healthy_install(rows=[
            {"name": "same", "base": "https://a/v1", "model": "m", "key": "k"},
            {"name": "same", "base": "https://b/v1", "model": "m", "key": "k"}])
        code, data = self.run_doctor()
        self.assertIn("row names", self.names(data, "FAIL"))

    def test_malformed_rows_are_a_failure_not_a_silent_skip(self):
        self.healthy_install(rows=[
            {"name": "no-base", "model": "m", "key": "k"},
            {"name": "no-model", "base": "https://a/v1", "key": "k"}])
        code, data = self.run_doctor()
        blob = self.text(data, "FAIL")
        self.assertIn("no-base", blob)
        self.assertIn("no-model", blob)

    def test_empty_pool_is_a_warning_not_a_crash(self):
        self.healthy_install(rows=[])
        code, data = self.run_doctor()
        self.assertIn("EMPTY", self.text(data, "WARN").upper())
        self.assertEqual(code, 0, "an empty pool is usable, just idle")

    def test_unparseable_rows_file_is_a_failure(self):
        open(mr.ROWS_PATH, "w").write("{not json")
        code, data = self.run_doctor()
        self.assertIn("rows.json", self.names(data, "FAIL"))
        self.assertEqual(code, 1)

    def test_parked_rows_are_reported_not_hidden(self):
        self.healthy_install(rows=[
            {"name": "live", "base": "https://a/v1", "model": "m", "key": "k"},
            {"name": "parked", "base": "https://b/v1", "model": "m", "key": "k",
             "disabled": True}])
        code, data = self.run_doctor()
        self.assertIn("parked", self.text(data, "OK"))

    # ---------------- OAuth findings ----------------
    def _expired(self, used, can_renew):
        """Fixture builder: an expired token, optionally with a refresh path,
        optionally backing a row. Returns (code, data)."""
        rows = [{"name": "api-row", "base": "https://a/v1", "model": "m",
                 "key": "k"}]
        if used:
            rows.append({"name": "nous-row", "base": "https://b/v1",
                         "model": "m", "oauth_provider": "nous"})
        self.healthy_install(rows=rows)
        prof = {"nous": {"label": "Nous", "class": "oauth"}}
        if can_renew:
            prof["nous"]["refresh_url"] = "https://refresh"
        json.dump(prof, open(mr.OAUTH_PROFILES_PATH, "w"))
        tok = {"access_token": "a", "expires_at": time.time() - 10}
        if can_renew:
            tok["refresh_token"] = "r"
        json.dump({"nous": tok}, open(mr.OAUTH_PATH, "w"))
        return self.run_doctor()

    def test_expired_token_behind_a_row_is_a_failure(self):
        """A request WILL break, so this is the only shape that earns FAIL."""
        code, data = self._expired(used=True, can_renew=False)
        self.assertIn("oauth:nous", self.names(data, "FAIL"))
        blob = self.text(data, "FAIL")
        self.assertIn("EXPIRED", blob)
        self.assertIn("cannot serve", blob)
        self.assertIn("interactive", blob.lower())
        self.assertEqual(code, 1)

    def test_expired_token_behind_a_row_with_a_refresh_path_says_refresh(self):
        code, data = self._expired(used=True, can_renew=True)
        blob = self.text(data, "FAIL")
        self.assertIn("EXPIRED", blob)
        self.assertIn("minimalrouter refresh nous", blob,
                      "when a headless refresh exists the remedy must name it")
        self.assertEqual(code, 1)

    def test_expired_token_on_an_unused_profile_is_a_warning_not_a_failure(self):
        """THE SEVERITY DEFECT, now pinned. Moon's real install had zero
        OAuth-backed rows and an expired `inference` token, and the first
        doctor run printed UNHEALTHY — '1 thing(s) will break requests' —
        while nothing could break. An unused dead credential is hygiene."""
        code, data = self._expired(used=False, can_renew=False)
        self.assertNotIn("oauth:nous", self.names(data, "FAIL"),
                         "no row depends on it, so no request breaks")
        self.assertIn("oauth:nous", self.names(data, "WARN"))
        blob = self.text(data, "WARN")
        self.assertIn("no row uses it", blob)
        self.assertEqual(code, 0, "unused hygiene must not fail the exit code")
        self.assertTrue(data["healthy"])

    def test_near_expiry_behind_a_row_is_a_warning(self):
        self.healthy_install(rows=[
            {"name": "nous-row", "base": "https://b/v1", "model": "m",
             "oauth_provider": "nous"}])
        json.dump({"nous": {"label": "Nous", "refresh_url": "https://r"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        json.dump({"nous": {"access_token": "a", "refresh_token": "r",
                            "expires_at": time.time() + 300}},
                  open(mr.OAUTH_PATH, "w"))
        code, data = self.run_doctor()
        blob = self.text(data, "WARN")
        self.assertIn("a row depends on it", blob)
        self.assertIn("minimalrouter refresh nous", blob)
        self.assertEqual(code, 0, "a warning is not a failure")

    def test_near_expiry_on_an_unused_profile_is_informational(self):
        self.healthy_install(rows=[{"name": "api-row", "base": "https://a/v1",
                                    "model": "m", "key": "k"}])
        json.dump({"nous": {"label": "Nous", "refresh_url": "https://r"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        json.dump({"nous": {"access_token": "a", "refresh_token": "r",
                            "expires_at": time.time() + 300}},
                  open(mr.OAUTH_PATH, "w"))
        code, data = self.run_doctor()
        self.assertEqual([c for c in self.findings(data, "FAIL")
                          if c["name"].startswith("oauth:")], [])
        self.assertEqual(self.oauth(data, "WARN"), [],
                         "unused and merely expiring soon is not even a warning")
        self.assertIn("no row uses it", self.text(data, "OK"))

    def test_unused_unsigned_profile_is_a_warning_not_a_failure(self):
        """Severity must mean "will break requests". A profile that was created
        and never signed in, while no row references it, is dead weight — the
        real install had exactly this (kilocode) and flagging it as a failure
        would train Moon to ignore the report."""
        json.dump({}, open(mr.OAUTH_PATH, "w"))
        json.dump({"nous": {"label": "Nous", "class": "oauth"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        code, data = self.run_doctor()
        self.assertIn("never signed in", self.text(data, "WARN"))
        self.assertNotIn("oauth:nous", self.names(data, "FAIL"))
        self.assertEqual(code, 0, "an unused profile breaks nothing")
        self.assertIn("oauth rm nous", self.text(data, "WARN"),
                      "the remedy must offer removal as well as sign-in")

    def test_signed_in_profile_used_by_a_row_is_a_failure(self):
        """The same shape, but a row points at it: now it really cannot serve."""
        self.healthy_install(rows=[
            {"name": "nous-row", "base": "https://a/v1", "model": "m",
             "oauth_provider": "nous"}])
        json.dump({}, open(mr.OAUTH_PATH, "w"))
        json.dump({"nous": {"label": "Nous", "class": "oauth"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        code, data = self.run_doctor()
        self.assertIn("oauth:nous", self.names(data, "FAIL"))
        self.assertIn("cannot serve", self.text(data, "FAIL"))
        self.assertEqual(code, 1)

    def test_sub_hour_expiry_prints_minutes_not_zero_hours(self):
        """A token alive for 45 minutes printed "0h left" — a number that reads
        as broken. Sub-hour shows minutes."""
        json.dump({"nous": {"access_token": "a", "refresh_token": "r",
                            "expires_at": time.time() + 2700}},
                  open(mr.OAUTH_PATH, "w"))
        json.dump({"nous": {"label": "Nous", "refresh_url": "https://r"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        code, data = self.run_doctor()
        blob = self.text(data, "OK")
        # the SHAPE is the invariant, not the digit: the clock advances between
        # the fixture write and the read, so 2700s can legitimately read as 44m.
        self.assertRegex(blob, r"\b\d+m left\b",
                         "sub-hour expiry must print minutes, got: %s" % blob)
        self.assertNotIn("0h left", blob)
        self.assertNotRegex(blob, r"\b\d+h left\b",
                            "45 minutes must not be rounded to hours")

    def test_row_referencing_a_missing_profile_is_a_failure(self):
        """A registry PRESET is not a profile — the incident that makes a new
        user think the row is broken when it was never created."""
        self.healthy_install(rows=[
            {"name": "nous-x", "base": "https://a/v1", "model": "m",
             "oauth_provider": "nous"}])
        code, data = self.run_doctor()
        blob = self.text(data, "FAIL")
        self.assertIn("unknown profile", blob)
        self.assertIn("PRESET", blob, "the fix text must name the real trap")

    def test_unused_credential_gets_one_line_not_two(self):
        """The refresh-path note used to fire as a SECOND warning for the same
        unused credential, so one hygiene item cost two lines to read. It now
        earns its own line only when a row depends on it."""
        self.healthy_install(rows=[{"name": "api-row", "base": "https://a/v1",
                                    "model": "m", "key": "k"}])
        json.dump({"nous": {"label": "Nous", "class": "oauth"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        json.dump({"nous": {"access_token": "a",
                            "expires_at": time.time() - 10}},
                  open(mr.OAUTH_PATH, "w"))
        code, data = self.run_doctor()
        self.assertEqual(len(self.oauth(data, "WARN")), 1,
                         "one unused dead credential = one line, got %s"
                         % [c["detail"] for c in self.oauth(data, "WARN")])
        self.assertEqual(code, 0)

    def test_refresh_path_note_fires_when_a_row_depends(self):
        self.healthy_install(rows=[
            {"name": "nous-row", "base": "https://b/v1", "model": "m",
             "oauth_provider": "nous"}])
        json.dump({"nous": {"label": "Nous", "class": "oauth"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        json.dump({"nous": {"access_token": "a",
                            "expires_at": time.time() + 9e4}},
                  open(mr.OAUTH_PATH, "w"))
        code, data = self.run_doctor()
        self.assertIn("no refresh path stored", self.text(data, "WARN"))

    def test_clobbered_registry_device_fields_are_caught(self):
        """`oauth add` rewrites provider-registry.json to a minimal shape; the
        device-flow fields vanish and an expired profile can never be re-signed
        in. This is the recorded trap the rich copy has to be restored from."""
        json.dump({"providers": {"nous": {"label": "Nous",
                                          "base_url": "https://b"}}},
                  open(mr.PROVIDER_REGISTRY_PATH, "w"))
        json.dump({"nous": {"access_token": "a", "expires_at": time.time() + 9e4}},
                  open(mr.OAUTH_PATH, "w"))
        json.dump({"nous": {"label": "Nous", "class": "oauth"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        code, data = self.run_doctor()
        blob = self.text(data, "WARN")
        self.assertIn("device-flow fields", blob)
        self.assertIn("oauth add", blob, "the fix text must name the cause")

    def test_zcode_row_without_the_bridge_asset_is_a_failure(self):
        """Without bridges/zcode-identity.json the vendor answers 405 code
        3012 — the exact hole PR #1's installer left."""
        self.healthy_install(rows=[
            {"name": "z", "base": "https://a/v1", "model": "m",
             "oauth_provider": "zcode"}])
        json.dump({"zcode": {"access_token": "a",
                             "expires_at": time.time() + 9e4}},
                  open(mr.OAUTH_PATH, "w"))
        json.dump({"zcode": {"label": "Z", "class": "oauth"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        code, data = self.run_doctor()
        self.assertIn("zcode bridge", self.names(data, "FAIL"))
        self.assertIn("3012", self.text(data, "FAIL"))

    # ---------------- prefs + themes ----------------
    def test_theme_keys_parse_without_importing_textual(self):
        """doctor has to work when Textual is missing — that is often why it was
        called — so the theme list is parsed from source, not imported."""
        before = set(sys.modules)
        keys = mr._theme_keys()
        self.assertNotIn("textual", set(sys.modules) - before,
                         "_theme_keys must not drag textual into the process")
        self.assertGreaterEqual(len(keys), 14)
        for k in ("midnight", "nous", "claude"):
            self.assertIn(k, keys)

    def test_a_dead_theme_in_prefs_is_a_warning(self):
        json.dump({"theme": "bogus-theme", "dense": False, "motion": True},
                  open(os.path.join(mr.MR_HOME, "dashboard.json"), "w"))
        code, data = self.run_doctor()
        self.assertIn("does not exist", self.text(data, "WARN"))
        self.assertEqual(code, 0)

    def test_corrupt_prefs_warn_and_name_the_delete(self):
        open(os.path.join(mr.MR_HOME, "dashboard.json"), "w").write("{{")
        code, data = self.run_doctor()
        self.assertIn("unparseable", self.text(data, "WARN"))
        self.assertIn("rm ", self.text(data, "WARN"))

    def test_missing_prefs_is_fine(self):
        os.remove(os.path.join(mr.MR_HOME, "dashboard.json"))
        code, data = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertIn("defaults", self.text(data, "OK"))

    # ---------------- runtime ----------------
    def test_foreign_listener_on_the_port_is_a_failure(self):
        s = socket.socket()
        s.bind(("127.0.0.1", mr.PORT))
        s.listen(1)
        try:
            code, data = self.run_doctor()
            self.assertIn("port %d" % mr.PORT, self.names(data, "FAIL"))
            self.assertIn("NOT this router", self.text(data, "FAIL"))
            self.assertEqual(code, 1)
        finally:
            s.close()

    def test_stale_pidfile_is_a_warning_and_survives_without_fix(self):
        # a pid that cannot exist: reserved range
        open(mr.PID_PATH, "w").write("4194304")
        code, data = self.run_doctor()
        self.assertIn("stale", self.text(data, "WARN"))
        self.assertTrue(os.path.exists(mr.PID_PATH),
                        "a diagnostic must not delete anything unasked")
        code2, data2 = self.run_doctor(fix=True)
        self.assertFalse(os.path.exists(mr.PID_PATH),
                         "--fix clears the stale pidfile")
        self.assertIn("removed the stale pidfile", self.text(data2, "OK"))
        self.assertEqual(code2, 0)

    def test_fix_never_touches_user_data(self):
        """rows.json / oauth.json / dashboard.json hold his data and his LIVE
        preferences. A diagnostic that overwrites them creates a second
        incident — dense:true was set by him, not by a bug."""
        before_rows = open(mr.ROWS_PATH, "rb").read()
        pref = os.path.join(mr.MR_HOME, "dashboard.json")
        json.dump({"theme": "midnight", "dense": True, "motion": True},
                  open(pref, "w"))
        before_pref = open(pref, "rb").read()
        open(mr.PID_PATH, "w").write("4194304")
        code, data = self.run_doctor(fix=True)
        self.assertEqual(open(mr.ROWS_PATH, "rb").read(), before_rows,
                         "--fix must not rewrite rows.json")
        self.assertEqual(open(pref, "rb").read(), before_pref,
                         "--fix must not reset a saved preference")

    def test_group_readable_secrets_are_flagged_and_fixed(self):
        os.chmod(mr.ROWS_PATH, 0o644)
        code, data = self.run_doctor()
        self.assertIn("perms · rows.json", self.names(data, "WARN"))
        self.assertIn("600", self.text(data, "WARN"))
        code2, data2 = self.run_doctor(fix=True)
        self.assertEqual(os.stat(mr.ROWS_PATH).st_mode & 0o777, 0o600)
        self.assertIn("chmod 600", self.text(data2, "OK"))

    def test_server_version_drift_is_reported(self):
        """The board shows a version; the code answering requests may be
        another one after an un-restarted update."""
        payload = json.dumps({"rows": [], "version": "0.0.1",
                              "stats": {"serving": 0, "down": 0, "rows": 0},
                              "models": 0}).encode()

        class Resp:
            """A urlopen-shaped stub: doctor uses it as a context manager and
            calls .read(). No BytesIO inheritance — that was the first draft's
            bug (it passed payload twice and read() ignored the buffer)."""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return payload

        def spy(url, *a, **k):
            u = url if isinstance(url, str) else getattr(url, "full_url", "")
            if "/api/state" in u:
                return Resp()
            raise mr.urllib.error.URLError("no models endpoint in test")
        with mock.patch.object(mr.urllib.request, "urlopen", spy):
            code, data = self.run_doctor()
        blob = self.text(data, "WARN")
        self.assertIn("serving v0.0.1", blob)
        self.assertIn("file on disk", blob)

    def test_bad_minimalrouter_tui_value_is_a_warning(self):
        with mock.patch.dict(os.environ, {"MINIMALROUTER_TUI": "pretty"}):
            code, data = self.run_doctor()
        self.assertIn("MINIMALROUTER_TUI", self.names(data, "WARN"))

    def test_fail_is_only_ever_claimed_for_something_that_breaks_requests(self):
        """Structural guard on the severity law: every FAIL must name a row, a
        key, a port, a file the engine must read, or a schema problem. A FAIL
        that names only a credential nobody routes through is the defect class
        this release fixed."""
        code, data = self.run_doctor()
        for c in self.findings(data, "FAIL"):
            blob = (c["name"] + " " + c["detail"] + " " + c["fix"]).lower()
            self.assertTrue(
                any(t in blob for t in ("row", "key", "port", "rows.json",
                                       "oauth.json", "schema", "base", "model",
                                       "registry", "cannot serve", "recursion")),
                "FAIL with no request-breaking subject: %r" % c)

    def test_ok_rows_carry_no_remedy_text(self):
        """The spec's negative invariant, asserted both ways.

        A remedy line means ACT HERE. If OK findings carry one, the report
        loses the distinction between informational and actionable. The
        positive half matters as much: stripping every remedy from the tool
        would satisfy the negative half while destroying the answer.
        """
        code, data = self.run_doctor()
        for c in self.findings(data, "OK"):
            self.assertEqual(c["fix"].strip(), "",
                             "%s: an OK finding must not carry remedy text, "
                             "got %r" % (c["name"], c["fix"]))
        # STATIC half, and it is load-bearing. The runtime half only sees the
        # branches the current environment reaches — e.g. the from-checkout
        # launcher row runs only when the engine sits in a git checkout, so a
        # sabotage copy outside one passed vacuously. This scans the SOURCE, so
        # the invariant holds regardless of where the file lives.
        import ast as _ast
        src = Path(ENGINE).read_text()
        tree = _ast.parse(src)
        offenders = []
        for node in _ast.walk(tree):
            if not (isinstance(node, _ast.Call)
                    and getattr(node.func, "id", "") == "add"):
                continue
            if len(node.args) < 4:
                continue
            if not (isinstance(node.args[0], _ast.Constant)
                    and node.args[0].value == "OK"):
                continue
            fix = node.args[3]
            if isinstance(fix, _ast.Constant) and fix.value:
                offenders.append((node.lineno, str(fix.value)[:60]))
        self.assertEqual(offenders, [],
                         "OK findings carrying remedy text at %s — a remedy "
                         "line means ACT HERE" % offenders)
        # positive control: a real failure still carries its remedy
        self.healthy_install(rows=[
            {"name": "loop", "base": "http://127.0.0.1:%d/v1" % mr.PORT,
             "model": "m", "key": "k"}])
        code2, data2 = self.run_doctor()
        self.assertTrue(any(c["fix"].strip() for c in data2["checks"]),
                        "a FAIL must still carry its remedy")

    def test_every_finding_carries_a_fix_line(self):
        """A diagnosis without a next step is a status report, not a doctor."""
        self.healthy_install(rows=[
            {"name": "loop", "base": "http://127.0.0.1:%d/v1" % mr.PORT,
             "model": "m", "key": "k"}])
        os.chmod(mr.ROWS_PATH, 0o644)
        code, data = self.run_doctor()
        for c in data["checks"]:
            if c["level"] in ("FAIL", "WARN"):
                self.assertTrue(c["fix"].strip(),
                                "%s (%s) has no remedy text"
                                % (c["name"], c["level"]))

    def test_a_healthy_report_is_not_vacuously_green(self):
        """The real risk in a self-check tool is a report that says nothing.
        A healthy install must still enumerate what it actually inspected, so
        "0 fail" means "20 things checked and passed", not "nothing ran"."""
        code, data = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertGreaterEqual(data["ok"], 12,
                                "a healthy install reported only %d checks"
                                % data["ok"])
        self.assertEqual(data["ok"] + data["warn"] + data["fail"],
                         len(data["checks"]),
                         "the counts must partition the findings")
        names = [c["name"] for c in data["checks"]]
        for must in ("engine", "row schema", "key resolution", "prefs",
                     "port %d" % mr.PORT, "pidfile", "log"):
            self.assertIn(must, names, "doctor skipped %s entirely" % must)

    def test_doctor_runs_against_the_real_install_without_crashing(self):
        """The synthetic fixtures prove the logic; this proves the tool. It runs
        against the live home read-only and must return a valid exit code and
        parseable JSON whatever the estate state is."""
        import io
        from contextlib import redirect_stdout
        saved = {a: getattr(mr, a) for a in
                 ("MR_HOME", "ROWS_PATH", "OAUTH_PATH", "OAUTH_PROFILES_PATH",
                  "PROVIDER_REGISTRY_PATH", "ZCODE_IDENTITY_PATH", "PID_PATH",
                  "LOG_PATH", "PORT", "HOME", "ENV")}
        mr.PORT = free_port()      # never touch a server Moon may be running
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = mr.doctor(fix=False, as_json=True)
            data = json.loads(buf.getvalue())
        finally:
            for a, v in saved.items():
                setattr(mr, a, v)
        self.assertIn(code, (0, 1))
        self.assertTrue(data["checks"], "the real install produced no findings")
        for c in data["checks"]:
            self.assertIn(c["level"], ("OK", "WARN", "FAIL"))

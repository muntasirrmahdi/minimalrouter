"""Offline unit tests for MinimalRouter — stdlib unittest, zero network.

Run: python -m unittest discover -s tests -v
Sabotage check (a regression test must fail pre-fix):
    MR_TEST_ENGINE=/path/to/pre-fix/minimalrouter.py python -m unittest discover -s tests

Regression tests carry the defect id they bite (D1-D4, full audit 2026-11-19).
"""
import importlib.util
import email.message
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
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
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
                {"state": {"status": "fail", "error": "HTTP Error 429: x",
                           "retry_at": time.time() + 60}},
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
        now = time.time()
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "HTTP Error 429: Too Many Requests",
             "fail_streak": 1, "retry_at": now + 5}, now), "cooldown")

    def test_a_429_without_a_clock_is_not_benched_forever(self):
        """v1.8.0: the cooldown is the clock, not the status code."""
        self.assertEqual(mr.classify_status(
            {"status": "fail", "error": "HTTP Error 429: Too Many Requests",
             "fail_streak": 1}, time.time()), "error")

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
            ok, ms, err, content, tokens, q = mr.probe("https://x/v1", "k", "m")
            self.assertTrue(ok)
            self.assertEqual(tokens, 42)
            self.assertEqual(q, {}, "a response with no .headers degrades to no meter")
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
            ok, ms, err, content, tokens, q = mr.probe("https://x/v1", "k", "m")
            self.assertTrue(ok)
            self.assertEqual(tokens, 0)  # honest floor, never an estimate
            self.assertEqual(q, {})
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
                     "LOG_PATH", "ENV", "PORT", "HOME", "DOCTOR_HISTORY_PATH"):
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
        mr.DOCTOR_HISTORY_PATH = os.path.join(home, "doctor-history.jsonl")
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
                  "LOG_PATH", "PORT", "HOME", "ENV", "DOCTOR_HISTORY_PATH")}
        mr.PORT = free_port()      # never touch a server Moon may be running
        _hist_tmp = tempfile.TemporaryDirectory(prefix="mr-doctor-history-")
        mr.DOCTOR_HISTORY_PATH = os.path.join(_hist_tmp.name,
                                              "doctor-history.jsonl")
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = mr.doctor(fix=False, as_json=True)
            data = json.loads(buf.getvalue())
        finally:
            _hist_tmp.cleanup()
            for a, v in saved.items():
                setattr(mr, a, v)
        self.assertIn(code, (0, 1))
        self.assertTrue(data["checks"], "the real install produced no findings")
        for c in data["checks"]:
            self.assertIn(c["level"], ("OK", "WARN", "FAIL"))

    # ---------------- v1.7.0: catalogue, coverage, repairs ----------------
    def _diag(self, data):
        return [c for c in data["checks"] if c.get("kind") == "check"]

    def test_every_diagnostic_check_has_a_stable_id_and_category(self):
        code, data = self.run_doctor()
        for c in self._diag(data):
            self.assertIn(c["id"], mr.DOCTOR_CATALOGUE,
                          "%r has no catalogue id" % c["name"])
            self.assertIn(c["category"], mr.DOCTOR_CATEGORY_LABELS,
                          "%r has no category" % c["name"])

    def test_catalogue_fixable_set_is_pinned(self):
        """The coverage number is only honest while this set is deliberate."""
        auto = {cid for cid, meta in mr.DOCTOR_CATALOGUE.items()
                if meta.get("fixable")}
        self.assertEqual(auto, {
            "dashboard-module", "launcher-pin", "launcher-exec",
            "zcode-bridge", "registry-fields", "pidfile", "perms",
        })
        self.assertEqual(len(mr.DOCTOR_CATALOGUE), 24)

    def test_json_shape_carries_the_fixable_flag(self):
        code, data = self.run_doctor()
        self.assertIn("coverage", data)
        for k in ("catalogue", "observed", "weighted", "by_category"):
            self.assertIn(k, data["coverage"])
        for c in data["checks"]:
            for k in ("id", "category", "fixable", "auto_fixed", "kind"):
                self.assertIn(k, c)
            self.assertIsInstance(c["fixable"], bool)
            self.assertIsInstance(c["auto_fixed"], bool)

    def test_observed_coverage_counts_this_run_only(self):
        """A perms finding is auto-fixable; a dead theme is not."""
        self.healthy_install()
        os.chmod(mr.ROWS_PATH, 0o644)
        json.dump({"theme": "no-such-theme", "dense": False},
                  open(os.path.join(mr.MR_HOME, "dashboard.json"), "w"))
        code, data = self.run_doctor()
        obs = data["coverage"]["observed"]
        # The fake home always carries a PATH warning; perms + prefs are the
        # two findings this test introduces.
        self.assertEqual(obs["findings"], 3)
        self.assertEqual(obs["auto_fixable"], 1)
        self.assertEqual(obs["pct"], round(100.0 / 3.0, 1))

    def test_history_is_private_bounded_and_weighted(self):
        hist_path = mr.DOCTOR_HISTORY_PATH
        for i in range(mr.DOCTOR_HISTORY_MAX + 25):
            mr._doctor_history_append("1.7.0", [
                {"id": "perms", "level": "WARN", "fixable": True},
                {"id": "prefs", "level": "WARN", "fixable": False},
            ])
        with open(hist_path, encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        self.assertEqual(len(lines), mr.DOCTOR_HISTORY_MAX)
        blob = "\n".join(lines)
        self.assertNotIn("detail", blob)
        self.assertNotIn("/home", blob)
        self.assertNotIn("rows.json", blob)
        hist = mr._doctor_history_read()
        self.assertEqual(len(hist), mr.DOCTOR_HISTORY_MAX)
        w = mr._doctor_coverage([], hist)["weighted"]
        self.assertEqual(w["runs"], mr.DOCTOR_HISTORY_MAX)
        self.assertEqual(w["findings"], mr.DOCTOR_HISTORY_MAX * 2)
        self.assertEqual(w["auto_fixable"], mr.DOCTOR_HISTORY_MAX)
        self.assertEqual(w["pct"], 50.0)

    def test_first_run_reports_weighted_coverage_from_its_own_history(self):
        code, data = self.run_doctor()
        w = data["coverage"]["weighted"]
        obs = data["coverage"]["observed"]
        self.assertIsNotNone(w)
        self.assertEqual(w["runs"], 1)
        self.assertEqual(w["findings"], obs["findings"])
        self.assertEqual(w["auto_fixable"], obs["auto_fixable"])
        self.assertEqual(w["pct"], obs["pct"])

    def test_source_add_names_all_resolve_to_the_catalogue(self):
        """A new check cannot ship without metadata, and metadata cannot rot."""
        import ast as _ast
        src = Path(mr.__file__).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, _ast.FunctionDef) and n.name == "doctor")
        allowed = {
            "fix", "fix applied",
            "oauth:%s", "perms \u00b7 %s", "port %d",
        }
        for node in _ast.walk(fn):
            if not (isinstance(node, _ast.Call)
                    and isinstance(node.func, _ast.Name)
                    and node.func.id == "add"):
                continue
            arg = node.args[1]
            if isinstance(arg, _ast.Constant):
                name = arg.value
            elif isinstance(arg, _ast.BinOp) and isinstance(arg.left, _ast.Constant):
                name = arg.left.value
            else:
                self.fail("doctor add() has a non-literal check name at line %d"
                          % node.lineno)
            if name in allowed:
                continue
            self.assertIn(mr._doctor_check_id(name), mr.DOCTOR_CATALOGUE,
                          "add(%r) has no catalogue id" % name)

    def test_repair_outcomes_do_not_inflate_coverage(self):
        self.healthy_install()
        os.chmod(mr.ROWS_PATH, 0o644)
        code, data = self.run_doctor(fix=True)
        repairs = [c for c in data["checks"] if c.get("kind") == "repair"]
        self.assertTrue(repairs)
        self.assertTrue(all(c["auto_fixed"] for c in repairs))
        obs = data["coverage"]["observed"]
        # PATH is the other standing WARN on a fake home; it is not auto-fixable.
        self.assertEqual(obs["findings"], 2)
        self.assertEqual(obs["auto_fixable"], 1)

    def test_fix_restores_a_missing_dashboard_module(self):
        dash = os.path.join(self.home, "minimalrouter_dashboard.py")
        with mock.patch.object(mr, "_dash_sibling", return_value=dash):
            code, data = self.run_doctor()
            self.assertIn("dashboard module", self.names(data, "WARN"))
            code2, data2 = self.run_doctor(fix=True)
            self.assertTrue(os.path.exists(dash))
            self.assertIn("dashboard module", self.names(data2, "WARN"),
                          "the fix run must still report what it found")
            self.assertIn("fix applied", self.names(data2, "OK"))
            code3, data3 = self.run_doctor()
            self.assertNotIn("dashboard module", self.names(data3, "WARN"))

    def test_fix_restores_the_zcode_identity_asset(self):
        json.dump([{"name": "z", "base": "https://z/v1", "model": "m",
                    "oauth_provider": "zcode"}], open(mr.ROWS_PATH, "w"))
        json.dump({"zcode": {"label": "Z"}}, open(mr.OAUTH_PROFILES_PATH, "w"))
        json.dump({"zcode": {"access_token": "a", "expires_at": time.time() + 9999}},
                  open(mr.OAUTH_PATH, "w"))
        os.chmod(mr.OAUTH_PATH, 0o600)
        if os.path.exists(mr.ZCODE_IDENTITY_PATH):
            os.remove(mr.ZCODE_IDENTITY_PATH)
        code, data = self.run_doctor()
        self.assertIn("zcode bridge", self.names(data, "FAIL"))
        code2, data2 = self.run_doctor(fix=True)
        self.assertTrue(os.path.exists(mr.ZCODE_IDENTITY_PATH))
        with open(mr.ZCODE_IDENTITY_PATH, "rb") as f:
            self.assertEqual(len(f.read()), mr.ZCODE_IDENTITY_BYTES)
        code3, data3 = self.run_doctor()
        self.assertNotIn("zcode bridge", self.names(data3, "FAIL"))

    def test_fix_repairs_the_launcher_exec_bit(self):
        binp = mr._launcher_path()
        os.makedirs(os.path.dirname(binp), exist_ok=True)
        with open(binp, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nexec python3 "%s/minimalrouter.py" "$@"\n'
                    % mr.MR_HOME)
        os.chmod(binp, 0o644)
        code, data = self.run_doctor()
        self.assertIn("launcher exec bit", self.names(data, "WARN"))
        code2, data2 = self.run_doctor(fix=True)
        self.assertTrue(os.access(binp, os.X_OK))
        code3, data3 = self.run_doctor()
        self.assertNotIn("launcher exec bit", self.names(data3, "WARN"))

    def test_fix_repins_a_bare_python_launcher_when_the_venv_exists(self):
        binp = mr._launcher_path()
        os.makedirs(os.path.dirname(binp), exist_ok=True)
        with open(binp, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nexec python3 "%s/minimalrouter.py" "$@"\n'
                    % mr.MR_HOME)
        os.chmod(binp, 0o755)
        venv = mr._launcher_venv_python()
        os.makedirs(os.path.dirname(venv), exist_ok=True)
        with open(venv, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(venv, 0o755)
        code, data = self.run_doctor()
        self.assertIn("launcher", self.names(data, "WARN"))
        code2, data2 = self.run_doctor(fix=True)
        with open(binp, encoding="utf-8") as f:
            self.assertIn('exec "%s"' % venv, f.read())
        code3, data3 = self.run_doctor()
        self.assertNotIn("launcher", self.names(data3, "WARN"))

    def test_fix_restores_registry_device_flow_fields(self):
        json.dump({"providers": {"nous": {"label": "Nous",
                                          "base_url": "https://portal.nousresearch.com",
                                          "models": ["x"]}}},
                  open(mr.PROVIDER_REGISTRY_PATH, "w"))
        json.dump({"nous": {"label": "Nous", "refresh_url": "https://r"}},
                  open(mr.OAUTH_PROFILES_PATH, "w"))
        code, data = self.run_doctor()
        self.assertIn("provider registry", self.names(data, "WARN"))
        code2, data2 = self.run_doctor(fix=True)
        reg = mr.read_provider_registry()
        self.assertIn("authorize_url", reg["nous"])
        self.assertIn("token_url", reg["nous"])
        code3, data3 = self.run_doctor()
        self.assertNotIn("provider registry", self.names(data3, "WARN"))

    def test_user_data_files_survive_every_repair(self):
        self.healthy_install()
        binp = mr._launcher_path()
        os.makedirs(os.path.dirname(binp), exist_ok=True)
        with open(binp, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nexec python3 "%s/minimalrouter.py" "$@"\n'
                    % mr.MR_HOME)
        os.chmod(binp, 0o644)
        os.chmod(mr.ROWS_PATH, 0o644)
        before_rows = open(mr.ROWS_PATH, "rb").read()
        before_oauth = open(mr.OAUTH_PATH, "rb").read()
        prefs = os.path.join(mr.MR_HOME, "dashboard.json")
        before_prefs = open(prefs, "rb").read()
        dash = os.path.join(self.home, "minimalrouter_dashboard.py")
        with mock.patch.object(mr, "_dash_sibling", return_value=dash):
            code, data = self.run_doctor(fix=True)
        self.assertEqual(open(mr.ROWS_PATH, "rb").read(), before_rows)
        self.assertEqual(open(mr.OAUTH_PATH, "rb").read(), before_oauth)
        self.assertEqual(open(prefs, "rb").read(), before_prefs)

    def test_a_failed_repair_reports_and_does_not_crash(self):
        self.healthy_install()
        dash = os.path.join(self.home, "minimalrouter_dashboard.py")
        with mock.patch.object(mr, "_dash_sibling", return_value=dash):
            with mock.patch.object(mr, "_restore_dashboard_module",
                                   side_effect=OSError("offline")):
                code, data = self.run_doctor(fix=True)
        self.assertEqual(code, 0)
        blob = self.text(data, "WARN")
        self.assertIn("dashboard module not restored", blob)
        self.assertIn("re-run install.sh", blob)

    def test_merge_registry_entry_preserves_device_flow_fields(self):
        reg = {"nous": {"label": "Nous", "base_url": "https://old",
                        "models": ["old"], "authorize_url": "https://a",
                        "token_url": "https://t", "poll_method": "GET",
                        "note": "keep me"}}
        out = mr.merge_registry_entry(reg, "nous", "Nous Portal",
                                      "https://new", ["new"], "responses")
        self.assertEqual(out["nous"]["authorize_url"], "https://a")
        self.assertEqual(out["nous"]["token_url"], "https://t")
        self.assertEqual(out["nous"]["poll_method"], "GET")
        self.assertEqual(out["nous"]["note"], "keep me")
        self.assertEqual(out["nous"]["base_url"], "https://new")
        self.assertEqual(out["nous"]["api_mode"], "responses")

    def test_fix_alias_and_coverage_flag_are_documented(self):
        u = mr.usage()
        self.assertIn("minimalrouter fix", u)
        self.assertIn("minimalrouter doctor --coverage", u)
        self.assertIn("minimalrouter fix", Path(mr.__file__).read_text(encoding="utf-8").split("def usage(", 1)[0])


class TestPoolLearnsAcrossRequests(unittest.TestCase):
    """v1.5.0: the pool must carry what it learned from the last request.

    Defect measured on 100milionseek.top, 2026-11-24: four accounts behind one
    router, `enabled_rows()` filtering only on `disabled` and the chain walked in
    plain row order. Failover worked WITHIN a request, so a request survived a
    429 — but nothing crossed over, so the next request re-struck the same
    exhausted account first. The pool re-derived the same dead end on every call.
    """

    # ---- pure: order_chain ----

    def _st(self, err, streak=1, status="fail", at=None, retry_at=0):
        return {"status": status, "error": err, "fail_streak": streak,
                "at": at if at is not None else time.time(),
                "retry_at": retry_at}

    def test_a_cooldown_row_moves_behind_a_healthy_row(self):
        now = time.time()
        chain = [{"name": "a", "model": "m"}, {"name": "b", "model": "m"}]
        state = {"a": self._st("HTTP Error 429: rate limited",
                               retry_at=now + 60),
                 "b": self._st("", status="ok")}
        out = [r["name"] for r in mr.order_chain(chain, state, now)]
        self.assertEqual(out, ["b", "a"],
                         "the row the upstream just 429'd must not take the next request")

    def test_deferral_covers_every_state_the_upstream_declared(self):
        """429 cooldown, 402 empty, 403 gated and a dead streak are all answers
        the upstream GAVE us. Each must defer, and each must defer for the same
        reason: classify_status already knows them."""
        now = time.time()
        chain = [{"name": n, "model": "m"} for n in
                 ("cool", "empty", "gated", "dead", "good")]
        state = {"cool": self._st("HTTP Error 429", retry_at=now + 60),
                 "empty": self._st("HTTP Error 402"),
                 "gated": self._st("HTTP Error 403"),
                 "dead": self._st("HTTP Error 500", streak=mr.FAIL_STREAK_DEAD),
                 "good": self._st("", status="ok")}
        out = [r["name"] for r in mr.order_chain(chain, state, now)]
        self.assertEqual(out[0], "good")
        self.assertEqual(set(out[1:]), {"cool", "empty", "gated", "dead"})

    def test_an_uncertain_row_stays_in_line_and_an_untried_row_gets_tried(self):
        """`error` and `stale` are not deferrals: the row might still serve, and a
        never-probed row reads `unknown` at boot. Deferring those would starve a
        pool whose state has simply not been learned yet."""
        now = time.time()
        chain = [{"name": "err", "model": "m"}, {"name": "stale", "model": "m"},
                 {"name": "untried", "model": "m"}, {"name": "cool", "model": "m"}]
        state = {"err": self._st("HTTP Error 500"),
                 "stale": self._st("", status="ok", at=now - mr.STALE_AFTER - 1),
                 "cool": self._st("HTTP Error 429", retry_at=now + 60)}
        out = [r["name"] for r in mr.order_chain(chain, state, now)]
        self.assertEqual(out[:3], ["err", "stale", "untried"],
                         "only upstream-declared states may defer")
        self.assertEqual(out[3], "cool")

    def test_partition_never_filter_a_wholly_exhausted_pool_keeps_its_order(self):
        """The load-bearing property. A daily-allowance pool where every account
        is spent must still be TRIED, because waiting for the reset is the answer
        and 502-without-trying is not. So this is a stable partition, not a filter."""
        now = time.time()
        chain = [{"name": "a", "model": "m"}, {"name": "b", "model": "m"},
                 {"name": "c", "model": "m"}]
        state = {n: self._st("HTTP Error 429", retry_at=now + 60)
                 for n in ("a", "b", "c")}
        out = [r["name"] for r in mr.order_chain(chain, state, now)]
        self.assertEqual(out, ["a", "b", "c"],
                         "all-deferred must preserve order and drop nothing")
        self.assertEqual(len(out), len(chain))

    def test_relative_order_is_stable_within_each_group(self):
        now = time.time()
        chain = [{"name": n, "model": "m"} for n in ("a", "b", "c", "d", "e")]
        state = {"b": self._st("HTTP Error 429", retry_at=now + 60),
                 "d": self._st("HTTP Error 429", retry_at=now + 60),
                 "a": self._st("", status="ok"),
                 "c": self._st("", status="ok"),
                 "e": self._st("", status="ok")}
        out = [r["name"] for r in mr.order_chain(chain, state, now)]
        self.assertEqual(out, ["a", "c", "e", "b", "d"],
                         "row order inside each group is the operator's configured order")

    # ---- pure: quota_from_headers ----

    def test_quota_headers_are_picked_ints_and_nothing_else(self):
        hdrs = {"X-Quota-Limit": "120000000", "x-quota-used": "18159",
                "X-Quota-Remaining": "119981841", "X-Qos": "full",
                "Retry-After": "30512", "Content-Type": "application/json",
                "Set-Cookie": "session=abc"}
        out = mr.quota_from_headers(hdrs)
        self.assertEqual(out["x-quota-limit"], 120000000)
        self.assertEqual(out["x-quota-used"], 18159)
        self.assertEqual(out["x-quota-remaining"], 119981841)
        self.assertEqual(out["retry-after"], 30512)
        self.assertEqual(out["x-qos"], "full", "a non-numeric meter stays a string")
        self.assertNotIn("content-type", out)
        self.assertNotIn("set-cookie", out,
                         "a credential-bearing header must never land in row state")

    def test_absent_or_broken_headers_yield_nothing_not_a_crash(self):
        self.assertEqual(mr.quota_from_headers(None), {})
        self.assertEqual(mr.quota_from_headers({}), {})
        self.assertEqual(mr.quota_from_headers("not-a-header-object"), {})

    # ---- record_state carries the meter, and never shows a stale one ----

    def test_record_state_stores_quota_and_a_later_call_without_one_clears_it(self):
        name = "quota-row"
        mr.ROW_STATE.pop(name, None)
        try:
            mr.record_state(name, "ok", 100, quota={"x-quota-remaining": 5})
            self.assertEqual(mr.ROW_STATE[name]["quota"], {"x-quota-remaining": 5})
            mr.record_state(name, "ok", 100)
            self.assertEqual(mr.ROW_STATE[name]["quota"], {},
                             "quota describes the LAST response; a carried-over "
                             "number from a previous call would be a lie on the board")
        finally:
            mr.ROW_STATE.pop(name, None)

    def test_a_row_without_a_meter_gets_an_empty_quota_not_a_missing_key(self):
        """Every row carries the key, so /api/state consumers never branch on
        absence — an upstream with no meter reads {} , which is an answer."""
        name = "no-meter-row"
        mr.ROW_STATE.pop(name, None)
        try:
            mr.record_state(name, "ok", 50)
            self.assertIn("quota", mr.ROW_STATE[name])
            self.assertEqual(mr.ROW_STATE[name]["quota"], {})
        finally:
            mr.ROW_STATE.pop(name, None)

    # ---- per-row timeout ----

    def test_a_generic_call_uses_the_row_timeout(self):
        """TIMEOUT is a default, not a ceiling. Measured: an upstream that HANGS
        instead of returning 429 at its session limit turns 90s into a PER-ROW
        multiplier — four gated rows is 360s before the caller sees anything."""
        seen = {}

        class FakeResp(object):
            status = 200
            headers = {}

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return FakeResp()

        real = mr.urllib.request.urlopen
        try:
            mr.urllib.request.urlopen = fake_urlopen
            # the client path: no timeout argument, so the row decides
            mr.upstream("https://example.test/v1", "k", "/v1/chat/completions",
                        payload={"model": "m"}, row={"name": "slow", "timeout": 20})
            self.assertEqual(seen["timeout"], 20,
                             "the row's own timeout must govern a generic call")
            mr.upstream("https://example.test/v1", "k", "/v1/chat/completions",
                        payload={"model": "m"}, row={"name": "plain"})
            self.assertEqual(seen["timeout"], mr.TIMEOUT,
                             "a row with no timeout keeps the global default")
        finally:
            mr.urllib.request.urlopen = real

    def test_an_explicit_call_budget_beats_the_row_so_boot_cannot_be_blown_out(self):
        """The property I got wrong on the first pass. probe() passes PROBE_TIMEOUT
        deliberately; if the row always won, a row declaring timeout 600 would
        stretch every boot probe to 600s and stall the whole pool. An explicit
        caller value is a decision for that call path, so it wins."""
        seen = {}

        class FakeResp(object):
            status = 200
            headers = {}

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return FakeResp()

        real = mr.urllib.request.urlopen
        try:
            mr.urllib.request.urlopen = fake_urlopen
            mr.upstream("https://example.test/v1", "k", "/v1/chat/completions",
                        payload={"model": "m"}, timeout=mr.PROBE_TIMEOUT,
                        row={"name": "greedy", "timeout": 600})
            self.assertEqual(seen["timeout"], mr.PROBE_TIMEOUT,
                             "a row may not lengthen a caller's explicit budget")
        finally:
            mr.urllib.request.urlopen = real

    def test_a_row_may_shorten_its_probe_budget(self):
        """The other direction is legitimate: a fast-failing row can tighten the
        probe so a dead upstream is abandoned quickly."""
        seen = {}

        class FakeResp(object):
            status = 200
            headers = {}

            def read(self):
                return b'{"choices":[{"message":{"content":"OK"}}]}'

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return FakeResp()

        real = mr.urllib.request.urlopen
        try:
            mr.urllib.request.urlopen = fake_urlopen
            mr.probe("https://example.test/v1", "k", "m",
                     row={"name": "tight", "timeout": 7})
            self.assertEqual(seen["timeout"], 7,
                             "min(PROBE_TIMEOUT, row) — a row may only shorten")
            mr.probe("https://example.test/v1", "k", "m",
                     row={"name": "loose", "timeout": 600})
            self.assertEqual(seen["timeout"], mr.PROBE_TIMEOUT)
        finally:
            mr.urllib.request.urlopen = real

    def test_the_probe_path_records_the_allowance_meter_it_sees(self):
        """The gap I found on the live install: quota was wired into the client
        path only, so every row read quota={} on the board until real traffic
        arrived — and for a pool managed by remaining allowance, the boot probe is
        the meter the operator actually looks at."""
        class FakeResp(object):
            status = 200

            def __init__(self):
                self.headers = {"x-quota-remaining": "119981841",
                                "x-quota-limit": "120000000", "x-qos": "full",
                                "Content-Type": "application/json"}

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "OK"}}],
                                   "usage": {"completion_tokens": 1}}).encode()

        real = mr.urllib.request.urlopen
        try:
            mr.urllib.request.urlopen = lambda req, timeout=None: FakeResp()
            ok, ms, err, content, tokens, q = mr.probe(
                "https://example.test/v1", "k", "m", row={"name": "metered"})
            self.assertTrue(ok)
            self.assertEqual(q["x-quota-remaining"], 119981841)
            self.assertEqual(q["x-qos"], "full")
        finally:
            mr.urllib.request.urlopen = real

    def test_a_failed_probe_returns_its_meter_instead_of_losing_it(self):
        """A 429 is the response where remaining=0 and the reset clock matter most."""
        real = mr.urllib.request.urlopen

        def boom(req, timeout=None):
            # a real email.message.Message, the type urllib actually delivers, so
            # this proves the parser on the live header shape and not on a dict
            h = email.message.Message()
            h["x-quota-remaining"] = "0"
            h["retry-after"] = "43200"
            raise urllib.error.HTTPError(
                "https://example.test/v1/chat/completions", 429, "rate limited", h, None)

        try:
            mr.urllib.request.urlopen = boom
            ok, ms, err, content, tokens, q = mr.probe(
                "https://example.test/v1", "k", "m", row={"name": "spent"})
            self.assertFalse(ok)
            self.assertEqual(q["x-quota-remaining"], 0)
            self.assertEqual(q["retry-after"], 43200)
        finally:
            mr.urllib.request.urlopen = real


class _SpentFreshFixture(object):
    """Two accounts, one model: 'a' is spent for the day (429 + meter at 0),
    'b' is fresh (200 + meter at 999). Records every key it is asked by, in order,
    so a test can see WHICH account a request actually struck."""

    def __init__(self):
        self.seen = []
        self.server = None
        self.port = None

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                try:
                    payload = json.loads(raw or b"{}")
                except Exception:
                    payload = {}
                auth = str(self.headers.get("Authorization", ""))
                key = "a" if auth.endswith("a") else ("hang" if auth.endswith("h") else "b")
                if self.path.endswith("/chat/completions"):
                    outer.seen.append(key)
                if key == "hang":
                    time.sleep(12)          # slower than the row's own timeout
                    body = json.dumps({"error": {"message": "too slow"}}).encode()
                    self.send_response(504)
                elif key == "a":
                    body = json.dumps({"error": {"message": "today's allowance is gone",
                                                 "type": "rate_limit_error",
                                                 "code": "daily_quota_exceeded"}}).encode()
                    self.send_response(429)
                    self.send_header("x-quota-remaining", "0")
                    self.send_header("x-quota-limit", "100000000")
                    self.send_header("retry-after", "43200")
                else:
                    body = json.dumps({"id": "chatcmpl-f", "object": "chat.completion",
                                       "model": payload.get("model", "m"),
                                       "choices": [{"index": 0, "finish_reason": "stop",
                                                    "message": {"role": "assistant",
                                                                "content": "FRESH"}}],
                                       "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                                                 "total_tokens": 6}}).encode()
                    self.send_response(200)
                    self.send_header("x-quota-remaining", "999")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.endswith("/models"):
                    body = json.dumps({"object": "list", "data": [{"id": "m"}]}).encode()
                    self.send_response(200)
                else:
                    body = b"{}"
                    self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass


class TestPoolLearnsEndToEnd(unittest.TestCase):
    """The defect only shows with a real router process: request 1 fails over,
    request 2 must NOT re-strike the account request 1 just learned is spent."""

    def _boot(self, rows, port=None):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, ".minimalrouter"), exist_ok=True)
        with open(os.path.join(tmp, ".minimalrouter-setup"), "w") as f:
            f.write("")
        with open(os.path.join(tmp, ".minimalrouter", "rows.json"), "w") as f:
            json.dump(rows, f)
        rport = port or free_port()
        env = dict(os.environ, HOME=tmp, MINIMALROUTER_PORT=str(rport),
                   MINIMALROUTER_PROBE_INTERVAL="0")   # no mid-test re-probe noise
        srv = subprocess.Popen([sys.executable, str(ENGINE), "serve"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=env)
        base = "http://127.0.0.1:%d" % rport
        for _ in range(60):
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        else:
            self._stop(srv)
            self.fail("router subprocess did not come up")
        return srv, base, tmp

    def _stop(self, srv):
        """Terminate AND reap the router subprocess.

        A bare terminate() leaves the child running while the Popen object is
        garbage-collected, which is exactly the
        `ResourceWarning: subprocess N is still running` the first suite run
        printed three times. Leaked children keep holding their ports, so a
        repeated suite run accumulates zombies and can squat on a free_port()
        winner — a flake that looks like a race and is really our own hygiene.
        """
        if srv is None:
            return
        try:
            srv.terminate()
            srv.wait(timeout=10)
        except Exception:
            try:
                srv.kill()
                srv.wait(timeout=5)
            except Exception:
                pass

    def _chat(self, base, model="m", timeout=25):
        rq = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps({"model": model,
                             "messages": [{"role": "user", "content": "hi"}],
                             "max_tokens": 10}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(rq, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode()), time.time() - t0
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:300], time.time() - t0

    def _state(self, base):
        with urllib.request.urlopen(base + "/api/state", timeout=10) as r:
            return json.loads(r.read().decode())

    def test_the_second_request_does_not_restrike_the_spent_account(self):
        fx = _SpentFreshFixture().start()
        srv = None
        try:
            base_url = "http://127.0.0.1:%d/v1" % fx.port
            srv, base, _ = self._boot([
                # the SPENT account is configured first: that is exactly the
                # pre-fix trap, and row order must not decide who gets hammered.
                {"name": "spent", "base": base_url, "model": "m", "key": "a"},
                {"name": "fresh", "base": base_url, "model": "m", "key": "b"},
            ])
            # let the boot probe land so state exists before the first client call
            for _ in range(40):
                st = self._state(base)
                if all(r["state"].get("status") for r in st["rows"]):
                    break
                time.sleep(0.25)
            mark = len(fx.seen)

            code1, body1, _ = self._chat(base)
            self.assertEqual(code1, 200, "request 1 must still succeed via failover")
            self.assertEqual(body1["choices"][0]["message"]["content"], "FRESH")
            strike1 = fx.seen[mark:]
            self.assertEqual(strike1, ["a", "b"],
                             "request 1 strikes the configured order, then fails over")

            mark2 = len(fx.seen)
            code2, body2, _ = self._chat(base)
            self.assertEqual(code2, 200)
            self.assertEqual(body2["choices"][0]["message"]["content"], "FRESH")
            strike2 = fx.seen[mark2:]
            self.assertEqual(
                strike2, ["b"],
                "request 2 must go straight to the account that can serve. "
                "Pre-v1.5.0 this is ['a','b'] again — the pool re-derives the "
                "same dead end on every request, which is the 'hammer one free "
                "tier N ways' surface 1.5.0 exists to close.")
        finally:
            self._stop(srv)
            fx.stop()

    def test_the_allowance_meter_survives_the_proxy_into_api_state(self):
        """x-quota-* is some upstreams' ONLY meter. The router forwards the body
        and dropped the headers, so a pool of such accounts was blind to remaining
        tokens — the one number the pool exists to manage."""
        fx = _SpentFreshFixture().start()
        srv = None
        try:
            base_url = "http://127.0.0.1:%d/v1" % fx.port
            srv, base, _ = self._boot([
                {"name": "spent", "base": base_url, "model": "m", "key": "a"},
                {"name": "fresh", "base": base_url, "model": "m", "key": "b"},
            ])
            self._chat(base)
            st = self._state(base)
            by = {r["name"]: r for r in st["rows"]}
            self.assertEqual(by["fresh"]["state"]["quota"].get("x-quota-remaining"), 999,
                             "the serving row must show what is left")
            q = by["spent"]["state"]["quota"]
            self.assertEqual(q.get("x-quota-remaining"), 0)
            self.assertEqual(q.get("retry-after"), 43200,
                             "the 429's reset clock is the useful half of the meter")
        finally:
            self._stop(srv)
            fx.stop()

    def test_a_hanging_upstream_fails_at_the_row_timeout_not_the_global(self):
        """The exposure this closes: an upstream that hangs instead of returning
        429 makes the global 90s a PER-ROW multiplier. Four such rows is 360s of
        silence before the caller learns anything."""
        fx = _SpentFreshFixture().start()
        srv = None
        try:
            base_url = "http://127.0.0.1:%d/v1" % fx.port
            srv, base, _ = self._boot([
                {"name": "hang", "base": base_url, "model": "m", "key": "h",
                 "timeout": 3},
            ])
            code, body, elapsed = self._chat(base, timeout=20)
            self.assertEqual(code, 502, "a dead chain still answers 502")
            self.assertLess(elapsed, 12,
                            "must fail near the row's 3s, not the global 90s "
                            "(elapsed %.1fs)" % elapsed)
            self.assertIn("hang", str(body))
        finally:
            self._stop(srv)
            fx.stop()


# v1.8.0 — runtime discipline: retry clocks, backoff, concurrency, single-flight, pre-rotation
    def _upstream(self, handler_cls):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, "http://127.0.0.1:%d/v1" % srv.server_address[1]

    def test_a_429_with_retry_after_sets_a_retry_clock(self):
        hits = {"n": 0}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                hits["n"] += 1
                self.send_response(429)
                self.send_header("Retry-After", "3")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv, base_url = self._upstream(H)
        router = None
        try:
            router, base, _ = self._boot([
                {"name": "spent", "base": base_url, "model": "m", "key": "k"},
            ])
            # Let the boot probe run and its own 3s clock expire before the
            # client path is measured; the probe is a real upstream hit and the
            # test must not mistake it for a second client strike.
            time.sleep(4.5)
            before = hits["n"]
            code, body, elapsed = self._chat(base, timeout=20)
            self.assertEqual(code, 502)
            self.assertEqual(hits["n"], before + 1,
                             "the first client request is ONE upstream hit")
            st = self._state(base)
            row = st["rows"][0]["state"]
            self.assertEqual(row.get("retry_source"), "retry-after")
            left = float(row.get("retry_at", 0)) - time.time()
            self.assertGreater(left, 1.5,
                               "the 3s clock must still be running (left %.1fs)" % left)
            self.assertLess(left, 5.0)
            # the second request must wait the advertised interval, not restrike
            t0 = time.time()
            code2, _, elapsed2 = self._chat(base, timeout=20)
            waited = time.time() - t0
            self.assertEqual(code2, 502)
            self.assertGreaterEqual(waited, 2.5,
                                    "a 429 row was restriked before its clock expired")
            self.assertEqual(hits["n"], before + 2,
                             "the second client request must be ONE upstream hit")
        finally:
            self._stop(router)
            srv.shutdown()

    def test_a_bare_429_runs_the_backoff_curve(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv, base_url = self._upstream(H)
        router = None
        try:
            router, base, _ = self._boot([
                {"name": "bare", "base": base_url, "model": "m", "key": "k"},
            ])
            code, _, _ = self._chat(base, timeout=20)
            self.assertEqual(code, 502)
            row = self._state(base)["rows"][0]["state"]
            self.assertEqual(row.get("retry_source"), "backoff")
            left = float(row.get("retry_at", 0)) - time.time()
            self.assertGreater(left, 0.0)
            self.assertLess(left, 2.0,
                            "streak 1 curve is 0.5s + jitter, not a long bench")
        finally:
            self._stop(router)
            srv.shutdown()

    def test_the_retry_clock_re_admits_the_row_after_it_expires(self):
        now = 1000.0
        cooled = {"status": "fail", "error": "HTTP Error 429: x",
                  "retry_at": now + 5}
        expired = {"status": "fail", "error": "HTTP Error 429: x",
                   "retry_at": now - 1}
        self.assertEqual(mr.classify_status(cooled, now), "cooldown")
        self.assertEqual(mr.classify_status(expired, now), "error",
                         "a row whose clock passed is not benched forever")
        chain = [{"name": "cool", "model": "m"}, {"name": "fit", "model": "m"}]
        state = {"cool": cooled, "fit": {"status": "ok"}}
        out = [r["name"] for r in mr.order_chain(chain, state, now)]
        self.assertEqual(out, ["fit", "cool"])
        out2 = [r["name"] for r in mr.order_chain(chain, {"cool": expired, "fit": {"status": "ok"}}, now)]
        self.assertEqual(out2, ["cool", "fit"],
                         "the re-admitted row keeps its file position")

    def test_weights_and_latency_order_the_healthy_group(self):
        now = 1000.0
        chain = [
            {"name": "heavy", "model": "m", "weight": 10},
            {"name": "slow", "model": "m"},
            {"name": "fast", "model": "m"},
        ]
        state = {
            "heavy": {"status": "ok", "ms": 900},
            "slow": {"status": "ok", "ms": 400},
            "fast": {"status": "ok", "ms": 100},
        }
        out = [r["name"] for r in mr.order_chain(chain, state, now)]
        self.assertEqual(out, ["heavy", "fast", "slow"])
        # no weight, no latency: file order survives (stable sort)
        blind = [{"name": "a", "model": "m"}, {"name": "b", "model": "m"}]
        self.assertEqual([r["name"] for r in mr.order_chain(blind, {}, now)],
                         ["a", "b"])

    def test_row_concurrency_cap_serialises_a_burst(self):
        live = {"active": 0, "max": 0}
        lock = threading.Lock()

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                with lock:
                    live["active"] += 1
                    live["max"] = max(live["max"], live["active"])
                time.sleep(0.15)
                with lock:
                    live["active"] -= 1
                body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv, base_url = self._upstream(H)
        router = None
        try:
            router, base, _ = self._boot([
                {"name": "capped", "base": base_url, "model": "m", "key": "k",
                 "concurrency": 1},
            ])
            codes = []

            def one():
                c, _, _ = self._chat(base, timeout=30)
                codes.append(c)

            ts = [threading.Thread(target=one) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(codes, [200] * 8,
                             "a capped row queues, it does not refuse clients")
            self.assertEqual(live["max"], 1,
                             "the row was hammered %d ways at once" % live["max"])
        finally:
            self._stop(router)
            srv.shutdown()

    def test_a_row_can_opt_out_of_the_cap(self):
        live = {"active": 0, "max": 0}
        lock = threading.Lock()

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                with lock:
                    live["active"] += 1
                    live["max"] = max(live["max"], live["active"])
                time.sleep(0.15)
                with lock:
                    live["active"] -= 1
                body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv, base_url = self._upstream(H)
        router = None
        try:
            router, base, _ = self._boot([
                {"name": "uncapped", "base": base_url, "model": "m", "key": "k",
                 "concurrency": 0},
            ])
            ts = [threading.Thread(target=self._chat, args=(base,),
                                   kwargs={"timeout": 30}) for _ in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertGreater(live["max"], 1,
                               "concurrency 0 must mean unlimited, not a hidden cap")
        finally:
            self._stop(router)
            srv.shutdown()

    def test_generic_oauth_refresh_is_single_flight(self):
        tmp = tempfile.mkdtemp()
        saved = {a: getattr(mr, a) for a in
                 ("OAUTH_PATH", "OAUTH_PROFILES_PATH", "MR_HOME")}
        calls = {"n": 0}
        lock = threading.Lock()
        try:
            mr.MR_HOME = tmp
            mr.OAUTH_PATH = os.path.join(tmp, "oauth.json")
            mr.OAUTH_PROFILES_PATH = os.path.join(tmp, "oauth-providers.json")
            json.dump({"nous": {"label": "Nous", "refresh_url": "https://r/refresh"}},
                      open(mr.OAUTH_PROFILES_PATH, "w"))
            json.dump({"nous": {"access_token": "old", "refresh_token": "rt",
                                "expires_at": time.time() - 10}},
                      open(mr.OAUTH_PATH, "w"))

            class FakeResp:
                def __init__(self, body):
                    self._b = body

                def read(self):
                    return self._b

            def opener(req, timeout=30):
                with lock:
                    calls["n"] += 1
                time.sleep(0.2)
                return FakeResp(json.dumps({"access_token": "new",
                                            "expires_in": 3600}).encode())

            old = mr.urllib.request.urlopen
            mr.urllib.request.urlopen = opener
            tokens = []

            def grab():
                tokens.append(mr.oauth_access_token("nous"))

            ts = [threading.Thread(target=grab) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(calls["n"], 1,
                             "8 concurrent 401s produced %d vendor refreshes" % calls["n"])
            self.assertEqual(set(tokens), {"new"})
            self.assertEqual(json.load(open(mr.OAUTH_PATH))["nous"]["access_token"], "new")
        finally:
            mr.urllib.request.urlopen = old
            for a, v in saved.items():
                setattr(mr, a, v)

    def test_pre_rotation_refreshes_only_expiring_profiles(self):
        tmp = tempfile.mkdtemp()
        saved = {a: getattr(mr, a) for a in
                 ("OAUTH_PATH", "OAUTH_PROFILES_PATH", "MR_HOME")}
        seen = []
        try:
            mr.MR_HOME = tmp
            mr.OAUTH_PATH = os.path.join(tmp, "oauth.json")
            mr.OAUTH_PROFILES_PATH = os.path.join(tmp, "oauth-providers.json")
            json.dump({
                "soon": {"label": "Soon", "refresh_url": "https://r"},
                "later": {"label": "Later", "refresh_url": "https://r"},
                "jwt": {"label": "No expiry", "refresh_url": "https://r"},
            }, open(mr.OAUTH_PROFILES_PATH, "w"))
            json.dump({
                "soon": {"access_token": "a", "refresh_token": "rt",
                         "expires_at": time.time() + 100},
                "later": {"access_token": "b", "refresh_token": "rt",
                          "expires_at": time.time() + 999999},
                "jwt": {"access_token": "c", "refresh_token": "rt"},
            }, open(mr.OAUTH_PATH, "w"))
            old = mr.oauth_refresh
            mr.oauth_refresh = lambda p: (seen.append(p), (True, "ok"))[1]
            mr._prerotate_once()
            self.assertEqual(seen, ["soon"],
                             "pre-rotation touched a profile that did not need it")
        finally:
            mr.oauth_refresh = old
            for a, v in saved.items():
                setattr(mr, a, v)


    # ---------------- v1.9.0: Anthropic /v1/messages shim ----------------
    def _messages(self, base, body, timeout=25):
        rq = urllib.request.Request(
            base + "/v1/messages",
            data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(rq, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def test_anthropic_request_translation_preserves_tools(self):
        body = {
            "model": "m", "max_tokens": 64,
            "system": [{"type": "text", "text": "be brief"}],
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "calling"},
                    {"type": "tool_use", "id": "toolu_1", "name": "read",
                     "input": {"path": "a.txt"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1",
                     "content": [{"type": "text", "text": "ok"}]}]},
            ],
            "tools": [{"name": "read", "description": "read a file",
                       "input_schema": {"type": "object", "properties": {}}}],
            "tool_choice": {"type": "auto"},
        }
        oa = mr.anthropic_to_openai(body)
        self.assertEqual(oa["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual(oa["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual(oa["messages"][2]["tool_calls"][0]["function"]["name"], "read")
        self.assertEqual(oa["messages"][2]["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(oa["messages"][3]["role"], "tool")
        self.assertEqual(oa["messages"][3]["tool_call_id"], "toolu_1")
        self.assertEqual(oa["tools"][0]["function"]["name"], "read")
        self.assertEqual(oa["tool_choice"], "auto")
        self.assertFalse(oa["stream"])

    def test_anthropic_response_translation_maps_tool_calls(self):
        data = {"id": "chatcmpl-1", "choices": [{
            "message": {"role": "assistant", "content": "calling",
                        "tool_calls": [{"id": "call_1", "type": "function",
                                        "function": {"name": "read",
                                                     "arguments": '{"path":"a.txt"}'}}]},
            "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        anth = mr.openai_to_anthropic(data, "m")
        self.assertEqual(anth["stop_reason"], "tool_use")
        self.assertEqual(anth["content"][0]["type"], "text")
        self.assertEqual(anth["content"][1]["type"], "tool_use")
        self.assertEqual(anth["content"][1]["input"], {"path": "a.txt"})
        self.assertEqual(anth["usage"], {"input_tokens": 10, "output_tokens": 5})

    def test_v1_messages_endpoint_translates_a_tool_call(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                data = {"id": "chatcmpl-1", "choices": [{
                    "message": {"role": "assistant", "content": None,
                                "tool_calls": [{"id": "call_1", "type": "function",
                                                "function": {"name": "read",
                                                             "arguments": '{"path":"a.txt"}'}}]},
                    "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 4}}
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv, base_url = self._upstream(H)
        router = None
        try:
            router, base, _ = self._boot([
                {"name": "anth", "base": base_url, "model": "m", "key": "k"},
            ])
            body = {"model": "m", "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"name": "read", "description": "read",
                               "input_schema": {"type": "object"}}]}
            code, raw = self._messages(base, body)
            self.assertEqual(code, 200)
            data = json.loads(raw)
            self.assertEqual(data["type"], "message")
            self.assertEqual(data["stop_reason"], "tool_use")
            self.assertEqual(data["content"][0]["type"], "tool_use")
            self.assertEqual(data["content"][0]["name"], "read")
            self.assertEqual(data["usage"], {"input_tokens": 7, "output_tokens": 4})
        finally:
            self._stop(router)
            srv.shutdown()

    def test_v1_messages_stream_emits_anthropic_events(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                data = {"id": "chatcmpl-1", "choices": [{
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv, base_url = self._upstream(H)
        router = None
        try:
            router, base, _ = self._boot([
                {"name": "anth", "base": base_url, "model": "m", "key": "k"},
            ])
            code, raw = self._messages(base, {
                "model": "m", "max_tokens": 64, "stream": True,
                "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(code, 200)
            self.assertIn("event: message_start", raw)
            self.assertIn("event: content_block_delta", raw)
            self.assertIn("hello", raw)
            self.assertIn("event: message_stop", raw)
        finally:
            self._stop(router)
            srv.shutdown()


# v1.6.0 — auto-fetch add, responses auto-detect, row User-Agent
class TestProbeAutoV160(unittest.TestCase):
    """probe_auto retries through the Responses transport when the chat endpoint
    itself is absent, and reports the RESPONSES-side failure when both fail."""

    def _row(self, **kw):
        r = {"base": "https://x/v1", "model": "m", "key": "K"}
        r.update(kw)
        return r

    def test_retries_responses_on_protocol_miss(self):
        calls = []

        def fake_probe(base, key, model, row=None):
            mode = (row or {}).get("api_mode", "chat_completions")
            calls.append(mode)
            if mode == "chat_completions":
                return (False, 5.0, "HTTPError: HTTP Error 404: Not Found",
                        "", 0, {})
            return (True, 7.0, "", "OK", 1, {})

        old = mr.probe
        mr.probe = fake_probe
        try:
            res, row = mr.probe_auto("https://x/v1", "K", "m", self._row())
        finally:
            mr.probe = old
        self.assertTrue(res[0])
        self.assertEqual(row.get("api_mode"), "responses")
        self.assertEqual(calls, ["chat_completions", "responses"])

    def test_no_retry_on_auth_failure(self):
        calls = []

        def fake_probe(base, key, model, row=None):
            calls.append((row or {}).get("api_mode", "chat_completions"))
            return (False, 5.0, "HTTPError: HTTP Error 401: Unauthorized",
                    "", 0, {})

        old = mr.probe
        mr.probe = fake_probe
        try:
            res, row = mr.probe_auto("https://x/v1", "K", "m", self._row())
        finally:
            mr.probe = old
        self.assertFalse(res[0])
        self.assertNotEqual(row.get("api_mode"), "responses")
        self.assertEqual(calls, ["chat_completions"],
                         "a 401 is an auth verdict, not a protocol shape")

    def test_dual_failure_surfaces_responses_error(self):
        def fake_probe(base, key, model, row=None):
            if (row or {}).get("api_mode") == "responses":
                return (False, 5.0, "HTTPError: HTTP Error 403: Forbidden",
                        "", 0, {})
            return (False, 5.0, "HTTPError: HTTP Error 400: Bad Request",
                    "", 0, {})

        old = mr.probe
        mr.probe = fake_probe
        try:
            res, _row = mr.probe_auto("https://x/v1", "K", "m", self._row())
        finally:
            mr.probe = old
        self.assertFalse(res[0])
        self.assertIn("403", res[2],
                      "the working endpoint's verdict is the actionable one")

    def test_explicit_responses_mode_skips_chat(self):
        calls = []

        def fake_probe(base, key, model, row=None):
            calls.append((row or {}).get("api_mode", "chat_completions"))
            return (True, 5.0, "", "OK", 1, {})

        old = mr.probe
        mr.probe = fake_probe
        try:
            res, row = mr.probe_auto("https://x/v1", "K", "m",
                                     self._row(api_mode="responses"))
        finally:
            mr.probe = old
        self.assertTrue(res[0])
        self.assertEqual(calls, ["responses"])


class TestWriteRowsKeepsSecretsV160(unittest.TestCase):
    def test_key_env_row_never_persists_resolved_key(self):
        tmp = tempfile.TemporaryDirectory()
        old = mr.ROWS_PATH
        mr.ROWS_PATH = os.path.join(tmp.name, "rows.json")
        try:
            rows = [{"name": "env-row", "base": "https://x/v1", "model": "m",
                     "key_env": "SOME_KEY", "key": "sk-resolved-plaintext"}]
            mr.write_rows(rows)
            with open(mr.ROWS_PATH) as f:
                disk = json.load(f)
            self.assertNotIn("key", disk[0],
                             "read_rows injects the key; writing it back copies "
                             "the secret into rows.json")
            self.assertEqual(disk[0]["key_env"], "SOME_KEY")
        finally:
            mr.ROWS_PATH = old
            tmp.cleanup()

    def test_inline_key_row_still_persists(self):
        tmp = tempfile.TemporaryDirectory()
        old = mr.ROWS_PATH
        mr.ROWS_PATH = os.path.join(tmp.name, "rows.json")
        try:
            rows = [{"name": "plain", "base": "https://x/v1", "model": "m",
                     "key": "sk-inline"}]
            mr.write_rows(rows)
            with open(mr.ROWS_PATH) as f:
                disk = json.load(f)
            self.assertEqual(disk[0]["key"], "sk-inline")
        finally:
            mr.ROWS_PATH = old
            tmp.cleanup()


class TestAddAutoFetchV160(unittest.TestCase):
    """POST /api/add with no model: fetch /v1/models, save the set behind ONE
    real completion. Driven through the real handler over a temp rows file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rows = os.path.join(self.tmp.name, "rows.json")
        with open(rows, "w") as f:
            json.dump([], f)
        self._saved = (mr.ROWS_PATH, mr.OAUTH_PROFILES_PATH, mr.OAUTH_PATH)
        mr.ROWS_PATH = rows
        mr.OAUTH_PROFILES_PATH = os.path.join(self.tmp.name, "none.json")
        mr.OAUTH_PATH = os.path.join(self.tmp.name, "none2.json")
        self._saved_state = dict(mr.ROW_STATE)
        mr.ROW_STATE.clear()
        self.srv = HTTPServer(("127.0.0.1", 0), mr.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        (mr.ROWS_PATH, mr.OAUTH_PROFILES_PATH, mr.OAUTH_PATH) = self._saved
        mr.ROW_STATE.clear()
        mr.ROW_STATE.update(self._saved_state)
        self.tmp.cleanup()

    def _post(self, path, obj):
        rq = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(obj).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(rq, timeout=15) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}")

    def test_no_model_fetches_and_saves_the_set(self):
        def fake_fetch(base, key):
            return ["m-a", "m-b", "m-c"]

        def fake_probe_auto(base, key, model, row=None):
            if model == "m-a":
                return (False, 5.0, "HTTPError: HTTP Error 401: Unauthorized",
                        "", 0, {}), dict(row or {})
            return (True, 9.0, "", "OK", 3, {}), dict(row or {},
                                                      api_mode="responses")

        of, op = mr.fetch_models, mr.probe_auto
        mr.fetch_models, mr.probe_auto = fake_fetch, fake_probe_auto
        try:
            res = self._post("/api/add", {"base": "https://x/v1", "key": "K"})
        finally:
            mr.fetch_models, mr.probe_auto = of, op
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("verified"), "m-b")
        self.assertEqual(res.get("saved"), 3)
        self.assertEqual(res.get("api_mode"), "responses")
        with open(mr.ROWS_PATH) as f:
            disk = json.load(f)
        self.assertEqual([r["model"] for r in disk], ["m-b", "m-a", "m-c"],
                         "the verified id leads; every fetched id is saved")
        self.assertTrue(all(r.get("api_mode") == "responses" for r in disk),
                        "the row that PASSED defines the protocol for the set")
        self.assertEqual(disk[0]["name"], res.get("name"))

    def test_named_add_saves_only_the_verified_model(self):
        of, op = mr.fetch_models, mr.probe_auto
        mr.fetch_models = lambda base, key: ["m-a", "m-b"]
        mr.probe_auto = lambda base, key, model, row=None: (
            (True, 9.0, "", "OK", 3, {}), dict(row or {}))
        try:
            res = self._post("/api/add", {"base": "https://x/v1", "key": "K",
                                          "name": "one-row"})
        finally:
            mr.fetch_models, mr.probe_auto = of, op
        self.assertTrue(res.get("ok"), res)
        with open(mr.ROWS_PATH) as f:
            disk = json.load(f)
        self.assertEqual(len(disk), 1)
        self.assertEqual(disk[0]["name"], "one-row")

    def test_fetch_empty_is_a_clean_400(self):
        of, op = mr.fetch_models, mr.probe_auto
        mr.fetch_models = lambda base, key: []
        mr.probe_auto = lambda *a, **k: self.fail("probed with no candidates")
        try:
            res = self._post("/api/add", {"base": "https://x/v1", "key": "K"})
        finally:
            mr.fetch_models, mr.probe_auto = of, op
        self.assertIn("error", res)
        with open(mr.ROWS_PATH) as f:
            self.assertEqual(json.load(f), [])

    def test_failing_set_writes_nothing(self):
        of, op = mr.fetch_models, mr.probe_auto
        mr.fetch_models = lambda base, key: ["m-a", "m-b"]
        mr.probe_auto = lambda base, key, model, row=None: (
            (False, 5.0, "HTTPError: HTTP Error 401: Unauthorized", "", 0, {}),
            dict(row or {}))
        try:
            res = self._post("/api/add", {"base": "https://x/v1", "key": "K"})
        finally:
            mr.fetch_models, mr.probe_auto = of, op
        self.assertIn("test failed", str(res.get("error")))
        with open(mr.ROWS_PATH) as f:
            self.assertEqual(json.load(f), [],
                             "test-before-save: a failing paste never lands")

    def test_base_required(self):
        res = self._post("/api/add", {"key": "K"})
        self.assertIn("error", res)

    def test_bad_api_mode_rejected(self):
        res = self._post("/api/add", {"base": "https://x/v1", "model": "m",
                                      "api_mode": "anthropic"})
        self.assertIn("api_mode", str(res.get("error")))

    def test_cap_limits_an_auto_add(self):
        ids = ["m-%02d" % i for i in range(mr.AUTO_ADD_CAP + 7)]
        of, op = mr.fetch_models, mr.probe_auto
        mr.fetch_models = lambda base, key: list(ids)
        mr.probe_auto = lambda base, key, model, row=None: (
            (True, 9.0, "", "OK", 3, {}), dict(row or {}))
        try:
            res = self._post("/api/add", {"base": "https://x/v1", "key": "K"})
        finally:
            mr.fetch_models, mr.probe_auto = of, op
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("advertised"), len(ids))
        self.assertEqual(res.get("saved"), mr.AUTO_ADD_CAP)
        with open(mr.ROWS_PATH) as f:
            self.assertEqual(len(json.load(f)), mr.AUTO_ADD_CAP)


class TestUpdateCommandV160(unittest.TestCase):
    """`minimalrouter update` — version-parsed, AST-checked, atomic, code-only."""

    GOOD = ('#!/usr/bin/env python3\n"""fake engine"""\n'
            '__version__ = "%s"\n\ndef main():\n    return 0\n')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (mr.MR_HOME, mr.UPDATE_RAW, mr._update_get)
        mr.MR_HOME = self.tmp.name
        mr.UPDATE_RAW = "https://example.invalid/main"
        for name in ("minimalrouter.py", "minimalrouter_dashboard.py",
                     "rows.json", "oauth.json"):
            with open(os.path.join(self.tmp.name, name), "w") as f:
                f.write('{"keep": true}' if name.endswith(".json")
                        else "OLD %s" % name)

    def tearDown(self):
        mr.MR_HOME, mr.UPDATE_RAW, mr._update_get = self._saved
        self.tmp.cleanup()

    def _run(self, files, check_only=False, force=False):
        files = {k: (v.encode() if isinstance(v, str) else v)
                 for k, v in files.items()}
        mr._update_get = lambda path, timeout=30: files[path]
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mr.cmd_update(check_only=check_only, force=force)
        return rc, buf.getvalue()

    def test_check_reports_without_writing(self):
        files = {"minimalrouter.py": self.GOOD % "9.9.9",
                 "minimalrouter_dashboard.py": b"# dash"}
        rc, out = self._run(files, check_only=True)
        self.assertEqual(rc, 0)
        self.assertIn("update available: %s -> 9.9.9" % mr.__version__, out)
        with open(os.path.join(mr.MR_HOME, "minimalrouter.py")) as f:
            self.assertTrue(f.read().startswith("OLD"),
                            "--check must never write")

    def test_update_replaces_code_and_leaves_data(self):
        files = {"minimalrouter.py": (self.GOOD % "9.9.9").encode(),
                 "minimalrouter_dashboard.py": b"# new dash\n"}
        before = {}
        for name in ("rows.json", "oauth.json"):
            with open(os.path.join(mr.MR_HOME, name)) as f:
                before[name] = f.read()
        rc, out = self._run(files)
        self.assertEqual(rc, 0, out)
        with open(os.path.join(mr.MR_HOME, "minimalrouter.py")) as f:
            self.assertIn('__version__ = "9.9.9"', f.read())
        with open(os.path.join(mr.MR_HOME, "minimalrouter_dashboard.py")) as f:
            self.assertEqual(f.read(), "# new dash\n")
        for name, text in before.items():
            with open(os.path.join(mr.MR_HOME, name)) as f:
                self.assertEqual(f.read(), text,
                                 "update must never touch user data files")
        backups = [n for n in os.listdir(mr.MR_HOME) if ".bak-" in n]
        self.assertEqual(len(backups), 2, backups)

    def test_truncated_download_writes_nothing(self):
        files = {"minimalrouter.py": b"<html>404</html>",
                 "minimalrouter_dashboard.py": b"# dash"}
        rc, out = self._run(files)
        self.assertEqual(rc, 1)
        self.assertIn("no __version__", out)
        with open(os.path.join(mr.MR_HOME, "minimalrouter.py")) as f:
            self.assertTrue(f.read().startswith("OLD"))

    def test_unparseable_download_writes_nothing(self):
        files = {"minimalrouter.py": b'__version__ = "9.9.9"\ndef broken(:\n',
                 "minimalrouter_dashboard.py": b"# dash"}
        rc, out = self._run(files)
        self.assertEqual(rc, 1)
        self.assertIn("does not parse", out)
        with open(os.path.join(mr.MR_HOME, "minimalrouter.py")) as f:
            self.assertTrue(f.read().startswith("OLD"))

    def test_same_version_is_a_noop(self):
        files = {"minimalrouter.py": (self.GOOD % mr.__version__).encode(),
                 "minimalrouter_dashboard.py": b"# dash"}
        rc, out = self._run(files)
        self.assertEqual(rc, 0)
        self.assertIn("already up to date", out)
        with open(os.path.join(mr.MR_HOME, "minimalrouter.py")) as f:
            self.assertTrue(f.read().startswith("OLD"))

    def test_force_reinstalls_same_version(self):
        files = {"minimalrouter.py": (self.GOOD % mr.__version__).encode(),
                 "minimalrouter_dashboard.py": b"# dash"}
        rc, out = self._run(files, force=True)
        self.assertEqual(rc, 0, out)
        with open(os.path.join(mr.MR_HOME, "minimalrouter.py")) as f:
            self.assertIn('__version__ = "%s"' % mr.__version__, f.read())

    def test_dashboard_failure_keeps_local_copy(self):
        class Boom(Exception):
            pass

        def getter(path, timeout=30):
            if path == "minimalrouter_dashboard.py":
                raise Boom("offline")
            return (self.GOOD % "9.9.9").encode()

        mr._update_get = getter
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mr.cmd_update()
        self.assertEqual(rc, 0)
        self.assertIn("dashboard not refreshed", buf.getvalue())
        with open(os.path.join(mr.MR_HOME, "minimalrouter_dashboard.py")) as f:
            self.assertTrue(f.read().startswith("OLD"))

    def test_parse_version_never_guesses(self):
        self.assertEqual(mr.parse_version('__version__ = "1.2.3"'), (1, 2, 3))
        self.assertIsNone(mr.parse_version("no version here"))
        self.assertIsNone(mr.parse_version('__version__ = "1.2"' ))


class TestPatchClassV193194(unittest.TestCase):
    """1.9.3/1.9.4 patch-class limits: backlog, OAuth snapshot, catalog,
    log rotation. Measured defects, not speculative optimizations."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, ".minimalrouter")
        os.makedirs(self.home)
        self.saved = {a: getattr(mr, a) for a in
                      ("MR_HOME", "ROWS_PATH", "OAUTH_PATH", "OAUTH_PROFILES_PATH",
                       "LOG_PATH", "PID_PATH", "PORT", "HOME", "ENV",
                       "ROW_STATE", "REQLOG")}
        mr.MR_HOME = self.home
        mr.ROWS_PATH = os.path.join(self.home, "rows.json")
        mr.OAUTH_PATH = os.path.join(self.home, "oauth.json")
        mr.OAUTH_PROFILES_PATH = os.path.join(self.home, "oauth-providers.json")
        mr.LOG_PATH = os.path.join(self.home, "minimalrouter.log")
        mr.PID_PATH = os.path.join(self.home, "minimalrouter.pid")
        mr.HOME = self.tmp
        mr.ENV = {}
        mr.ROW_STATE = {}
        mr.REQLOG = []
        self._servers = []

    def tearDown(self):
        for s in self._servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        for a, v in self.saved.items():
            setattr(mr, a, v)

    def _upstream(self, handler):
        s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        self._servers.append(s)
        return "http://127.0.0.1:%d/v1" % s.server_address[1]

    def _router(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        mr.PORT = port
        srv = mr._Srv(("127.0.0.1", port), mr.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._servers.append(srv)
        time.sleep(0.25)
        return "http://127.0.0.1:%d" % port, srv

    def test_backlog_and_worker_cap_survive_a_100_burst(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(0.05)
                b = json.dumps({"choices": [{"message": {"content": "ok"},
                                            "finish_reason": "stop"}],
                               "usage": {"completion_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        base_url = self._upstream(H)
        json.dump([{"name": "burst", "base": base_url, "model": "m", "key": "k"}],
                  open(mr.ROWS_PATH, "w"))
        base, srv = self._router()
        self.assertEqual(mr._Srv.request_queue_size, 128)
        self.assertIsNotNone(srv._worker_slots)
        body = json.dumps({"model": "m",
                           "messages": [{"role": "user", "content": "hi"}]}).encode()

        def one(_):
            req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status
            except Exception as e:
                return type(e).__name__

        with ThreadPoolExecutor(max_workers=100) as ex:
            results = list(ex.map(one, range(100)))
        self.assertEqual(results, [200] * 100,
                         "a 100-request burst must not reset: %s" % results[:5])
        # client responses can arrive a hair before the handler thread releases
        # its slot; poll briefly instead of calling a normal drain a leak.
        drained = False
        for _ in range(100):
            if srv._worker_slots.acquire(blocking=False):
                drained = True
                break
            time.sleep(0.02)
        self.assertTrue(drained, "the worker cap leaked a slot")
        srv._worker_slots.release()

    def test_one_request_reads_oauth_files_once(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                b = json.dumps({"choices": [{"message": {"content": "ok"},
                                            "finish_reason": "stop"}],
                               "usage": {"completion_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        base_url = self._upstream(H)
        json.dump([{"name": "api", "base": base_url, "model": "m", "key": "k"}],
                  open(mr.ROWS_PATH, "w"))
        json.dump({"p%d" % i: {"label": "P%d" % i, "base_url": base_url,
                               "models": ["om%d" % i]}
                   for i in range(5)}, open(mr.OAUTH_PROFILES_PATH, "w"))
        json.dump({"p%d" % i: {"access_token": "t",
                               "expires_at": time.time() + 99999}
                   for i in range(5)}, open(mr.OAUTH_PATH, "w"))
        counts = {"rows": 0, "auth": 0, "profiles": 0}
        old = (mr.read_rows, mr.read_oauth, mr.read_oauth_profiles)

        def wrap(fn, key):
            def f(*a, **kw):
                counts[key] += 1
                return fn(*a, **kw)
            return f

        mr.read_rows = wrap(old[0], "rows")
        mr.read_oauth = wrap(old[1], "auth")
        mr.read_oauth_profiles = wrap(old[2], "profiles")
        try:
            base, _ = self._router()
            body = json.dumps({"model": "m",
                               "messages": [{"role": "user", "content": "hi"}]}).encode()
            req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual(counts, {"rows": 1, "auth": 1, "profiles": 1},
                             "request-scoped OAuth snapshot regressed: %s" % counts)
        finally:
            mr.read_rows, mr.read_oauth, mr.read_oauth_profiles = old

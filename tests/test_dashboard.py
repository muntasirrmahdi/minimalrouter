"""Tests for the optional Textual dashboard (minimalrouter_dashboard.py).

These need `textual`; they are skipped when it is absent so the zero-dep core
test run stays green. CI runs them in a dedicated job that installs textual.

Run (with textual installed): python -m unittest discover -s tests -v
"""
import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401
    HAVE_TEXTUAL = True
except ImportError:
    HAVE_TEXTUAL = False

ROOT = Path(__file__).resolve().parent.parent


def _load():
    p = ROOT / "minimalrouter_dashboard.py"
    spec = importlib.util.spec_from_file_location("mrdash_under_test", p)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestDashboardHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = _load()

    def test_classify(self):
        d = self.d
        self.assertEqual(d.classify({"status": "ok"}), "ok")
        self.assertEqual(d.classify({}), "unknown")
        self.assertEqual(d.classify({"status": "fail", "error": "HTTP Error 403: x"}), "gated")
        self.assertEqual(d.classify({"status": "fail", "error": "HTTP Error 402: x"}), "empty")
        self.assertEqual(d.classify({"status": "fail", "error": "HTTP Error 530: x"}), "error")
        self.assertEqual(d.classify({"status": "fail", "error": "URLError refused"}), "error")

    def test_clip(self):
        self.assertEqual(self.d.clip("short", 10), "short")
        self.assertEqual(self.d.clip("abcdefghij", 5), "abcd…")
        self.assertEqual(len(self.d.clip("x" * 100, 22)), 22)

    def test_spark_empty_and_flat(self):
        self.assertEqual(self.d.spark([]), " ─    ")
        flat = self.d.spark([100, 100, 100])
        self.assertEqual(len(flat), 5)
        self.assertIn("▄", flat)  # flat history = steady mid line, not bottom bleed

    def test_spark_width_and_range(self):
        sp = self.d.spark([10, 20, 30, 40, 50, 60, 70])
        self.assertEqual(len(sp), 5)  # last 5 samples
        self.assertEqual(sp[0], "▁")
        self.assertEqual(sp[-1], "█")

    def test_wordmark_shape_and_split(self):
        rows = self.d.wordmark_rows("minimalrouter")
        self.assertEqual(len(rows), 4)              # 7 pixel rows -> 4 terminal rows
        self.assertTrue(all(len(r) == len(rows[0]) for r in rows))
        split = self.d.split_index("minimalrouter", "minimal")
        self.assertGreater(split, 0)
        self.assertLess(split, len(rows[0]))
        # solid blocks present, no stray chars
        self.assertTrue(any("█" in r or "▀" in r or "▄" in r for r in rows))

    def test_prefs_roundtrip_and_bad_file(self):
        d = self.d
        with tempfile.TemporaryDirectory() as tmp:
            old_home, old_pref = d.MR_HOME, d.PREF_PATH
            d.MR_HOME = tmp
            d.PREF_PATH = os.path.join(tmp, "dashboard.json")
            try:
                d.save_prefs("ocean", True)
                self.assertEqual(d.load_prefs(), {"theme": "ocean", "dense": True})
                d.save_prefs("not-a-theme", False)
                self.assertEqual(d.load_prefs()["theme"], "midnight")  # invalid -> default
                with open(d.PREF_PATH, "w") as f:
                    f.write("{corrupt")
                self.assertEqual(d.load_prefs(), {"theme": "midnight", "dense": False})
            finally:
                d.MR_HOME, d.PREF_PATH = old_home, old_pref

    def test_tail_events_missing_log(self):
        d = self.d
        old = d.LOG_PATH
        d.LOG_PATH = "/nonexistent/minimalrouter.log"
        try:
            self.assertEqual(d.tail_events(4), [])
        finally:
            d.LOG_PATH = old

    def test_tail_events_parses_ok_fail(self):
        d = self.d
        with tempfile.TemporaryDirectory() as tmp:
            old = d.LOG_PATH
            d.LOG_PATH = os.path.join(tmp, "minimalrouter.log")
            try:
                with open(d.LOG_PATH, "w") as f:
                    f.write("[2026-11-20 00:00:01] OK  m via row 1.2s\n")
                    f.write("[2026-11-20 00:00:02] FAIL m via row: HTTP Error 403 (0.5s) -> next\n")
                    f.write("[2026-11-20 00:00:03] MinimalRouter starting\n")
                evs = d.tail_events(4)
                self.assertEqual(len(evs), 2)
                kinds = [k for _, k, _ in evs]
                self.assertIn("ok", kinds)
                self.assertIn("bad", kinds)
            finally:
                d.LOG_PATH = old


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestDashboardApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = _load()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self.d
        self._old = (d.MR_HOME, d.PREF_PATH, d.LOG_PATH)
        d.MR_HOME = self._tmp.name
        d.PREF_PATH = os.path.join(self._tmp.name, "dashboard.json")
        d.LOG_PATH = os.path.join(self._tmp.name, "minimalrouter.log")

    def tearDown(self):
        d = self.d
        d.MR_HOME, d.PREF_PATH, d.LOG_PATH = self._old
        self._tmp.cleanup()

    def test_render_theme_density_quit_no_server(self):
        d = self.d

        async def go():
            app = d.MissionControl(port=8790)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(0.6)
                # no server running -> offline state, must not crash
                self.assertFalse(app.online)
                await pilot.press("p")     # cycle theme
                await pilot.pause(0.2)
                await pilot.press("v")     # toggle density
                await pilot.pause(0.2)
                await pilot.press("down")  # navigate (empty rows, no crash)
                await pilot.press("q")     # quit
                await pilot.pause(0.2)
        asyncio.run(go())
        # theme/density persisted to the isolated prefs file
        prefs = d.load_prefs()
        self.assertIn(prefs["theme"], d.THEME_KEYS)

    def test_render_with_rows(self):
        d = self.d

        async def go():
            app = d.MissionControl(port=8790)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(0.3)
                # inject synthetic state directly (no network)
                app.online = True
                app.rows = [
                    {"name": "row-a", "class": "keyless", "model": "m/a",
                     "base": "https://x/v1", "key_src": "none",
                     "state": {"status": "ok", "ms": 120, "hist": [100, 120, 110]}},
                    {"name": "row-b", "class": "oauth", "model": "m/b",
                     "base": "https://y/v1", "key_src": "env:K", "oauth_provider": "nous",
                     "state": {"status": "fail", "error": "HTTP Error 403: gated"}},
                ]
                app.nmodels = 2
                app.ensure_slots(2)
                app.rebuild_stats()
                app.repaint_rows()
                app.update_selected()
                await pilot.pause(0.3)
                self.assertEqual(len(app.row_statics), 2)
                await pilot.press("down")
                self.assertEqual(app.sel, 1)
                await pilot.press("q")
                await pilot.pause(0.2)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()

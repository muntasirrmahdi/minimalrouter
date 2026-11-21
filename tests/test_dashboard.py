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
        self.assertEqual(self.d.spark([]), "     ")  # blank, not a dash
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
                d.save_prefs("ocean", True, True)
                self.assertEqual(d.load_prefs(),
                                 {"theme": "ocean", "dense": True, "motion": True})
                d.save_prefs("ember", False, False)
                self.assertEqual(d.load_prefs()["motion"], False)
                d.save_prefs("not-a-theme", False, True)
                self.assertEqual(d.load_prefs()["theme"], "midnight")  # invalid -> default
                with open(d.PREF_PATH, "w") as f:
                    f.write("{corrupt")
                self.assertEqual(d.load_prefs(),
                                 {"theme": "midnight", "dense": False, "motion": True})
            finally:
                d.MR_HOME, d.PREF_PATH = old_home, old_pref

    def test_parse_event_line_v12(self):
        d = self.d
        ev = d.parse_event_line("[2026-11-20 00:00:01] OK  m via row 1.2s")
        self.assertEqual(ev, ("00:00:01", "OK  m via row 1.2s", "ok"))
        ev = d.parse_event_line(
            "[2026-11-20 00:00:02] FAIL m via row: HTTP Error 403 (0.5s) -> next")
        self.assertEqual(ev[2], "bad")
        ev = d.parse_event_line("[2026-11-20 00:00:03] MinimalRouter starting on x")
        self.assertEqual(ev[2], "dim")
        ev = d.parse_event_line("[2026-11-20 00:00:04] PROBE all (boot) start: 21 rows")
        self.assertEqual(ev[2], "dim")
        ev = d.parse_event_line("[2026-11-20 00:00:05] DISABLE r1")
        self.assertEqual(ev[2], "warn")
        ev = d.parse_event_line("[2026-11-20 00:00:06] TEST https://x m -> ok")
        self.assertEqual(ev[2], "accent")
        self.assertIsNone(d.parse_event_line("no timestamp here"))
        self.assertIsNone(d.parse_event_line(""))


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
# ---- v1.2.0 dashboard helper tests ----

@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestDashboardV12Helpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = _load()

    def test_px_rows_shape(self):
        d = self.d
        rows = d.px_rows("42")
        self.assertEqual(len(rows), 3)                    # 5 pixel rows -> 3 lines
        self.assertTrue(all(len(r) == len(rows[0]) for r in rows))
        self.assertEqual(len(rows[0]), 11)                # 5+1+5 glyphs
        self.assertTrue(any("█" in r or "▀" in r for r in rows))

    def test_px_rows_unknown_glyph_blank(self):
        rows = self.d.px_rows("±")                        # not in the face -> blank
        self.assertEqual(set("".join(rows)), {" "})

    def test_compact(self):
        d = self.d
        self.assertEqual(d.compact(0), "0")
        self.assertEqual(d.compact(999), "999")
        self.assertEqual(d.compact(1234), "1K")
        self.assertEqual(d.compact(12_345), "12K")
        self.assertEqual(d.compact(2_600_000), "3M")
        self.assertEqual(d.compact(None), "-")

    def test_uptime_str(self):
        d = self.d
        self.assertEqual(d.uptime_str(59), "1M")          # never 0M
        self.assertEqual(d.uptime_str(2790), "46M")
        self.assertEqual(d.uptime_str(7500), "2H")
        self.assertEqual(d.uptime_str(200000), "2D")

    def test_cw_str(self):
        d = self.d
        self.assertEqual(d.cw_str(None), "-")
        self.assertEqual(d.cw_str(131072), "131K")
        self.assertEqual(d.cw_str(32768), "33K")
        self.assertEqual(d.cw_str(512), "512")

    def test_prov_short(self):
        d = self.d
        self.assertEqual(d.prov_short("api.groq.com"), "GROQ")
        self.assertEqual(d.prov_short("Kilocode"), "KILOCODE")
        self.assertEqual(d.prov_short(""), "?")
        # v1.2.0 polish cases (captured-board defects)
        self.assertEqual(d.prov_short("Nous Portal"), "NOUSPORTAL")
        self.assertEqual(d.prov_short("ZCode (Z.ai)"), "ZCODE")
        self.assertEqual(d.prov_short("127.0.0.1", "freebuff-bridge"), "FREEBUFF")
        self.assertEqual(d.prov_short("inference.dahl.global"), "INFERENCE")

    def test_plan_cols_full_and_drop_order(self):
        d = self.d
        cols, w = d.plan_cols(200)
        self.assertEqual(cols, ["ROW", "PROV", "CLASS", "MODEL", "CW", "LAT",
                                "TPS", "OK", "STATUS"])
        cols, w = d.plan_cols(88)
        self.assertNotIn("CW", cols)      # first drop (after MODEL floors at 14)
        self.assertIn("TPS", cols)        # still standing at 88
        self.assertIn("STATUS", cols)     # never dropped
        self.assertIn("LAT", cols)        # never dropped
        self.assertEqual(w["MODEL"], 14)  # floored, not gone
        cols, w = d.plan_cols(60)
        for c in ("TPS", "OK"):
            self.assertNotIn(c, cols)
        self.assertGreaterEqual(w["ROW"], 12)
        self.assertGreaterEqual(w["MODEL"], 14)

    def test_classify_v12_mirror(self):
        d = self.d
        self.assertEqual(d.classify(
            {"status": "fail", "error": "HTTP Error 429: x", "fail_streak": 1}),
            "cooldown")
        self.assertEqual(d.classify(
            {"status": "fail", "error": "HTTP Error 530: x", "fail_streak": 3}),
            "dead")
        import time as _t
        self.assertEqual(d.classify(
            {"status": "ok", "at": _t.time() - 2000}), "stale")

    def test_status_key_covers_every_class(self):
        d = self.d
        for k in ("ok", "gated", "empty", "error", "cooldown", "stale", "dead",
                  "unknown", "off"):
            self.assertIn(k, d.STATUS_KEY)

    def test_themes_fourteen_and_complete(self):
        d = self.d
        self.assertEqual(len(d.THEMES), 14)
        keys = [t["key"] for t in d.THEMES]
        for want in ("midnight", "ocean", "ember", "nous", "claude"):
            self.assertIn(want, keys)
        for t in d.THEMES:
            for field in ("bg", "line", "accent", "strong", "muted", "dim",
                          "sel", "ok", "warn", "bad", "dark"):
                self.assertIn(field, t)

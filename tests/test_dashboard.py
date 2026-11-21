"""Tests for the optional Textual dashboard (minimalrouter_dashboard.py).

These need `textual`; they are skipped when it is absent so the zero-dep core
test run stays green. CI runs them in a dedicated job that installs textual.

Run (with textual installed): python -m unittest discover -s tests -v
"""
import asyncio
import importlib.util
import json
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
# ---- GEOMETRY gate v2


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestDashboardGeometry(unittest.TestCase):
    """Composition gate. Per-widget CONTENT checks cannot see that a panel is
    clipped off-screen or starved to border-only — these assertions measure the
    rendered REGIONS.

    Regression target: 1.2.0-1.2.2 shipped rows_visible = 0/21 at every terminal
    size, because a Vertical gauge inside an auto-height Horizontal defaults to
    `1fr` and never resolves: SELECTED blew up to 40 rows on a 42-row screen,
    free space went negative, the route table's 1fr collapsed to border-only, and
    EVENTS / toast / legend were pushed below the bottom edge.

    Contract after 1.2.3: the frame never scrolls; ROUTES is the only flexible
    panel (1fr + min-height floor); on a short terminal the secondary panels DROP
    OUT (display: none) instead of being pushed off-screen.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = _load()
        # ISOLATION: this class presses p/v/m, and every one of those calls
        # save_prefs(). Without a private PREF_PATH the suite writes the real
        # ~/.minimalrouter/dashboard.json and flips Moon's density between
        # methods — the measured cause of the alternating OK/FAILED runs.
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old = (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH)
        cls.d.MR_HOME = cls._tmp.name
        cls.d.PREF_PATH = os.path.join(cls._tmp.name, "dashboard.json")
        cls.d.LOG_PATH = os.path.join(cls._tmp.name, "minimalrouter.log")

    @classmethod
    def tearDownClass(cls):
        (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH) = cls._old
        cls._tmp.cleanup()

    def _prefs(self, theme="midnight", dense=False, motion=True):
        """Write an explicit, deterministic prefs file before any mount, so
        test ORDER can never change what a mount reads."""
        with open(self.d.PREF_PATH, "w") as f:
            json.dump({"theme": theme, "dense": dense, "motion": motion}, f)

    ROWS = [{"name": "row-%d" % i, "model": "m/%d" % i,
             "base": "https://x%d/v1" % i, "class": "api",
             "provider": "api.x%d.com" % i, "oauth_provider": "",
             "disabled": False, "cw": 131072, "key_src": "pasted", "ok_pct": 100,
             "state": {"status": "ok", "ms": 1200, "hist": [1100, 1200],
                       "tps": 20.0, "fail_streak": 0, "recent": [1, 1],
                       "at": 0}}
            for i in range(21)]

    def _mount(self, w, h, press=None, dense=False):
        d = self.d
        self._prefs(dense=dense)

        async def go():
            app = d.MissionControl(port=8799)
            async with app.run_test(size=(w, h)) as pilot:
                # deterministic wait for the tier to converge (never a fixed
                # sleep: the deciding event can land late, which was the flake)
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break
                app.online = True
                app.rows = [dict(r) for r in self.ROWS]
                app.nmodels = 20
                app.stats = {"rows": 21, "serving": 12, "down": 8, "unknown": 0,
                             "req_min": 0, "tok_s": 0, "err_pct": 0,
                             "uptime": 300, "blocked": 1, "cooldown": 1,
                             "dead": 0, "stale": 0, "disabled": 0}
                app.probe = {"done": 0, "total": 0, "active": False}
                app.render_wordmark()
                app.rebuild_stats()
                app.ensure_slots(21)
                for i in range(len(app.row_statics)):
                    app.row_statics["slot%d" % i].display = i < 21
                app.repaint_rows()
                app.update_selected()
                app.update_events()
                await pilot.pause(0.4)
                if press:
                    for k in press:
                        await pilot.press(k)
                    await pilot.pause(0.4)
                snap = {"screen_h": app.screen.region.height,
                        "tier": app._tier_now(),
                        "classes": list(app.screen.classes),
                        "regions": {}, "vis": {}}
                for sel in ("#frame", "#mast", "#stats", "#tablebox", "#table",
                            "#selbox", "#selrow", ".gauge", "#evbox", "#evbody",
                            "#toast", "#foot", "#help"):
                    try:
                        wd = app.query_one(sel)
                        r = wd.region
                        snap["regions"][sel] = (r.y, r.height)
                        snap["vis"][sel] = bool(wd.display)
                    except Exception as e:
                        snap["regions"][sel] = ("ERR", str(e)[:30])
                        snap["vis"][sel] = False
                try:
                    tb = app.query_one("#table")
                    snap["table_vp"] = tb.content_region.height
                    vis = 0
                    for i in range(21):
                        wd = app.row_statics.get("slot%d" % i)
                        if wd is None or not wd.display:
                            continue
                        if 0 <= wd.region.y - tb.region.y < tb.content_region.height:
                            vis += 1
                    snap["rows_visible"] = vis
                except Exception:
                    snap["table_vp"] = 0
                    snap["rows_visible"] = 0
                await pilot.press("q")
                await pilot.pause(0.2)
            return snap
        return asyncio.run(go())

    def test_routes_table_always_has_rows(self):
        """The primary panel can never be starved — the 1.2.0-1.2.2 defect."""
        for w, h in ((140, 42), (120, 35), (100, 30), (80, 24), (160, 50),
                     (200, 60)):
            with self.subTest(size=(w, h)):
                s = self._mount(w, h)
                self.assertGreaterEqual(s["table_vp"], 1,
                                        "%s viewport=%s" % ((w, h), s["table_vp"]))
                self.assertGreaterEqual(s["rows_visible"], 1,
                                        "%s rows_visible=%s" % ((w, h), s["rows_visible"]))
                self.assertGreaterEqual(s["regions"]["#tablebox"][1], 4,
                                        "%s tablebox h=%s (floor 4)"
                                        % ((w, h), s["regions"]["#tablebox"][1]))

    def test_no_visible_panel_is_off_screen(self):
        """Nothing may sit below the bottom edge. Panels that do not fit must be
        display:none (dropped), never clipped-out-but-claimed-visible."""
        for w, h in ((140, 42), (120, 35), (100, 30), (80, 24), (160, 50)):
            with self.subTest(size=(w, h)):
                s = self._mount(w, h)
                sh = s["screen_h"]
                for sel, (y, hh) in s["regions"].items():
                    if y == "ERR" or not s["vis"][sel]:
                        continue
                    self.assertLessEqual(
                        y + hh, sh + 1,
                        "%s %s y=%s h=%s exceeds screen %s (visible=%s)"
                        % ((w, h), sel, y, hh, sh, s["vis"][sel]))

    def test_selected_and_gauges_stay_pinned(self):
        """The blow-up class: an auto-height row holding a Vertical gauge.
        SELECTED must never exceed its pinned budget at ANY size."""
        for w, h in ((140, 42), (120, 35), (100, 30), (80, 24)):
            with self.subTest(size=(w, h)):
                s = self._mount(w, h)
                if s["vis"]["#selbox"]:
                    self.assertLessEqual(s["regions"]["#selbox"][1], 6,
                                         "selbox h=%s" % s["regions"]["#selbox"][1])
                    self.assertLessEqual(s["regions"][".gauge"][1], 4,
                                         "gauge h=%s" % s["regions"][".gauge"][1])

    def test_tier_bands_match_screen_height(self):
        for h, want in ((42, "airy"), (50, "airy"), (35, "compact"),
                        (32, "compact"), (31, "micro"), (26, "micro"),
                        (24, "micro")):
            with self.subTest(h=h):
                s = self._mount(120, h)
                self.assertEqual(s["tier"], want, "h=%s tier=%s" % (h, s["tier"]))
                # the tier class must actually be applied after on_idle
                self.assertIn("tier-" + want, s["classes"],
                              "classes=%s" % s["classes"])

    def test_micro_drops_events_keeps_selected_and_table(self):
        s = self._mount(100, 26)
        self.assertEqual(s["tier"], "micro")
        self.assertFalse(s["vis"]["#evbox"], "EVENTS must drop in micro")
        self.assertTrue(s["vis"]["#selbox"], "SELECTED is the cursor detail: keep")
        self.assertTrue(s["vis"]["#tablebox"], "ROUTES must never drop")
        self.assertGreaterEqual(s["rows_visible"], 1)

    def test_routes_monotonic_within_each_tier(self):
        """Inside a tier, a taller terminal must never show FEWER routes. The
        first 1.2.3 attempt failed this outright (30 rows -> 4 routes while 28
        rows -> 12, because the boundary kept both secondary panels on a short
        screen). Measured bands: micro < 32, compact 32-39, airy >= 40."""
        for band in ((22, 31), (32, 39), (40, 54)):
            prev = -1
            for h in range(band[0], band[1] + 1):
                with self.subTest(h=h):
                    s = self._mount(120, h)
                    rv = s["rows_visible"]
                    self.assertGreaterEqual(
                        rv, prev,
                        "h=%s shows %s routes but h=%s showed %s (band %s)"
                        % (h, rv, h - 1, prev, band))
                    prev = rv

    def test_tier_boundary_drop_is_bounded(self):
        """Crossing a tier UP may cost rows (the wordmark + pixel tiles + EVENTS
        appear) but never below the usable floor, and never below what the
        smallest screen of that band shows."""
        for up, down in ((32, 31), (40, 39)):
            with self.subTest(boundary=up):
                above = self._mount(120, up)["rows_visible"]
                below = self._mount(120, down)["rows_visible"]
                self.assertGreaterEqual(above, 4,
                                        "tier upgrade collapsed the table: %s" % above)
                self.assertLessEqual(above, below,
                                     "hero should cost rows, not gain them")

    def test_every_screen_size_keeps_the_table_usable(self):
        for h in range(22, 55, 3):
            with self.subTest(h=h):
                s = self._mount(120, h)
                self.assertGreaterEqual(s["rows_visible"], 2,
                                        "h=%s routes_visible=%s" % (h, s["rows_visible"]))

    def test_labels_stay_below_numbers_every_tier(self):
        """Moon's layout law: a tier or density change may shrink the DIGIT FACE
        only. Labels stay below the numbers, never beside them. The tile is a
        Vertical(number, label) in every tier, and the stats strip height equals
        number rows + label row (+1 gap in airy)."""
        for h, want_num in ((42, 3), (35, 1), (26, 1)):
            for dense in (False, True):
                with self.subTest(h=h, dense=dense):
                    s = self._mount(120, h, dense=dense)
                    want = 2 if dense else want_num + 1
                    self.assertEqual(s["regions"]["#stats"][1], want,
                                     "h=%s dense=%s stats h=%s expected %s"
                                     % (h, dense, s["regions"]["#stats"][1], want))

    def test_help_is_a_modal_that_never_displaces_the_layout(self):
        """`?` opens an overlay: it must not push the legend or the table off a
        short screen (the in-flow help did exactly that at 26 rows), and any key
        must close it."""
        d = self.d
        self._prefs()

        async def go():
            app = d.MissionControl(port=8799)
            async with app.run_test(size=(100, 26)) as pilot:
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break
                await pilot.press("question_mark")
                await pilot.pause(0.4)
                names = [type(s).__name__ for s in app._screen_stack]
                opened = "HelpScreen" in names
                # the layout underneath is untouched: legend still on screen
                foot = app.query_one("#foot").region
                sh = app.screen.region.height
                fits = foot.y + foot.height <= sh + 1
                await pilot.press("x")          # any key closes it
                await pilot.pause(0.4)
                names2 = [type(s).__name__ for s in app._screen_stack]
                closed = "HelpScreen" not in names2
                await pilot.press("q")
                await pilot.pause(0.2)
            return opened, fits, closed
        opened, fits, closed = asyncio.run(go())
        self.assertTrue(opened, "help modal must open on ?")
        self.assertTrue(fits, "legend must stay on screen while help is up")
        self.assertTrue(closed, "any key must close the help modal")

    def test_keys_survive_the_help_modal(self):
        """Regression for the class introduced (and caught) during the 1.2.3
        layout fix: an app-level 'help is open' input guard that is never cleared
        freezes the WHOLE keyboard after the modal closes — strictly worse than
        the clipped panels it was meant to protect. A Textual modal owns key
        events on its own, so no app-level guard may exist at all.
        """
        d = self.d
        self._prefs()

        async def go():
            app = d.MissionControl(port=8799)
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break
                # rows must exist or the cursor assertion measures nothing
                app.online = True
                app.rows = [dict(x) for x in TestDashboardGeometry.ROWS]
                app.nmodels = 20
                app.stats = {"rows": 21, "serving": 12, "down": 8, "unknown": 0,
                             "req_min": 0, "tok_s": 0, "err_pct": 0,
                             "uptime": 300, "blocked": 1, "cooldown": 1,
                             "dead": 0, "stale": 0, "disabled": 0}
                app.ensure_slots(21)
                for i in range(len(app.row_statics)):
                    app.row_statics["slot%d" % i].display = i < 21
                app.repaint_rows()
                theme_before = app.theme_ix
                dens_before = app.dense
                await pilot.press("question_mark")
                await pilot.pause(0.4)
                opened = "HelpScreen" in [type(s).__name__
                                          for s in app._screen_stack]
                await pilot.press("x")                 # any key pops it
                await pilot.pause(0.4)
                closed = "HelpScreen" not in [type(s).__name__
                                              for s in app._screen_stack]
                # the board must still respond to EVERY class of key
                await pilot.press("p")
                await pilot.pause(0.4)
                theme_after = app.theme_ix
                await pilot.press("v")
                await pilot.pause(0.4)
                dens_after = app.dense
                await pilot.press("down")
                await pilot.pause(0.2)
                cursor_after = app.sel
                await pilot.press("q")
                await pilot.pause(0.2)
            return (opened, closed, theme_before, theme_after,
                    dens_before, dens_after, cursor_after)
        (opened, closed, tb, ta, db, da, cur) = asyncio.run(go())
        self.assertTrue(opened, "help modal must open")
        self.assertTrue(closed, "help modal must close")
        self.assertNotEqual(tb, ta, "p must still cycle themes after help")
        self.assertNotEqual(db, da, "v must still toggle density after help")
        self.assertEqual(cur, 1, "arrows must still move the cursor after help")

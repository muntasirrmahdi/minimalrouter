"""Tests for the optional Textual dashboard (minimalrouter_dashboard.py).

These need `textual`; they are skipped when it is absent so the zero-dep core
test run stays green. CI runs them in a dedicated job that installs textual.

Run (with textual installed): python -m unittest discover -s tests -v
"""
import asyncio
import importlib.util
import json
import os
import re
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

    def test_columns_line_up_under_their_labels(self):
        """Horizontal alignment. The geometry gate originally measured HEIGHTS
        only, so 1.2.3 shipped with the first four table columns one character
        LEFT of their header labels (header prefix 4 cells, row prefix 3) and a
        LAT cell 2 cells wider in rows than the header allocated.

        Left-justified columns (ROW/PROV/CLASS/MODEL/STATUS) must have their
        token start exactly under the header token. Right-justified numeric
        columns (CW/TPS/OK%) must share the header's cell RIGHT edge, because a
        right-aligned value legitimately starts later than its label.
        """
        d = self.d
        self._prefs()

        async def go():
            app = d.MissionControl(port=8799)
            async with app.run_test(size=(140, 42)) as pilot:
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
                app.ensure_slots(21)
                for i in range(len(app.row_statics)):
                    app.row_statics["slot%d" % i].display = i < 21
                app.repaint_rows()
                await pilot.pause(0.4)
                head = app.query_one("#colhead").render().plain
                rows_txt = [app.query_one("#slot%d" % i).render().plain
                            for i in range(21)]
                # the plan must come from the LIVE board (frame width is 96% of
                # the terminal minus padding), never from an assumed width
                plan = app.col_plan()
                await pilot.press("q")
                await pilot.pause(0.2)
            return head, rows_txt, plan
        head, rows_txt, (cols, w) = asyncio.run(go())

        def toks(s):
            return [(m.group(0), m.start()) for m in re.finditer(r"\S+", s)]

        # cell START offsets, computed from the board's own column plan:
        # 4-cell prefix, then each field's width plus one separating space.
        starts, off = {}, 4
        for c in cols:
            starts[c] = off
            off += d.col_width(cols, w, c) + 1
        H = dict(toks(head))
        self.assertIn("ROW", H, "header lost the ROW label: %r" % head)

        # LEFT-justified: the header label must sit at the cell start, and so
        # must the data field (its first non-space char at the cell start).
        for c, label in (("ROW", "ROW"), ("PROV", "PROV"), ("CLASS", "CLASS"),
                         ("MODEL", "MODEL"), ("STATUS", "STATUS")):
            want = starts[c]
            self.assertEqual(
                H.get(label), want,
                "header label %s at %s, cell starts at %d (header: %r)"
                % (label, H.get(label), want, head))
            for ri, ln in enumerate(rows_txt):
                cell = ln[want:want + w[c]].rstrip()
                if not cell:
                    continue          # a blank cell cannot be judged
                self.assertNotEqual(
                    ln[want], " ",
                    "col %s row %d: field starts at %d, cell starts at %d: %r"
                    % (c, ri, ln.find(cell.strip()[:1]) if cell.strip() else -1,
                       want, ln))

        # right-justified numeric columns: the last character of the field must
        # land on the cell's right edge, and the header label must too. A
        # right-aligned value legitimately starts later than its label, so a
        # token-start comparison would be wrong here.
        for c, label in (("CW", "CW"), ("TPS", "TPS"), ("OK", "OK%")):
            right = starts[c] + w[c]
            self.assertEqual(
                H[label] + len(label), right,
                "header %s ends at %d, cell right edge is %d"
                % (label, H[label] + len(label), right))
            for ri, ln in enumerate(rows_txt):
                window = ln[starts[c]:right]
                stripped = window.rstrip()
                if not stripped:
                    continue          # a blank cell cannot be judged
                self.assertEqual(
                    starts[c] + len(stripped), right,
                    "col %s row %d: value ends at %d, cell right edge is %d: %r"
                    % (c, ri, starts[c] + len(stripped), right, ln))

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


# ---- v1.2.5 regression tests (six defects found by USING the board) ----
#
# Moon ran the real launcher at 05:43 and found: a `?` crash that killed the
# whole board, a CW readout rendering broken digits, arrow/theme/token keys
# blocking the UI thread, `t` ignoring marks, `T` promising "all enabled" while
# testing one row, and a legend label that lied about `m`. Every test below
# bites the PRE-FIX behaviour. None of them needed a screenshot to catch.


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestPixelFaceNeverClips(unittest.TestCase):
    """A 5x5 face with 1-col gaps is 6n-1 wide. The CW gauge box held 22 content
    columns while `256K` needs 23, so `overflow-x: hidden` sliced the last glyph
    mid-stroke and the number LOOKED BROKEN rather than long. The rule: measure
    the box, and if the face does not fit render plain bold text — never half a
    digit.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = _load()

    def test_face_used_when_it_fits(self):
        t = self.d.pixel_face("256K", 30, "bold red")
        lines = t.plain.split("\n")
        self.assertEqual(len(lines), 3, "pixel face is 3 rows")
        self.assertEqual(len(lines[0]), 23,
                         "4 glyphs at 6 cols minus the trailing gap")
        self.assertNotIn("256K", t.plain, "pixels, not the literal string")

    def test_text_fallback_when_it_does_not_fit(self):
        """The exact 1.2.4 geometry: 22 columns of content, 23 of face."""
        t = self.d.pixel_face("256K", 22, "bold red")
        self.assertEqual(t.plain, "256K", "whole digits, no slice")
        self.assertEqual(t.plain.count("\n"), 0, "one line, not a clipped block")

    def test_sweep_no_box_width_yields_partial_glyphs(self):
        """Every box width for every realistic value: a rendered pixel row is
        either the FULL face width or the value is plain text. A short row of
        block characters IS the clipped state."""
        blocks = ("\u2588", "\u2580", "\u2584")
        for value in ("8", "21", "256K", "1M", "1049K", "0", "5M"):
            need = 6 * len(value) - 1
            for avail in range(1, need + 6):
                t = self.d.pixel_face(value, avail, "bold")
                rows = t.plain.split("\n")
                pixel = any(b in t.plain for b in blocks)
                if pixel:
                    self.assertEqual(len(rows), 3,
                                     "%s@%d: pixel face must be 3 rows"
                                     % (value, avail))
                    self.assertEqual(len(rows[0]), need,
                                     "%s@%d: face is %d wide, rendered %d — "
                                     "CLIPPED MID-GLYPH" % (value, avail, need,
                                                           len(rows[0])))
                else:
                    self.assertEqual(t.plain, value,
                                     "%s@%d: fallback must be the whole value"
                                     % (value, avail))

    def test_gauge_box_actually_holds_five_glyphs(self):
        """The CSS is the other half of the fix: 32 wide with 1 column of
        padding each side is 30 content columns, which holds the 5-glyph worst
        case (29)."""
        css = (Path(__file__).resolve().parent.parent /
               "minimalrouter_dashboard.py").read_text()
        m = re.search(r"\.gauge \{ width: (\d+); height: 4; padding: 0 1; \}",
                      css)
        self.assertIsNotNone(m, "gauge CSS must stay width 32 / padding 0 1")
        content = int(m.group(1)) - 2
        self.assertGreaterEqual(content, 29,
                                "gauge content %d cols cannot hold 5 glyphs"
                                % content)

    def test_cw_str_collapses_past_a_million(self):
        d = self.d
        self.assertEqual(d.cw_str(1049000), "1M", "1049K is 5 glyphs; 1M is 2")
        self.assertEqual(d.cw_str(256000), "256K")
        self.assertEqual(d.cw_str(131072), "131K")
        self.assertEqual(d.cw_str(32768), "33K")
        self.assertEqual(d.cw_str(512), "512")
        self.assertEqual(d.cw_str(None), "-")
        for cw in (1000, 999999, 1000000, 1048576, 2000000, 10000000):
            self.assertLessEqual(len(d.cw_str(cw)), 4,
                                 "cw_str(%d) = %r exceeds 4 glyphs"
                                 % (cw, d.cw_str(cw)))


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestCopyUntruncatedValues(unittest.TestCase):
    """v1.4.0: the MODEL cell is clipped for layout, so drag-selecting a long
    model id copies `openrouter/anthropic/claude-…` — a string that does not
    work when pasted. AKASH raised exactly this in the BAI group on 23 Nov
    ("boro gula to shob dot dekhai"). The y / Y / u keys copy the DATA, not the
    screen, and the assertion proves the clipping is real so the fix is not
    solving a problem that does not exist.
    """

    LONG = "openrouter/anthropic/claude-sonnet-5-5-thinking-extended"

    @classmethod
    def setUpClass(cls):
        cls.d = _load()
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old = (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH)
        cls.d.MR_HOME = cls._tmp.name
        cls.d.PREF_PATH = os.path.join(cls._tmp.name, "dashboard.json")
        cls.d.LOG_PATH = os.path.join(cls._tmp.name, "minimalrouter.log")

    @classmethod
    def tearDownClass(cls):
        (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH) = cls._old
        cls._tmp.cleanup()

    def _row(self, **over):
        r = {"name": "kilo-auto/free", "model": self.LONG,
             "base": "https://openrouter.ai/api/v1", "class": "api",
             "provider": "openrouter.ai", "oauth_provider": "",
             "disabled": False, "cw": 200000, "key_src": "env:K", "ok_pct": 100,
             "state": {"status": "ok", "ms": 900, "hist": [900], "tps": 20.0,
                       "fail_streak": 0, "recent": [1], "at": 0}}
        r.update(over)
        return r

    def _boot(self, rows, size=(140, 42)):
        """Mount the board, install the rows, and settle. Returns the pilot."""
        d = self.d
        with open(d.PREF_PATH, "w") as f:
            json.dump({"theme": "midnight", "dense": False, "motion": True}, f)
        app = d.MissionControl(port=8799)

        async def go(body):
            async with app.run_test(size=size) as pilot:
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break
                app.online = True
                app.rows = [dict(r) for r in rows]
                app.nmodels = len(rows)
                app.stats = {"rows": len(rows), "serving": len(rows), "down": 0,
                             "unknown": 0, "req_min": 0, "tok_s": 0,
                             "err_pct": 0, "uptime": 60}
                app.probe = {"done": len(rows), "total": len(rows),
                             "active": False}
                app.sel = 0
                app.marked = set()
                app.testing = {}
                app.toast = ""
                app.ensure_slots(len(app.rows))
                for i in range(len(app.row_statics)):
                    app.row_statics["slot%d" % i].display = i < len(app.rows)
                app.repaint_rows()
                app.update_selected()
                await pilot.pause(0.4)
                out = await body(app, pilot)
                await pilot.press("q")
                await pilot.pause(0.2)
            return out
        return app, go

    def test_y_copies_the_whole_untruncated_model_id(self):
        app, go = self._boot([self._row()])

        async def body(a, pilot):
            await pilot.press("y")
            await pilot.pause(0.25)
            return (a.app.clipboard, a.toast)

        clip, toast = asyncio.run(go(body))
        self.assertEqual(clip, self.LONG,
                         "y must copy the FULL id, not the clipped rendering")
        self.assertIn(self.LONG, toast,
                      "the toast must show the whole value, or the user cannot "
                      "tell whether the dots were copied")
        self.assertIn("MODEL", toast)

    def test_selection_is_already_enabled_and_returns_rendered_text(self):
        """Locks the measured Textual fact that makes the keys necessary.

        If someone later 'adds' drag-select support by passing allow_select to
        the Static ctor, the suite breaks with TypeError — this test records why
        that is not the fix: selection is ON by default, and what it returns is
        the rendered string, dots and all.
        """
        from textual.widget import Widget
        self.assertTrue(Widget.ALLOW_SELECT,
                        "Textual enables selection by default; there is nothing "
                        "to switch on")
        self.assertIsNone(getattr(Widget.allow_select, "fset", None),
                          "allow_select is read-only, so assigning it is a "
                          "silent no-op and passing it to a ctor raises")

        app, go = self._boot([self._row()], size=(120, 40))

        async def body(a, pilot):
            a.screen.text_select_all()
            await pilot.pause(0.25)
            return a.screen.get_selected_text()

        sel = asyncio.run(go(body))
        self.assertTrue(sel, "selection returned nothing at all")
        self.assertNotIn(self.LONG, sel,
                         "selected text can never contain the whole id — it is "
                         "the rendered cell, which is why the keys read the data")
        self.assertIn("…", sel, "the ellipsis is what a member would paste")

    def test_caps_Y_copies_the_row_name_and_u_the_base_url(self):
        app, go = self._boot([self._row()])

        async def body(a, pilot):
            await pilot.press("Y")
            await pilot.pause(0.2)
            name = a.app.clipboard
            await pilot.press("u")
            await pilot.pause(0.2)
            base = a.app.clipboard
            return name, base

        name, base = asyncio.run(go(body))
        self.assertEqual(name, "kilo-auto/free")
        self.assertEqual(base, "https://openrouter.ai/api/v1")

    def test_the_column_really_clips_so_the_fix_is_not_solving_air(self):
        """Proves the defect AKASH described is real on the shipped render: the
        id is longer than the column AND the rendered cell carries an ellipsis
        AND the whole id is absent from the screen."""
        app, go = self._boot([self._row()], size=(120, 40))

        async def body(a, pilot):
            rendered = a.query_one("#slot0").render().plain
            cols, w = a.col_plan()
            return rendered, w

        rendered, w = asyncio.run(go(body))
        self.assertGreater(len(self.LONG), w["MODEL"],
                           "the id must be longer than the column for the clip "
                           "to happen at all")
        self.assertIn("…", rendered,
                      "the MODEL cell must still clip — that is the complaint")
        self.assertNotIn(self.LONG, rendered,
                         "the whole id must NOT be on screen, which is exactly "
                         "why a select-only copy would hand back a broken string")

    def test_empty_field_says_so_instead_of_copying_nothing(self):
        app, go = self._boot([self._row(model="")])

        async def body(a, pilot):
            a.app.copy_to_clipboard("SENTINEL")     # pre-fill the clipboard
            await pilot.press("y")
            await pilot.pause(0.2)
            return a.toast, a.app.clipboard

        toast, clip = asyncio.run(go(body))
        self.assertIn("empty", toast,
                      "an empty id must be reported, not silently skipped")
        self.assertEqual(clip, "SENTINEL",
                         "nothing must be written to the clipboard when the "
                         "field is empty — a stale copy looks like success")

    def test_copy_keys_on_an_empty_pool_do_not_crash(self):
        app, go = self._boot([])

        async def body(a, pilot):
            err = None
            try:
                for k in ("y", "Y", "u"):
                    await pilot.press(k)
                    await pilot.pause(0.1)
            except Exception as e:
                err = repr(e)
            return err

        err = asyncio.run(go(body))
        self.assertIsNone(err,
                          "copy keys with no rows must not raise: %s" % err)

    def test_copy_respects_the_cursor_position(self):
        """Copying must follow the selected row, not always row 0."""
        rows = [self._row(name="first", model="model/one"),
                self._row(name="second", model=self.LONG)]
        app, go = self._boot(rows)

        async def body(a, pilot):
            await pilot.press("down")
            await pilot.pause(0.2)
            await pilot.press("y")
            await pilot.pause(0.2)
            return a.app.clipboard, a.sel

        clip, sel = asyncio.run(go(body))
        self.assertEqual(sel, 1, "the cursor must have moved")
        self.assertEqual(clip, self.LONG,
                         "y must copy the row under the cursor, not the first "
                         "row in the pool")

    def test_marked_batch_actions_are_untouched_by_the_new_keys(self):
        """Regression guard: the new keys must not collide with space/T/e/d."""
        rows = [self._row(), self._row(name="second", model="m2")]
        app, go = self._boot(rows)

        async def body(a, pilot):
            await pilot.press("space")
            await pilot.pause(0.15)
            marked = set(a.marked)
            await pilot.press("y")
            await pilot.pause(0.15)
            still = set(a.marked)
            clip = a.app.clipboard
            return marked, still, clip

        marked, still, clip = asyncio.run(go(body))
        self.assertEqual(marked, {"kilo-auto/free"}, "space must still mark")
        self.assertEqual(still, marked, "y must not disturb the marks")
        self.assertEqual(clip, self.LONG)


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestTimersAreCreatedOnce(unittest.TestCase):
    """v1.3.3: on_idle created the poll + anim timers and Textual fires Idle
    after EVERY message batch, so the board stacked a new live pair per cycle.
    Measured 8 timers at boot -> 162 after 40 s, row rebuild 227 -> 1353 rows/sec,
    no plateau. Moon felt it as progressive slowdown ("still slow hye jacche")
    and as lag on the first keypress, since dozens of timers were already
    running by then.

    A board must not degrade with uptime. This gate samples the live timer
    registry and the poll count at two points in an idle run and requires both
    to be FLAT, plus a static assertion on where set_interval lives.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = _load()
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old = (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH)
        cls.d.MR_HOME = cls._tmp.name
        cls.d.PREF_PATH = os.path.join(cls._tmp.name, "dashboard.json")
        cls.d.LOG_PATH = os.path.join(cls._tmp.name, "minimalrouter.log")

    @classmethod
    def tearDownClass(cls):
        (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH) = cls._old
        cls._tmp.cleanup()

    def _prefs(self):
        with open(self.d.PREF_PATH, "w") as f:
            json.dump({"theme": "midnight", "dense": False, "motion": True}, f)

    def test_timer_count_and_poll_rate_stay_flat(self):
        d = self.d
        self._prefs()

        async def go():
            app = d.MissionControl(port=8799)
            out = {}
            polls = {"n": 0}
            async with app.run_test(size=(140, 42)) as pilot:
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break
                real_poll = app.poll

                def counted():
                    polls["n"] += 1
                    real_poll()
                app.poll = counted
                import time as _t

                async def snap(secs):
                    t0 = _t.monotonic()
                    while _t.monotonic() - t0 < secs:
                        await pilot.pause(0.25)
                    return (len(app._timers), polls["n"])

                out["t1"], out["p1"] = await snap(6.0)
                polls["n"] = 0
                out["t2"], out["p2"] = await snap(14.0)
                await pilot.press("q")
                await pilot.pause(0.2)
            return out

        o = asyncio.run(go())
        self.assertLessEqual(
            o["t2"], o["t1"] + 1,
            "timers GREW while idle: %d -> %d. Something creates a timer per "
            "cycle, so the board gets slower the longer it is open."
            % (o["t1"], o["t2"]))
        # 6 s and 14 s at a 2.0 s interval => ~3 and ~7 polls. Generous slack,
        # but a multiplied timer blows past 2x immediately.
        self.assertLessEqual(o["p1"], 8,
                             "early window ran %d polls in 6 s (expect ~3)"
                             % o["p1"])
        self.assertLessEqual(o["p2"], 14,
                             "late window ran %d polls in 14 s (expect ~7) — "
                             "timer multiplication is back" % o["p2"])

    def test_on_idle_creates_no_timers(self):
        """Static half: the defect was set_interval living inside on_idle."""
        src = (Path(__file__).resolve().parent.parent /
               "minimalrouter_dashboard.py").read_text()
        body = src.split("def on_idle(self, event):", 1)[1].split("\n    def ", 1)[0]
        self.assertNotIn("set_interval", body,
                         "on_idle must never create timers — it fires on every "
                         "message batch, not once")
        self.assertNotIn("self.poll()", body,
                         "on_idle must not poll either; the timer owns that")
        # scope to MissionControl: the file holds THREE on_mount methods (the
        # modals have their own) and splitting on the first measures the wrong
        # class — the same trap as the on_key scan in the help-modal test.
        mc = src.split("class MissionControl", 1)[1]
        mount = mc.split("def on_mount(self):", 1)[1].split("\n    def ", 1)[0]
        self.assertEqual(mount.count("set_interval"), 2,
                         "exactly two timers, created in MissionControl.on_mount")
        self.assertIn("is None", mount,
                      "timer creation must be guarded against a second call")

    def test_doctor_is_discoverable_from_the_board(self):
        """`doctor` is a shell verb. Nothing in the board said it existed, so
        the instrument built to diagnose an install could not be found by the
        person using the install."""
        src = (Path(__file__).resolve().parent.parent /
               "minimalrouter_dashboard.py").read_text()
        help_body = src.split("class HelpScreen", 1)[1].split("\nclass ", 1)[0]
        self.assertIn("minimalrouter doctor", help_body,
                      "the help overlay must name the doctor command")
        eng = (Path(__file__).resolve().parent.parent /
               "minimalrouter.py").read_text()
        usage = eng.split("def usage():", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("minimalrouter doctor", usage,
                      "usage must point at doctor from the dashboard section")


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestHelpModalCannotCrashTheBoard(unittest.TestCase):
    """1.2.4: HelpScreen.on_key called app.pop_screen() unconditionally. A key
    event queued behind a pop reached the already-removed screen while the stack
    was back to one entry -> ScreenStackError -> Textual rendered a traceback and
    the app EXITED. The same unguarded pop re-delivered the OPENING keystroke to
    the new modal, which closed it before it could be read: help looked like it
    never opened at all.

    Probed against a LIVE app inside run_test(), not a hand-built screen:
    `self.app` is a Textual property that reads the active_app contextvar and
    otherwise walks `_parent` to the App, so a detached fake raises
    NoActiveAppError before on_key runs a single line. Moon's own traceback
    proves the popped screen still resolves `app` through that walk — his crash
    reached pop_screen, not NoActiveAppError — which is exactly the path this
    regression has to force, and it is forced here deterministically by calling
    on_key on a screen that has already been popped.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = _load()
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old = (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH)
        cls.d.MR_HOME = cls._tmp.name
        cls.d.PREF_PATH = os.path.join(cls._tmp.name, "dashboard.json")
        cls.d.LOG_PATH = os.path.join(cls._tmp.name, "minimalrouter.log")
        with open(cls.d.PREF_PATH, "w") as f:
            json.dump({"theme": "midnight", "dense": False, "motion": True}, f)

    @classmethod
    def tearDownClass(cls):
        (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH) = cls._old
        cls._tmp.cleanup()

    class Evt:
        def __init__(self, key="x"):
            self.key = key
            self.stopped = False

        def stop(self):
            self.stopped = True

    def _probe(self):
        d = self.d

        async def go():
            app = d.MissionControl(port=8799)
            out = {}
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break

                def stack():
                    return [type(s).__name__ for s in app._screen_stack]

                # --- open it the way a user does
                await pilot.press("question_mark")
                await pilot.pause(0.4)
                out["opened"] = "HelpScreen" in stack()
                hs = app.screen
                out["live_screen_is_help"] = isinstance(hs, d.HelpScreen)

                # --- CASE A: the keystroke that OPENED the modal must not be
                #     the keystroke that CLOSES it (1.2.4 flash-closed help, so
                #     Moon read it as "help does not open")
                hs._opened_at = d.time.monotonic()
                hs._open_key = "question_mark"
                ev = self.Evt("question_mark")
                err = None
                try:
                    hs.on_key(ev)
                except Exception as e:
                    err = repr(e)
                await pilot.pause(0.2)
                out["echo_err"] = err
                out["still_up_after_echo"] = "HelpScreen" in stack()
                out["echo_consumed"] = ev.stopped

                # --- CASE B: a deliberate key after the guard closes it ONCE
                hs._opened_at = d.time.monotonic() - 1.0
                err = None
                try:
                    hs.on_key(self.Evt("x"))
                except Exception as e:
                    err = repr(e)
                await pilot.pause(0.3)
                out["close_err"] = err
                out["closed_cleanly"] = "HelpScreen" not in stack()
                out["board_restored"] = len(app._screen_stack) == 1

                # --- CASE C: THE CRASH. A queued key reaching the screen that
                #     has ALREADY been popped. 1.2.4 raised ScreenStackError
                #     here and Textual killed the whole board.
                depth = len(app._screen_stack)
                err = None
                try:
                    hs.on_key(self.Evt("question_mark"))
                    hs.on_key(self.Evt("x"))
                except Exception as e:
                    err = repr(e)
                out["detached_err"] = err
                out["detached_depth"] = len(app._screen_stack)
                out["depth_before"] = depth

                # liveness is BEHAVIOURAL, not an internal flag: if the board
                # survived the detached keys, "?" must open help again. A dead
                # app cannot answer a keypress.
                await pilot.press("question_mark")
                await pilot.pause(0.4)
                out["app_alive"] = "HelpScreen" in stack()
                # leave the modal up or down, either way quit cleanly
                if "HelpScreen" in stack():
                    app._screen_stack[-1]._opened_at = d.time.monotonic() - 1.0
                    await pilot.press("x")
                    await pilot.pause(0.3)

                await pilot.press("q")
                await pilot.pause(0.2)
            return out
        return asyncio.run(go())

    def test_help_modal_cannot_crash_or_flash_close(self):
        o = self._probe()
        self.assertTrue(o["opened"], "? must open the help modal")
        self.assertTrue(o["live_screen_is_help"], "the modal owns the screen")
        self.assertIsNone(o["echo_err"],
                          "the opening keystroke's echo must not raise: %s"
                          % o["echo_err"])
        self.assertTrue(o["still_up_after_echo"],
                        "help must survive the keystroke that opened it — in "
                        "1.2.4 it popped itself instantly and read as 'help "
                        "does not open'")
        self.assertTrue(o["echo_consumed"], "the echo is still consumed")
        self.assertIsNone(o["close_err"],
                          "a deliberate close must not raise: %s"
                          % o["close_err"])
        self.assertTrue(o["closed_cleanly"], "any key after the guard closes it")
        self.assertTrue(o["board_restored"], "the board is back to one screen")
        self.assertIsNone(o["detached_err"],
                          "REGRESSION: a key reaching an already-popped modal "
                          "must not raise — 1.2.4 died here with "
                          "ScreenStackError and took the whole board down: %s"
                          % o["detached_err"])
        self.assertEqual(o["detached_depth"], o["depth_before"],
                         "a detached modal must not pop anything")
        self.assertTrue(o["app_alive"], "the board must still be running")

    def test_on_key_never_calls_app_pop_screen_directly(self):
        """Static half of the same regression: the crash came from
        app.pop_screen(). ModalScreen.dismiss() is the guarded path, and the
        stack membership test must run BEFORE either one."""
        src = (Path(__file__).resolve().parent.parent /
               "minimalrouter_dashboard.py").read_text()
        body = src.split("class HelpScreen", 1)[1].split("\nclass ", 1)[0]
        on_key = body.split("def on_key(self, event):", 1)[1]
        on_key = on_key.split("\n    def ", 1)[0] if "\n    def " in on_key else on_key
        self.assertNotIn("app.pop_screen()", on_key,
                         "the crash source must be gone from HelpScreen.on_key")
        self.assertIn("dismiss()", on_key, "closing goes through the modal API")
        self.assertIn("_screen_stack", on_key,
                      "stack membership is checked before popping")
        self.assertIn("_opened_at", on_key,
                      "the opening-keystroke guard is present")

    def test_a_fast_different_key_still_closes_the_help(self):
        """v1.2.5's blanket 0.3 s guard ate LEGITIMATE keys: `?` then `x` in
        quick succession left the modal up, so help looked like it would not
        close. Only the echo of the opening key may be swallowed."""
        # no _prefs(): this class writes deterministic prefs in
        # setUpClass and the probes never press p/v.
        d = self.d

        async def go():
            app = d.MissionControl(port=8799)
            out = {}
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break
                stack = lambda: [type(s).__name__ for s in app._screen_stack]

                async def open_help():
                    """Press ? and wait until the modal is MOUNTED and has taken
                    its opening key. Without this the follow-up key can be
                    delivered before the push settles, which measures the harness
                    race instead of the guard (measured flake: 1 of 3 runs)."""
                    await pilot.press("question_mark")
                    for _ in range(40):
                        await pilot.pause(0.02)
                        scr = app.screen
                        if (type(scr).__name__ == "HelpScreen"
                                and getattr(scr, "_open_key", None) is not None):
                            return True
                    return False

                out["mounted"] = await open_help()
                out["opened"] = "HelpScreen" in stack()
                # A DIFFERENT key closes the modal whether the guard window is
                # active or expired, so this half is timing-INDEPENDENT. The
                # echo behaviour is NOT asserted here: open_help() can spend up
                # to 0.8 s waiting for the mount, which by itself expires the
                # 0.3 s window, so a live echo assertion races the very clock
                # it is trying to measure (measured flake: 1 of 5 full runs).
                # It lives in the deterministic unit probe below instead.
                await pilot.press("x")
                out["closed_fast"] = "HelpScreen" not in stack()
                await pilot.press("q")
                await pilot.pause(0.2)
            return out

        o = asyncio.run(go())
        self.assertTrue(o["mounted"],
                        "the modal must mount and take its opening key, or the "
                        "guard has nothing to compare against")
        self.assertTrue(o["opened"], "? must open help")
        self.assertTrue(o["closed_fast"],
                        "a different key pressed right after ? must close help — "
                        "the blanket guard ate it (v1.2.5 regression)")

    def test_the_echo_behaviour_is_exact_and_deterministic(self):
        """The echo rule, with the clock SET BY HAND instead of raced.

        The guard is: the SAME key, within 0.3 s of mount, consumed ONCE. All
        three branches are driven by direct on_key calls with controlled
        _opened_at / _open_key values, so no pilot timing can flip a result.
        """
        d = self.d

        async def go():
            app = d.MissionControl(port=8799)
            out = {}
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break

                def stack():
                    return [type(s).__name__ for s in app._screen_stack]

                async def mount():
                    await pilot.press("question_mark")
                    for _ in range(60):
                        await pilot.pause(0.02)
                        if stack()[-1] == "HelpScreen":
                            return app.screen
                    return None

                # --- branch 1: same key, INSIDE the window -> swallowed once,
                #     modal stays up, and the echo token is consumed
                hs = await mount()
                if hs is None:
                    return {"mount_failed": True}
                hs._opened_at = d.time.monotonic()
                hs._open_key = "question_mark"
                hs.on_key(self.Evt("question_mark"))
                out["echo_held"] = "HelpScreen" in stack()
                out["echo_consumed"] = getattr(hs, "_open_key", "x") is None

                # --- branch 2: same key again, echo already consumed -> closes
                hs.on_key(self.Evt("question_mark"))
                out["second_closes"] = "HelpScreen" not in stack()

                # --- branch 3: same key but the window EXPIRED -> closes
                hs2 = await mount()
                if hs2 is None:
                    return {"mount_failed": True}
                hs2._opened_at = d.time.monotonic() - 1.0
                hs2._open_key = "question_mark"
                hs2.on_key(self.Evt("question_mark"))
                out["expired_closes"] = "HelpScreen" not in stack()

                # --- branch 4: a DIFFERENT key inside the window -> closes
                #     (this is the v1.2.5 blanket-guard regression)
                hs3 = await mount()
                if hs3 is None:
                    return {"mount_failed": True}
                hs3._opened_at = d.time.monotonic()
                hs3._open_key = "question_mark"
                hs3.on_key(self.Evt("x"))
                out["different_key_closes"] = "HelpScreen" not in stack()

                await pilot.press("q")
                await pilot.pause(0.2)
            return out

        o = asyncio.run(go())
        self.assertFalse(o.get("mount_failed"),
                         "the help modal must mount for the echo branches to "
                         "measure anything")
        self.assertTrue(o["echo_held"],
                        "the opening key's own echo must be swallowed while the "
                        "window is open, or help flashes shut as it did in 1.2.4")
        self.assertTrue(o["echo_consumed"],
                        "the echo must be consumed exactly once, never held")
        self.assertTrue(o["second_closes"],
                        "after the echo is consumed the same key must close it")
        self.assertTrue(o["expired_closes"],
                        "once the window expires the same key must close it")
        self.assertTrue(o["different_key_closes"],
                        "a different key must close it inside the window — the "
                        "blanket guard ate exactly this")

    def test_no_app_level_help_guard_exists(self):
        """The 1.2.3 lesson: an app-level 'help is open' flag that is never
        cleared freezes the whole keyboard. Textual modals own key events, so
        MissionControl must not carry one."""
        src = (Path(__file__).resolve().parent.parent /
               "minimalrouter_dashboard.py").read_text()
        self.assertNotIn("self.help_open", src,
                         "no app-level help guard may return")


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestBatchTestScope(unittest.TestCase):
    """`t` ignored marks (3 marked rows + t tested only the cursor row), and `T`
    was documented 'all enabled' while _targets() returned the cursor row
    whenever nothing was marked — so T tested ONE row and announced the toast as
    the whole pool. Disabled rows must never be in any batch.

    Driven through REAL KEYSTROKES on a live app, not by calling _test_many():
    the defect lives in the on_key binding (`t` -> _test_row), so a method-level
    test would pass against broken code.
    """

    ROWS = [{"name": "r%d" % i, "model": "m%d" % i,
             "base": "https://x%d/v1" % i, "class": "api",
             "provider": "api.x%d.com" % i, "oauth_provider": "",
             "disabled": (i == 3), "cw": 131072, "key_src": "pasted",
             "ok_pct": 100,
             "state": {"status": "ok", "ms": 10, "hist": [10], "tps": 10.0,
                       "fail_streak": 0, "recent": [1], "at": 0}}
            for i in range(5)]

    @classmethod
    def setUpClass(cls):
        cls.d = _load()
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old = (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH)
        cls.d.MR_HOME = cls._tmp.name
        cls.d.PREF_PATH = os.path.join(cls._tmp.name, "dashboard.json")
        cls.d.LOG_PATH = os.path.join(cls._tmp.name, "minimalrouter.log")
        with open(cls.d.PREF_PATH, "w") as f:
            json.dump({"theme": "midnight", "dense": False, "motion": True}, f)

    @classmethod
    def tearDownClass(cls):
        (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH) = cls._old
        cls._tmp.cleanup()

    def _run(self, press, marks=None, sel=0):
        """Mount the board with stubbed transport, press KEYS, return the probes
        that were actually issued plus the final toast."""
        d = self.d

        async def go():
            app = d.MissionControl(port=8799)
            calls = []

            def api(method, path, body=None, timeout=30):
                calls.append((method, path, body))
                return {"ok": True, "ms": 10}
            out = {}

            async def boot(pilot):
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break
                app.online = True
                app.rows = [dict(r) for r in self.ROWS]
                app.nmodels = 20
                app.stats = {"rows": 5, "serving": 4, "down": 1, "unknown": 0,
                             "req_min": 0, "tok_s": 0, "err_pct": 0,
                             "uptime": 1}
                app.probe = {"done": 0, "total": 0, "active": False}
                app.sel = sel
                app.marked = set(marks or ())
                app.api = api
                app.ensure_slots(len(app.rows))
                for i in range(len(app.row_statics)):
                    app.row_statics["slot%d" % i].display = i < len(app.rows)
                app.repaint_rows()
                app.update_selected()
                await pilot.pause(0.3)
                for k in press:
                    await pilot.press(k)
                    await pilot.pause(0.25)
                # the batch runs in a worker thread. Wait for it to START (an
                # empty scope never starts, so cap that wait short), then DRAIN.
                # A single "until calls appear" loop would burn the full timeout
                # on the parked-only case, where no call is ever legal.
                import time as _t
                end = _t.monotonic() + 1.5
                while _t.monotonic() < end and not app.testing:
                    await pilot.pause(0.05)
                end = _t.monotonic() + 8.0
                while _t.monotonic() < end and app.testing:
                    await pilot.pause(0.05)
                await pilot.pause(0.3)
                out["calls"] = list(calls)
                out["toast"] = app.toast
                await pilot.press("q")
                await pilot.pause(0.2)

            async with app.run_test(size=(120, 40)) as pilot:
                await boot(pilot)
            return out
        return asyncio.run(go())

    def _models(self, out):
        return sorted(c[2]["model"] for c in out["calls"]
                      if c[0] == "POST" and c[1] == "/api/test")

    def test_t_with_marks_probes_every_marked_row(self):
        o = self._run(["t"], marks={"r0", "r1", "r2"})
        self.assertEqual(self._models(o), ["m0", "m1", "m2"],
                         "spc x3 then t must probe all 3 — 1.2.4 bound t to "
                         "_test_row(), which reads the cursor row and never "
                         "looks at marks, so Moon saw one probe where he asked "
                         "for three")

    def test_t_without_marks_probes_the_cursor_row(self):
        o = self._run(["t"], sel=2)
        self.assertEqual(self._models(o), ["m2"],
                         "with no marks t still has to test the row under the "
                         "cursor")

    def test_T_probes_every_enabled_row_and_skips_parked(self):
        o = self._run(["T"])
        self.assertEqual(self._models(o), ["m0", "m1", "m2", "m4"],
                         "T is the whole pool; r3 is disabled and must not "
                         "burn a probe")

    def test_T_ignores_marks(self):
        o = self._run(["T"], marks={"r0"})
        self.assertEqual(len(self._models(o)), 4,
                         "T is every enabled row, not the selection")

    def test_toast_names_the_scope_actually_tested(self):
        """1.2.4 tested ONE row on an unmarked T and printed 'all enabled' — a
        toast that lies is worse than a missing toast. The single-row `t` path
        has its own legitimate per-row result format, so the invariant is:
        however many rows were probed, the toast must not claim the pool unless
        the pool was probed."""
        o = self._run(["t"], sel=1)                     # 1 row, unmarked
        self.assertEqual(len(self._models(o)), 1)
        self.assertNotIn("all enabled", o["toast"],
                         "one probe must not be announced as the pool: got %r"
                         % o["toast"])
        o2 = self._run(["T"])                           # 4 rows
        self.assertEqual(len(self._models(o2)), 4)
        self.assertIn("all enabled", o2["toast"],
                      "T probes the pool, so it may name the pool: got %r"
                      % o2["toast"])
        o3 = self._run(["t"], marks={"r0", "r1"})       # 2 rows, marked
        self.assertIn("marked", o3["toast"],
                      "a marked batch must say it was the marks: got %r"
                      % o3["toast"])

    def test_parked_only_scope_reports_instead_of_lieing(self):
        o = self._run(["t"], marks={"r3"})
        self.assertEqual(self._models(o), [],
                         "no probe may be issued on a parked row")
        self.assertIn("no enabled rows", o["toast"],
                      "an empty scope must say so: got %r" % o["toast"])

    def test_space_then_t_is_the_selection_loop(self):
        """The full gesture Moon performed: mark three rows with space, then
        test. Space must land on the rows the cursor visited, not just one."""
        o = self._run(["space", "down", "space", "down", "space", "t"])
        self.assertEqual(self._models(o), ["m0", "m1", "m2"],
                         "three spaces over three rows then t must probe all "
                         "three")

    def test_parallel_batch_probes_each_row_exactly_once(self):
        """The batch went parallel in 1.2.5; the invariant is one request per
        row in scope, not the order."""
        o = self._run(["T"])
        got = self._models(o)
        self.assertEqual(len(got), len(set(got)), "no duplicate probe")
        self.assertEqual(got, ["m0", "m1", "m2", "m4"])

@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class TestKeysDoNotBlockTheUIThread(unittest.TestCase):
    """1.2.4 froze the board on four keys: `r` did a synchronous /api/refresh on
    the UI thread (30s timeout), and `p` / `P` / `v` each called poll() — a
    synchronous /api/state plus a rebuild of every panel. Moon felt it as 'ekta
    key chapar por onekkhok wait'. A key handler may not spend wall-clock time on
    the network.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = _load()
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old = (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH)
        cls.d.MR_HOME = cls._tmp.name
        cls.d.PREF_PATH = os.path.join(cls._tmp.name, "dashboard.json")
        cls.d.LOG_PATH = os.path.join(cls._tmp.name, "minimalrouter.log")
        with open(cls.d.PREF_PATH, "w") as f:
            json.dump({"theme": "midnight", "dense": False, "motion": True}, f)
        root = Path(__file__).resolve().parent.parent
        cls.src = (root / "minimalrouter_dashboard.py").read_text()
        # scope to MissionControl: the file holds THREE on_key methods and the
        # first match in file order is AddScreen's
        cls.mc = cls.src.split("class MissionControl", 1)[1]

    @classmethod
    def tearDownClass(cls):
        (cls.d.MR_HOME, cls.d.PREF_PATH, cls.d.LOG_PATH) = cls._old
        cls._tmp.cleanup()

    def _branch(self, key):
        """The source of one `elif k == <key>:` branch inside on_key."""
        on_key = self.mc.split("def on_key(self, event):", 1)[1]
        on_key = on_key.split("\n    def ", 1)[0]
        needle = 'k == "%s"' % key
        self.assertIn(needle, on_key, "%s branch missing" % key)
        tail = on_key.split(needle, 1)[1]
        nxt = len(tail)
        for marker in ("\n        elif ", "\n        if ", "\n    def "):
            i = tail.find(marker)
            if i != -1:
                nxt = min(nxt, i)
        return tail[:nxt]

    def test_refresh_row_returns_before_the_network_does(self):
        import time as _t
        d = self.d
        app = d.MissionControl(port=8799)
        app.online = True
        app.rows = [{"name": "nous-free", "model": "m", "base": "https://x/v1",
                     "class": "oauth", "provider": "", "oauth_provider": "nous",
                     "disabled": False, "cw": None, "key_src": "oauth",
                     "ok_pct": None, "state": {}}]
        app.sel = 0
        app.testing = {}
        app.toast = ""
        app.repaint_rows = lambda: None
        app.update_events = lambda: None
        app.update_selected = lambda **kw: None
        app.poll = lambda: None
        app.call_from_thread = lambda fn, *a, **k: None

        def slow_api(*a, **k):
            _t.sleep(1.0)
            return {"results": [{"ok": True, "message": "refreshed"}]}
        app.api = slow_api

        t0 = _t.monotonic()
        app._refresh_row()
        elapsed = _t.monotonic() - t0
        self.assertLess(elapsed, 0.25,
                        "_refresh_row spent %.2fs on the UI thread — it must "
                        "hand the request to a worker like _test_row did"
                        % elapsed)
        self.assertIn("refreshing", app.toast, "the toast announces the work")

    def test_theme_and_density_keys_do_not_poll(self):
        for key in ("p", "P", "v"):
            branch = self._branch(key)
            self.assertNotIn("self.poll()", branch,
                             'key "%s" must not call the blocking poll()' % key)
            self.assertIn("rerender_local", branch,
                          'key "%s" must repaint from cache' % key)

    def test_motion_key_does_not_poll_either(self):
        branch = self._branch("m")
        self.assertNotIn("self.poll()", branch)

    def test_rerender_local_exists_and_touches_no_network(self):
        m = re.search(r"    def rerender_local\(self\):(.*?)\n    def ",
                      self.mc, re.S)
        self.assertIsNotNone(m, "rerender_local must exist")
        body = m.group(1)
        self.assertNotIn("self.api(", body, "no HTTP in a local repaint")
        self.assertNotIn("self.poll()", body)
        for panel in ("render_wordmark", "rebuild_stats", "repaint_rows",
                      "update_selected", "update_events"):
            self.assertIn(panel, body, "local repaint must cover %s" % panel)

    def test_cursor_move_repaints_two_rows_not_the_table(self):
        """Every arrow press ran repaint_rows() over the whole table — the
        'scroll late e hocche' Moon felt. row_line() is the unit of work, so
        counting it measures the real cost."""
        d = self.d

        async def go():
            app = d.MissionControl(port=8799)
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(40):
                    await pilot.pause(0.1)
                    if app._tier_now() == getattr(app, "_tier_key", None):
                        break
                app.online = True
                app.rows = [dict(r) for r in TestBatchTestScope.ROWS] * 4
                app.nmodels = 20
                app.stats = {"rows": 20, "serving": 10, "down": 10,
                             "unknown": 0, "req_min": 0, "tok_s": 0,
                             "err_pct": 0, "uptime": 1}
                app.probe = {"done": 0, "total": 0, "active": False}
                app.ensure_slots(len(app.rows))
                for i in range(len(app.row_statics)):
                    app.row_statics["slot%d" % i].display = i < len(app.rows)
                app.repaint_rows()
                app.update_selected()
                await pilot.pause(0.3)

                n = {"c": 0}
                real = app.row_line

                def counted(r, sel):
                    n["c"] += 1
                    return real(r, sel)
                app.row_line = counted
                await pilot.press("down")
                await pilot.pause(0.25)
                one_move = n["c"]
                await pilot.press("q")
                await pilot.pause(0.2)
            return one_move

        rows = asyncio.run(go())
        self.assertGreaterEqual(rows, 1, "the new cursor row must repaint")
        self.assertLessEqual(rows, 4,
                             "one arrow press rebuilt %d rows of 20 — cursor "
                             "movement must repaint only the rows that changed"
                             % rows)

    def test_anim_tick_is_not_faster_than_0_25(self):
        """The 0.15s tick rebuilt the wordmark (4 Texts at full frame width)
        6.7x/sec forever. 0.25s with the shimmer on alternate ticks halves it."""
        m = re.search(r"set_interval\(([0-9.]+), self\.tick_anim\)", self.mc)
        self.assertIsNotNone(m, "anim timer must exist")
        self.assertGreaterEqual(float(m.group(1)), 0.25,
                                "anim tick %s s is the 1.2.4 rate that made the "
                                "board feel laggy" % m.group(1))


class TestLabelsMatchWhatTheKeysDo(unittest.TestCase):
    """The legend said `m=move` while the key toggles MOTION. A stranger reading
    the one-line key map presses m expecting cursor movement and gets an
    animation freeze. A label that lies is its own defect class."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.root = root
        cls.src = (root / "minimalrouter_dashboard.py").read_text()
        cls.mc = cls.src.split("class MissionControl", 1)[1]

    def test_legend_labels_motion_not_move(self):
        self.assertNotIn('"m", "move"', self.src,
                         "the m label must not say 'move'")
        self.assertIn('"m", "motion"', self.src)

    def test_help_and_usage_agree_with_the_code(self):
        dash = self.src
        eng = (self.root / "minimalrouter.py").read_text()
        self.assertIn("EVERY enabled row", dash, "help must describe T honestly")
        self.assertIn("test marked rows (the cursor row if none marked)", eng,
                      "usage must describe t honestly")
        self.assertIn("T        test EVERY enabled row", eng)
        self.assertNotIn("t        test row          T", eng,
                         "the old t/T line promised what the code did not do")

    def test_col_plan_is_cached_per_frame(self):
        """row_line() called col_plan() once per row: a 21-row repaint
        recomputed the identical plan 21 times."""
        m = re.search(r"    def col_plan\(self\):(.*?)\n    # ", self.mc,
                      re.S)
        self.assertIsNotNone(m, "col_plan must exist")
        body = m.group(1)
        self.assertIn("_plan_key", body, "col_plan must memoize on its key")
        self.assertIn("_plan", body)

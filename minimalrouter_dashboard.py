"""MinimalRouter — Mission Control dashboard (bundled Textual board).

This module is the enhanced dashboard. It is imported lazily by minimalrouter.py
only when the user runs the interactive TUI; if `textual` is not installed the
import fails and the core falls back to its stdlib curses board. The router core
itself never depends on this file or on Textual.

Design (v1.2.0, Moon's rulings 2026-11-21): the btop-class console.
- One centered max-width frame; hero zone (wordmark, URL bar, stat tiles) is
  dead-center, data table left-flush inside the centered frame.
- 8 pixel-digit stat tiles (5x5 block face, >= half the wordmark's visual mass):
  ROWS / MODELS / SERVING / BLOCKED / REQ-MIN / TOK/S / ERR% / UPTIME.
  Numbers colored per meaning; labels are one text row (1/4 wordmark).
- Route table: ROW · PROV · CLASS · MODEL · CW · LAT · TPS · OK% · STATUS,
  with richer statuses (COOLDOWN/STALE/DEAD/OFF) and multi-row marks.
- SELECTED: model name highlighted, provider lighter-but-highlighted, plus
  pixel gauges (TPS, CW) at half-wordmark height, auto-updating.
- EVENTS: every log class (OK/FAIL/TEST/ADD/REMOVE/DISABLE/ENABLE/PROBE/
  OAUTH-REFRESH/CATALOG/boot), auto-updating, scrollable (Tab focuses it).
- Motion: wordmark shimmer, mast typewriter + pulse, stat flash on change;
  `m` pauses all animation timers.
- 14 themes incl. Nous and Claude; p/P cycle; density v.
- Add row in-board (a -> modal, test-before-save via /api/add); batch test (T),
  batch disable/enable (e), batch delete with confirm (d,d), refresh all (R),
  catalog re-fetch (f), help overlay (?).

Robustness: offline-tolerant polling, guarded widget queries, grow-only row
slots, theme/density/motion persistence, clean quit (the core shuts the server
down after return).
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

BLOCKS = "▁▂▃▄▅▆▇█"
SPIN = "⣾⣽⣻⢿⡿⣟⣯⣷"
PULSE = "◐◑◒◓"

# Status -> palette key. Every classify() outcome maps here; unknown-safe.
STATUS_KEY = {"ok": "ok", "gated": "bad", "empty": "warn", "error": "bad",
              "cooldown": "warn", "stale": "dim", "dead": "bad",
              "unknown": "dim", "off": "dim"}

THEMES = [
    dict(key="midnight", bg="#0a0c10", line="#222835", accent="#4fd1c5",
         strong="#e6e9ef", muted="#9aa2b4", dim="#5c6478", sel="#123330",
         ok="#57d9a3", warn="#e0b34f", bad="#e06c75", dark=True),
    dict(key="ocean", bg="#0a1018", line="#1e3350", accent="#5ab0ff",
         strong="#e8f0fa", muted="#8aa0bc", dim="#4a6080", sel="#16304f",
         ok="#4fd6a8", warn="#e8c56a", bad="#f07a7a", dark=True),
    dict(key="ember", bg="#100d0a", line="#3a2c1d", accent="#ffb454",
         strong="#f5e8d8", muted="#b09878", dim="#6a5a45", sel="#3a2817",
         ok="#a8d96a", warn="#e8a04a", bad="#f0705e", dark=True),
    # Nous Research: deep indigo-violet, electric lavender accent.
    dict(key="nous", bg="#0b0a14", line="#2a2350", accent="#8b7cf6",
         strong="#ece9ff", muted="#a79fd0", dim="#574f80", sel="#221a45",
         ok="#6ee7a8", warn="#f6c453", bad="#f2777a", dark=True),
    # Claude (Anthropic): warm cream, coral accent — the light board.
    dict(key="claude", bg="#f0eee6", line="#d8d2c4", accent="#d97757",
         strong="#191919", muted="#6d6a63", dim="#a39f96", sel="#f5e0d7",
         ok="#3e8e5a", warn="#b7791f", bad="#c74634", dark=False),
    dict(key="dracula", bg="#282a36", line="#44475a", accent="#bd93f9",
         strong="#f8f8f2", muted="#8b95c9", dim="#6272a4", sel="#44475a",
         ok="#50fa7b", warn="#ffb86c", bad="#ff5555", dark=True),
    dict(key="gruvbox", bg="#282828", line="#504945", accent="#fabd2d",
         strong="#ebdbb2", muted="#a89984", dim="#665c54", sel="#3c3836",
         ok="#b8bb26", warn="#fe8019", bad="#fb4934", dark=True),
    dict(key="nord", bg="#2e3440", line="#4c566a", accent="#88c0d0",
         strong="#eceff4", muted="#d8dee9", dim="#616e88", sel="#434c5e",
         ok="#a3be8c", warn="#ebcb8b", bad="#bf616a", dark=True),
    dict(key="tokyo", bg="#1a1b26", line="#292e42", accent="#7aa2f7",
         strong="#c0caf5", muted="#7f8ab8", dim="#444b6a", sel="#283457",
         ok="#9ece6a", warn="#e0af68", bad="#f7768e", dark=True),
    dict(key="monokai", bg="#272822", line="#3e3d32", accent="#66d9ef",
         strong="#f8f8f2", muted="#a59f85", dim="#75715e", sel="#49483e",
         ok="#a6e22e", warn="#fd971f", bad="#f92672", dark=True),
    dict(key="solarized", bg="#002b36", line="#073642", accent="#268bd2",
         strong="#fdf6e3", muted="#93a1a1", dim="#586e75", sel="#073642",
         ok="#859900", warn="#b58900", bad="#dc322f", dark=True),
    dict(key="onedark", bg="#282c34", line="#3e4451", accent="#61afef",
         strong="#dfe4ec", muted="#9aa2b1", dim="#5c6370", sel="#3e4451",
         ok="#98c379", warn="#e5c07b", bad="#e06c75", dark=True),
    dict(key="crtgreen", bg="#0a120a", line="#1d3a1d", accent="#33ff33",
         strong="#b7ffb7", muted="#57c957", dim="#1f7a1f", sel="#0f2a0f",
         ok="#33ff33", warn="#d9e033", bad="#ff6464", dark=True),
    dict(key="crtamber", bg="#140e05", line="#3a2a10", accent="#ffb000",
         strong="#ffd98a", muted="#c8922a", dim="#7a5c1e", sel="#2a1d08",
         ok="#ffc233", warn="#ff8f1f", bad="#ff4f4f", dark=True),
]
THEME_KEYS = [t["key"] for t in THEMES]

MR_HOME = os.path.expanduser("~/.minimalrouter")
LOG_PATH = os.path.join(MR_HOME, "minimalrouter.log")
PREF_PATH = os.path.join(MR_HOME, "dashboard.json")


# ---------------------------------------------------------------- wordmark
# 5x7 pixel font rendered with half-blocks: each terminal row = 2 pixel rows,
# giving smooth SOLID letters. Two-tone split like opencode's open/code.
FONT = {
    'M': ["10001", "11011", "10101", "10001", "10001", "10001", "10001"],
    'I': ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    'N': ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    'A': ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    'L': ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    'R': ["11110", "10001", "10001", "11110", "10100", "10110", "10011"],
    'O': ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    'U': ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    'T': ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    'E': ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    ' ': ["00000"] * 7,
}


def wordmark_rows(text, gap=1):
    text = text.upper()
    glyphs = [FONT.get(c, FONT[' ']) for c in text]
    rows = []
    for pr in range(0, 7, 2):
        line = []
        for gi, g in enumerate(glyphs):
            top = g[pr]
            bot = g[pr + 1] if pr + 1 < 7 else "00000"
            for x in range(5):
                t, b = top[x] == '1', bot[x] == '1'
                line.append('█' if t and b else '▀' if t else '▄' if b else ' ')
            if gi < len(glyphs) - 1:
                line.extend([' '] * gap)
        rows.append(''.join(line))
    return rows


def split_index(text, split_word):
    n = len(split_word)
    return n * 5 + (n - 1) * 1


# ------------------------------------------------------- 5x5 pixel stat face
# Digits + the few letters stat values can carry (K/M/G suffixes, H/M/D uptime,
# OK status). Renders 3 terminal rows (pairs 0-1, 2-3, 4-pad) — >= half the
# wordmark's 4-row height, comfortably above the 1-row label text.
PF = {
    '0': ["11111", "10001", "10001", "10001", "11111"],
    '1': ["01110", "00100", "00100", "00100", "01110"],
    '2': ["11111", "00001", "11111", "10000", "11111"],
    '3': ["11111", "00001", "11111", "00001", "11111"],
    '4': ["10001", "10001", "11111", "00001", "00001"],
    '5': ["11111", "10000", "11111", "00001", "11111"],
    '6': ["11111", "10000", "11111", "10001", "11111"],
    '7': ["11111", "00001", "00010", "00100", "00100"],
    '8': ["11111", "10001", "11111", "10001", "11111"],
    '9': ["11111", "10001", "11111", "00001", "11111"],
    ':': ["00000", "00100", "00000", "00100", "00000"],
    '.': ["00000", "00000", "00000", "00000", "00100"],
    '-': ["00000", "00000", "11111", "00000", "00000"],
    '+': ["00000", "00100", "11111", "00100", "00000"],
    '%': ["10001", "00010", "00100", "01000", "10001"],
    'K': ["10001", "10010", "11100", "10010", "10001"],
    'M': ["10001", "11011", "10101", "10001", "10001"],
    'G': ["01110", "10000", "10111", "10001", "01111"],
    'H': ["10001", "10001", "11111", "10001", "10001"],
    'D': ["11110", "10001", "10001", "10001", "11110"],
    'S': ["01111", "10000", "01110", "00001", "11110"],
    'O': ["01110", "10001", "10001", "10001", "01110"],
    'K2': None,  # placeholder removed below (keep dict clean)
    ' ': ["00000"] * 5,
}
PF.pop('K2', None)


def px_rows(text):
    """text -> 3 terminal rows of half-block pixels (5x5 face, 1-col gap)."""
    glyphs = []
    for c in str(text):
        g = PF.get(c) or PF.get(c.upper()) or PF[' ']
        glyphs.append(g)
    out = []
    for pr in (0, 2, 4):
        line = []
        for gi, g in enumerate(glyphs):
            top = g[pr]
            bot = g[pr + 1] if pr + 1 < 5 else "00000"
            for x in range(5):
                t, b = top[x] == '1', bot[x] == '1'
                line.append('█' if t and b else '▀' if t else '▄' if b else ' ')
            if gi < len(glyphs) - 1:
                line.append(' ')
        out.append(''.join(line))
    return out


def compact(v):
    """Number -> <= 3 glyphs for the pixel tiles (1234 -> '1K', 999 -> '999')."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    if v >= 1e9:
        return "%dG" % round(v / 1e9)
    if v >= 1e6:
        return "%dM" % round(v / 1e6)
    if v >= 1e3:
        return "%dK" % round(v / 1e3)
    return str(int(round(v)))


def uptime_str(sec):
    sec = int(sec or 0)
    if sec >= 86400:
        return "%dD" % (sec // 86400)
    if sec >= 3600:
        return "%dH" % (sec // 3600)
    return "%dM" % max(1, sec // 60)


def cw_str(cw):
    if not cw:
        return "-"
    if cw >= 1000:
        return "%dK" % round(cw / 1000.0)
    return str(cw)


# ---------------------------------------------------------------- helpers
def clip(s, w):
    s = str(s)
    return s if len(s) <= w else s[:w - 1] + "…"


def classify(state):
    """Mirror of the engine's classify_status (the board renders what the engine
    computed when it can; this is the offline/legacy fallback path)."""
    st = (state or {}).get("status", "unknown")
    if st == "ok":
        at = float((state or {}).get("at") or 0)
        if at and time.time() - at > 1800:
            return "stale"
        return "ok"
    if st != "fail":
        return "unknown"
    err = str((state or {}).get("error", ""))
    m = re.search(r"HTTP Error (\d{3})", err)
    code = m.group(1) if m else ""
    if code == "403":
        return "gated"
    if code == "402":
        return "empty"
    if int((state or {}).get("fail_streak", 0) or 0) >= 3:
        return "dead"
    if code == "429":
        return "cooldown"
    return "error"


def prov_short(name):
    n = str(name or "?")
    for p in ("api.", "www.", "free.", "openrouter.", "models."):
        if n.startswith(p):
            n = n[len(p):]
    host = n.split(".")[0][:10]
    return host.upper() or "?"


def load_prefs():
    try:
        with open(PREF_PATH) as f:
            d = json.load(f)
        theme = d.get("theme", "midnight")
        if theme not in THEME_KEYS:
            theme = "midnight"
        return {"theme": theme, "dense": bool(d.get("dense", False)),
                "motion": d.get("motion", True) is not False}
    except Exception:
        return {"theme": "midnight", "dense": False, "motion": True}


def save_prefs(theme_key, dense, motion):
    try:
        os.makedirs(MR_HOME, exist_ok=True)
        tmp = PREF_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"theme": theme_key, "dense": bool(dense),
                       "motion": bool(motion)}, f)
        os.replace(tmp, PREF_PATH)
    except Exception:
        pass


EV_KINDS = (("OK", "ok"), ("FAIL", "bad"), ("TEST", "accent"), ("ADD", "warn"),
            ("REMOVE", "bad"), ("DISABLE", "warn"), ("ENABLE", "ok"),
            ("PROBE", "dim"), ("OAUTH-REFRESH", "warn"), ("CATALOG", "dim"),
            ("starting", "dim"), ("stopped", "dim"), ("stream", "warn"))


def ev_kind(body):
    first = body.split(" ", 1)[0]
    for tok, kind in EV_KINDS:
        if first == tok or body.startswith("MinimalRouter " + tok) \
                or body.startswith("stream "):
            return kind
    return "dim"


def parse_event_line(ln):
    """One log line -> (HH:MM:SS, body, kind) or None. Pure, testable offline."""
    ln = str(ln).strip()
    if not ln:
        return None
    m = re.match(r"\[\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d)\] (.*)", ln)
    if not m:
        return None
    return (m.group(1), m.group(2), ev_kind(m.group(2)))


def plan_cols(fw):
    """Frame width -> (columns, widths). Drops CW/OK/TPS before shrinking the
    identity columns; MODEL shrinks to 14 first, ROW never below 12. Pure."""
    cols = ["ROW", "PROV", "CLASS", "MODEL", "CW", "LAT", "TPS", "OK", "STATUS"]
    w = {"ROW": 22, "PROV": 10, "CLASS": 8, "MODEL": 28, "CW": 6,
         "LAT": 8, "TPS": 6, "OK": 5, "STATUS": 9}
    total = 3 + sum(w.values())
    if total > fw:
        slack = total - fw
        take = min(slack, w["MODEL"] - 14)
        w["MODEL"] -= take
        slack -= take
        for drop in ("CW", "OK", "TPS"):
            if slack <= 0:
                break
            if drop in cols:
                cols.remove(drop)
                slack -= w[drop]
        if slack > 0:
            take = min(slack, w["PROV"] - 6)
            w["PROV"] -= take
            slack -= take
        if slack > 0:
            w["ROW"] = max(12, w["ROW"] - slack)
    return cols, w


# ---------------------------------------------------------------- add modal
ADD_CSS = """
AddScreen { align: center middle; }
#addbox { width: 72%; max-width: 78; height: auto; border: round $panel;
          padding: 1 2; }
#adddesc { height: 1; margin-bottom: 1; }
#addbox Input { margin-bottom: 1; }
"""


class AddScreen(ModalScreen):
    """Add an API/keyless row without leaving the board. The engine tests the
    row before saving (/api/add), so a failing paste never enters rows.json."""

    CSS = ADD_CSS

    def __init__(self, api, done_cb):
        super().__init__()
        self.api = api
        self.done_cb = done_cb

    def compose(self) -> ComposeResult:
        with Vertical(id="addbox"):
            yield Static(Text("ADD ROW  (Esc cancels)", style="bold"), id="adddesc")
            yield Input(placeholder="base URL  (https://…/v1)", id="in-base")
            yield Input(placeholder="API key  (empty = keyless row)", id="in-key")
            yield Input(placeholder="model id", id="in-model")
            yield Input(placeholder="row name  (optional)", id="in-name")

    def on_mount(self):
        try:
            self.query_one("#in-base", Input).focus()
        except Exception:
            pass

    def on_input_submitted(self, event):
        event.stop()
        order = ["in-base", "in-key", "in-model", "in-name"]
        cur = event.input.id
        if cur in order[:-1]:
            try:
                self.query_one("#" + order[order.index(cur) + 1], Input).focus()
            except Exception:
                pass
            return
        try:
            base = self.query_one("#in-base", Input).value.strip()
            key = self.query_one("#in-key", Input).value.strip()
            model = self.query_one("#in-model", Input).value.strip()
            name = self.query_one("#in-name", Input).value.strip()
        except Exception:
            base = key = model = name = ""
        if not base or not model:
            self.done_cb("add: base URL + model are required")
            self.app.pop_screen()
            return
        self.app.pop_screen()

        def run():
            res = self.api("POST", "/api/add",
                           {"base": base, "key": key, "model": model, "name": name})
            msg = ("added %s" % res.get("name", "")) if res.get("ok") \
                else ("add failed: %s" % str(res.get("error", ""))[:80])
            # Widget-touching callback must run on the app's event loop thread.
            self.app.call_from_thread(self.done_cb, msg)
        threading.Thread(target=run, daemon=True).start()

    def on_key(self, event):
        if event.key == "escape":
            event.stop()
            self.app.pop_screen()


# ---------------------------------------------------------------- the app
def _build_css():
    base = """
    Screen { align: center top; }
    #frame { width: 96%; max-width: 148; height: 100%; border: round #222835;
             padding: 0 2; }
    .wordmark { height: 1; text-align: center; }
    .wmcompact { display: none; height: 1; text-align: center; }
    Screen.dense .wordmark { display: none; }
    Screen.dense .wmcompact { display: block; }
    #mast { height: 1; text-align: center; margin-bottom: 1; }
    .box { border: round #222835; }
    .box.focused { border: heavy #4fd1c5; }
    .box { border-title-color: #4fd1c5; border-title-style: bold; }
    #stats { height: 7; margin-bottom: 1; }
    Screen.dense #stats { height: 4; }
    .stat { width: 1fr; }
    .statnum { height: 3; text-align: center; overflow-x: hidden; }
    Screen.dense .statnum { height: 1; }
    .statlab { height: 1; text-align: center; }
    #tablebox { height: 1fr; }
    #table { height: 1fr; overflow-y: auto; }
    .colhead { height: 1; }
    .row { height: 1; padding: 0 1; }
    .row.sel { background: #123330; }
    #selbox { height: auto; margin-top: 1; }
    #selrow { height: auto; }
    #selid { width: 1fr; padding: 0 1; }
    .gauge { width: 26; padding: 0 1; }
    .gnum { height: 3; text-align: center; overflow-x: hidden; }
    .glab { height: 1; text-align: center; }
    #evbox { height: 9; margin-top: 1; }
    Screen.dense #evbox { height: 6; }
    #evbody { height: 100%; padding: 0 1; }
    #toast { height: 1; margin-top: 1; padding-left: 1; }
    #foot { height: auto; margin-top: 1; border: round #222835; padding: 0 1; }
    #help { display: none; height: auto; margin-top: 1; border: round #222835;
            padding: 0 1; }
    #help.open { display: block; }
    * { scrollbar-size: 1 1; }
    """
    blocks = []
    for t in THEMES:
        k = t["key"]
        blocks.append("""
        Screen.theme-{k} {{ background: {bg}; }}
        .theme-{k} #frame {{ border: round {line}; }}
        .theme-{k} .box {{ border: round {line}; border-title-color: {accent};
                           border-title-background: {bg}; }}
        .theme-{k} .box.focused {{ border: heavy {accent}; }}
        .theme-{k} .row.sel {{ background: {sel}; }}
        .theme-{k} #foot {{ border: round {line}; }}
        .theme-{k} #help {{ border: round {line}; }}
        .theme-{k} * {{ scrollbar-color: {line}; scrollbar-background: {bg}; }}
        """.format(k=k, **t))
    return base + "".join(blocks)


class MissionControl(App):
    TITLE = "MinimalRouter — Mission Control"
    CSS = _build_css()

    def __init__(self, port=8790):
        super().__init__()
        self.port = port
        prefs = load_prefs()
        self.theme_ix = THEME_KEYS.index(prefs["theme"])
        self.dense = prefs["dense"]
        self.motion = prefs["motion"]
        self.sel = 0
        self.rows = []
        self.nmodels = 0
        self.stats = {}
        self.probe = {"done": 0, "total": 0, "active": False}
        self.online = True
        self.row_statics = {}
        self.testing = {}
        self.marked = set()
        self.toast = "ready"
        self.live_ix = 0
        self.shimmer = 0
        self.type_n = 0
        self.flash = {}
        self.prev_tiles = {}
        self.events = []
        self.log_off = None
        self.ev_scroll = 0
        self.zone = "table"
        self.help_open = False
        self.del_arm = (None, 0.0)
        self.server_ver = ""

    def pal(self):
        return THEMES[self.theme_ix]

    def api(self, method, path, body=None, timeout=30) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        rq = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(rq, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"error": f"HTTP {e.code}"}
        except Exception as e:
            return {"error": str(e)}

    # ---------- mount / timers ----------
    def on_mount(self):
        self.screen.set_class(self.dense, "dense")
        self._apply_theme_classes()
        for wid, title in (("#stats", "POOL"), ("#tablebox", "ROUTES"),
                           ("#selbox", "SELECTED"), ("#evbox", "EVENTS")):
            try:
                self.query_one(wid).border_title = title
            except Exception:
                pass
        self._poll_timer = self.set_interval(2.0, self.poll)
        self._anim_timer = self.set_interval(0.15, self.tick_anim)
        if not self.motion:
            self._anim_timer.pause()
        self.poll()

    def _apply_theme_classes(self):
        for i, t in enumerate(THEMES):
            self.screen.set_class(self.theme_ix == i, "theme-" + t["key"])
        try:
            self.dark = THEMES[self.theme_ix]["dark"]
        except Exception:
            pass

    def on_resize(self, event):
        self.render_wordmark()
        self.repaint_rows()
        self.update_selected()

    def tick_anim(self):
        self.live_ix = (self.live_ix + 1) % len(PULSE)
        self.shimmer = (self.shimmer + 2) % 96
        if self.type_n < 60:
            self.type_n += 4
        self._set("#mast", self.termbar_text())
        self.render_wordmark()
        for i in list(self.flash):
            self.flash[i] -= 1
            if self.flash[i] <= 0:
                self.flash.pop(i)
        if self.flash:
            self.rebuild_stats()
        if self.testing:
            for name in list(self.testing):
                self.testing[name] = (self.testing[name] + 1) % len(SPIN)
            self.repaint_rows()

    def _set(self, sel, renderable):
        try:
            self.query_one(sel, Static).update(renderable)
        except Exception:
            pass

    # ---------- data ----------
    def poll(self):
        st = self.api("GET", "/api/state")
        if "rows" not in st:
            self.online = False
            self._set("#mast", self.termbar_text())
            return
        self.online = True
        self.rows = st.get("rows", []) or []
        self.nmodels = st.get("models", 0)
        self.stats = st.get("stats", {}) or {}
        self.probe = st.get("probe", self.probe) or self.probe
        self.server_ver = st.get("version", "")
        # the mast carries live/probing/offline + version: with motion OFF the
        # anim timer is paused, so poll() itself must refresh it (found by the
        # v1.2.0 headless drill: frozen pre-boot render when motion persisted off)
        self._set("#mast", self.termbar_text())
        self.sel = min(self.sel, max(0, len(self.rows) - 1))
        self.marked &= {r.get("name") for r in self.rows}
        self.read_new_events()
        self.render_wordmark()
        self.rebuild_stats()
        self.ensure_slots(len(self.rows))
        for i in range(len(self.row_statics)):
            self.row_statics[f"slot{i}"].display = i < len(self.rows)
        self.repaint_rows()
        self.update_selected()
        self.update_events()

    def read_new_events(self):
        """Offset-tracking tail of the log: every poll reads only new bytes;
        rotation/truncate reseeds. Cache bounded to 300 lines."""
        try:
            size = os.path.getsize(LOG_PATH)
        except OSError:
            return
        if self.log_off is None:
            self.log_off = max(0, size - 16384)
        if size < self.log_off:
            self.log_off = 0
            self.events = []
        try:
            with open(LOG_PATH, "rb") as f:
                f.seek(self.log_off)
                data = f.read()
        except OSError:
            return
        self.log_off += len(data)
        lines = data.decode("utf-8", "replace").splitlines()
        if self.log_off > len(data) and lines:
            lines = lines[1:]  # partial head line from a mid-line seek
        for ln in lines:
            ev = parse_event_line(ln)
            if ev:
                self.events.append((ev[0], ev[1]))
        if len(self.events) > 300:
            del self.events[:len(self.events) - 300]

    def ensure_slots(self, n):
        """Grow-only row slots (no remove -> no mount/remove race)."""
        try:
            table = self.query_one("#table", Vertical)
        except Exception:
            return
        while len(self.row_statics) < n:
            i = len(self.row_statics)
            w = Static(Text(""), classes="row", id=f"slot{i}")
            self.row_statics[f"slot{i}"] = w
            table.mount(w)

    # ---------- column plan ----------
    def frame_w(self):
        try:
            return self.query_one("#frame").container_width
        except Exception:
            return 100

    def col_plan(self):
        return plan_cols(max(40, self.frame_w()))

    # ---------- rows ----------
    def row_line(self, r, selected):
        p = self.pal()
        s = r.get("state", {}) or {}
        name = r.get("name", "?")
        cols, w = self.col_plan()
        line = Text()
        mark = "✓" if name in self.marked else " "
        line.append(f"{mark}{'▶' if selected else ' '} ")
        dis = bool(r.get("disabled"))
        base_style = "dim" if dis else None
        if name in self.testing:
            line.append(f"{clip(name, w['ROW']):<{w['ROW']}} ",
                        style=base_style or p["strong"])
            if "PROV" in cols:
                line.append(f"{clip(prov_short(r.get('provider') or r.get('oauth_provider') or ''), w['PROV']):<{w['PROV']}} ", style=base_style or p["muted"])
            if "CLASS" in cols:
                line.append(f"{str(r.get('class', '?')).upper():<{w['CLASS']}} ", style=base_style or p["muted"])
            if "MODEL" in cols:
                line.append(f"{clip(r.get('model', '?'), w['MODEL']):<{w['MODEL']}} ", style=base_style or p["muted"])
            line.append(SPIN[self.testing[name] % len(SPIN)] + " testing",
                        style=f"bold {p['accent']}")
            return line
        cs = "off" if dis else classify(s)
        col = p[STATUS_KEY[cs]]
        lat = ("%dms" % int(s.get("ms") or 0)).rjust(w.get("LAT", 8) - 2) \
            if cs not in ("unknown", "off") else " " * (w.get("LAT", 8) - 2) + "—"
        line.append(f"{clip(name, w['ROW']):<{w['ROW']}} ",
                    style=base_style or p["strong"])
        if "PROV" in cols:
            line.append(f"{clip(prov_short(r.get('provider') or r.get('oauth_provider') or ''), w['PROV']):<{w['PROV']}} ", style=base_style or p["muted"])
        if "CLASS" in cols:
            line.append(f"{str(r.get('class', '?')).upper():<{w['CLASS']}} ", style=base_style or p["muted"])
        if "MODEL" in cols:
            line.append(f"{clip(r.get('model', '?'), w['MODEL']):<{w['MODEL']}} ", style=base_style or p["muted"])
        if "CW" in cols:
            line.append(f"{cw_str(r.get('cw')):>{w['CW'] - 1}} ", style=base_style or p["dim"])
        if "TPS" in cols:
            tps = s.get("tps") or 0
            line.append((f"{tps:>{w['TPS'] - 1}.0f} " if tps else
                         f"{'—':>{w['TPS'] - 1}} "), style=base_style or p["dim"])
        if "OK" in cols:
            pct = r.get("ok_pct")
            line.append((f"{pct:>{w['OK'] - 2}}% " if pct is not None else
                         f"{'—':>{w['OK'] - 1}} "),
                        style=base_style or (p["ok"] if (pct or 0) >= 80
                                             else p["warn"] if pct is not None
                                             else p["dim"]))
        line.append(spark(s.get("hist", [])) if "LAT" in cols else "", style="")
        if "LAT" in cols:
            line.append(lat + " ", style=base_style or p["dim"])
        line.append(f"{cs.upper():<{w['STATUS']}}", style=f"bold {col}")
        return line

    def repaint_rows(self):
        for i in range(len(self.row_statics)):
            w = self.row_statics.get(f"slot{i}")
            if w is None or not w.display:
                continue
            if i < len(self.rows):
                w.update(self.row_line(self.rows[i], i == self.sel))
                w.set_class(i == self.sel, "sel")

    # ---------- hero ----------
    def termbar_text(self):
        p = self.pal()
        t = Text(justify="center")
        url = f"http://127.0.0.1:{self.port}/v1"
        if self.motion and self.type_n < len(url):
            shown = url[:self.type_n] or " "
        else:
            shown = url
        t.append(shown, style=p["muted"])
        t.append("   localhost only · no server auth", style=p["dim"])
        if self.online:
            if self.probe.get("active"):
                t.append(f"   {PULSE[self.live_ix]} probing "
                         f"{self.probe.get('done', 0)}/{self.probe.get('total', 0)}",
                         style=f"bold {p['warn']}")
            else:
                t.append(f"   {PULSE[self.live_ix]} live", style=f"bold {p['ok']}")
            if self.server_ver:
                t.append(f"   v{self.server_ver}", style=p["dim"])
        else:
            t.append("   ○ OFFLINE — retrying", style=f"bold {p['bad']}")
        return t

    def compact_mark(self):
        p = self.pal()
        t = Text(justify="center")
        t.append("MinimalRouter", style=f"bold {p['strong']}")
        t.append(".", style=f"bold {p['accent']}")
        return t

    def render_wordmark(self):
        p = self.pal()
        rows = wordmark_rows("minimalrouter")
        split = split_index("minimalrouter", "minimal")
        width = len(rows[0]) if rows else 0
        for i, r in enumerate(rows):
            t = Text(justify="center")
            t.append(r[:split], style=p["muted"])
            t.append(r[split:], style=f"bold {p['strong']}")
            if self.motion and width:
                lo = self.shimmer % width
                hi = min(width, lo + 5)
                t.stylize(f"bold {p['accent']}", lo, hi)
                if lo < 5:  # wrap the wave head around the left edge
                    t.stylize(f"bold {p['accent']}", width - (5 - lo), width)
            self._set(f"#wm{i}", t)

    # ---------- stats ----------
    def tile_values(self):
        s = self.stats
        p = self.pal()
        blocked = s.get("blocked", 0)
        err = s.get("err_pct", 0)
        return [
            (compact(s.get("rows", 0)), "ROWS", p["strong"], s.get("rows", 0)),
            (compact(self.nmodels), "MODELS", p["strong"], self.nmodels),
            (compact(s.get("serving", 0)), "SERVING", p["ok"], s.get("serving", 0)),
            (compact(blocked), "BLOCKED", p["bad"] if blocked else p["dim"], blocked),
            (compact(s.get("req_min", 0)), "REQ/MIN", p["accent"], s.get("req_min", 0)),
            (compact(s.get("tok_s", 0)), "TOK/S ≥", p["accent"], s.get("tok_s", 0)),
            (compact(err), "ERR%", p["bad"] if err else p["dim"], err),
            (uptime_str(s.get("uptime", 0)), "UPTIME", p["muted"], None),
        ]

    def rebuild_stats(self):
        p = self.pal()
        vals = self.tile_values()
        for i, (num, lab, col, raw) in enumerate(vals):
            prev = self.prev_tiles.get(i)
            if raw is not None and prev is not None and prev != raw:
                self.flash[i] = 3
            self.prev_tiles[i] = raw
            style = p["strong"] if i in self.flash else col
            if self.dense:
                t = Text(justify="center")
                t.append(num, style=f"bold {style}")
            else:
                t = Text(justify="center")
                for ln in px_rows(num):
                    if len(t) > 0 and t.plain:
                        t.append("\n")
                    t.append(ln, style=f"bold {style}")
            self._set(f"#statnum{i}", t)
            self._set(f"#statlab{i}", Text(lab, justify="center",
                                           style=f"bold {p['dim']}"))

    # ---------- selected ----------
    def update_selected(self):
        p = self.pal()
        r = self.rows[self.sel] if self.rows else None
        s = Text()
        if not r:
            s.append("  (no rows)", style=p["dim"])
            self._set("#selid", s)
            for g in ("tps", "cw"):
                self._set(f"#g{g}", Text(" " * 5 + "\n" * 2, style=p["dim"]))
            return
        st = r.get("state", {}) or {}
        cs = "off" if r.get("disabled") else classify(st)
        col = p[STATUS_KEY[cs]]
        prov = prov_short(r.get("provider") or r.get("oauth_provider") or "")
        # Line 1: row name — the anchor. Line 2: MODEL highlighted hardest
        # (bold accent), provider lighter-but-highlighted (bold dim accent).
        s.append("  " + clip(r.get("name", "?"), 40), style=f"bold {p['strong']}")
        s.append("\n  ")
        s.append(prov, style=f"bold dim {p['accent']}")
        s.append("  ·  ", style=p["dim"])
        s.append(str(r.get("class", "?")).upper(), style=p["muted"])
        s.append("  ·  ", style=p["dim"])
        s.append(clip(r.get("model", "?"), 36), style=f"bold {p['accent']}")
        s.append("\n  " + clip(r.get("base", "?"), 46) + "  ·  key "
                 + str(r.get("key_src", "-")), style=p["dim"])
        s.append("\n  " + cs.upper(), style=f"bold {col}")
        if cs not in ("unknown", "off"):
            s.append(f"  ·  {int(st.get('ms') or 0)}ms", style=p["muted"])
            streak = int(st.get("fail_streak", 0) or 0)
            if streak:
                s.append(f"  ·  {streak} straight fails", style=p["warn"])
        if r.get("ok_pct") is not None:
            s.append(f"  ·  ok {r['ok_pct']}%", style=p["muted"])
        if st.get("error"):
            s.append("  ·  " + clip(st["error"], 46), style=p["dim"])
        self._set("#selid", s)
        # Gauges: live TPS + context window, pixel face, half-wordmark height.
        tps = st.get("tps") or 0
        tn = compact(tps) if tps else "-"
        t = Text(justify="center")
        for j, ln in enumerate(px_rows(tn)):
            if j:
                t.append("\n")
            t.append(ln, style=f"bold {p['accent'] if tps else p['dim']}")
        self._set("#gtps", t)
        self._set("#gtplab", Text("TPS", justify="center", style=f"bold {p['dim']}"))
        cwn = cw_str(r.get("cw"))
        t = Text(justify="center")
        for j, ln in enumerate(px_rows(cwn)):
            if j:
                t.append("\n")
            t.append(ln, style=f"bold {p['ok'] if r.get('cw') else p['dim']}")
        self._set("#gcw", t)
        self._set("#gcwlab", Text("CW", justify="center", style=f"bold {p['dim']}"))
        self.repaint_rows()

    # ---------- events ----------
    def update_events(self):
        p = self.pal()
        e = Text()
        room = 7 if not self.dense else 4
        try:
            room = max(1, self.query_one("#evbody").container_height)
        except Exception:
            pass
        n = self.events
        total = len(n)
        top = max(0, total - room - self.ev_scroll)
        window = n[top:top + room]
        self.ev_scroll = max(0, min(self.ev_scroll, max(0, total - room)))
        if not window:
            e.append("(no traffic yet)", style=p["dim"])
        for hh, body in window:
            kind = ev_kind(body)
            e.append(f"{hh} ", style=p["dim"])
            e.append(("● " if kind == "ok" else "✖ " if kind == "bad" else
                      "· " if kind == "dim" else "▲ "),
                     style=f"bold {p[kind]}")
            e.append(clip(body, 100), style=p["muted"] if kind in ("ok", "dim")
                     else f"bold {p[kind]}" if kind == "bad" else p[kind])
            e.append("\n")
        if self.ev_scroll > 0:
            e.append(f"(scrolled ▲ {self.ev_scroll} — ↓ newer, g newest)",
                     style=p["warn"])
        elif total > room:
            e.append(f"({total} buffered — Tab + ↑ scrolls)", style=p["dim"])
        self._set("#evbody", e)
        self._set("#toast", Text(f"▸ {self.toast}", style=p["accent"]))
        try:
            self.query_one("#evbox").set_class(self.zone == "events", "focused")
            self.query_one("#tablebox").set_class(self.zone == "table", "focused")
        except Exception:
            pass

    # ---------- help ----------
    def help_text(self):
        p = self.pal()
        t = Text()
        t.append("MISSION CONTROL KEYS", style=f"bold {p['accent']}")
        rows = [
            ("↑/↓", "move cursor  (in EVENTS zone: scroll)"),
            ("space", "mark/unmark row"),
            ("t / T", "test row / test marked (all enabled if none)"),
            ("r / R", "refresh row OAuth token / refresh ALL"),
            ("f", "re-fetch every catalog (learns CW)"),
            ("a", "add row  ·  d d: delete marked/cursor (twice = confirm)"),
            ("e", "disable/enable marked (or cursor) row — routing skips it"),
            ("p / P", "theme next/prev  ·  v: density  ·  m: motion"),
            ("Tab", "focus EVENTS panel  ·  g/G: newest/oldest"),
            ("?", "this help  ·  q: quit (stops the server)"),
        ]
        for k, d in rows:
            t.append(f"\n  {k:<9}", style=f"bold {p['strong']}")
            t.append(d, style=p["muted"])
        return t

    def toggle_help(self):
        self.help_open = not self.help_open
        try:
            w = self.query_one("#help", Static)
            w.display = self.help_open
            if self.help_open:
                w.update(self.help_text())
        except Exception:
            pass

    # ---------- compose ----------
    def compose(self) -> ComposeResult:
        with Vertical(id="frame"):
            for i in range(4):
                yield Static(Text(""), id=f"wm{i}", classes="wordmark")
            yield Static(self.compact_mark(), id="wmcompact", classes="wmcompact")
            yield Static(self.termbar_text(), id="mast")

            with Horizontal(classes="box", id="stats"):
                self._stats_box = True
                for i in range(8):
                    with Vertical(classes="stat", id=f"stat{i}"):
                        yield Static(Text(" "), classes="statnum", id=f"statnum{i}")
                        yield Static(Text(" "), classes="statlab", id=f"statlab{i}")

            with Vertical(classes="box", id="tablebox"):
                self._stats_box = True
                head = Text()
                cols, w = self.col_plan()
                head.append(" ✓▶ ")
                for c in cols:
                    if c == "ROW":
                        head.append(f"{'ROW':<{w['ROW']}} ")
                    elif c == "PROV":
                        head.append(f"{'PROV':<{w['PROV']}} ")
                    elif c == "CLASS":
                        head.append(f"{'CLASS':<{w['CLASS']}} ")
                    elif c == "MODEL":
                        head.append(f"{'MODEL':<{w['MODEL']}} ")
                    elif c == "CW":
                        head.append(f"{'CW':>{w['CW'] - 1}} ")
                    elif c == "TPS":
                        head.append(f"{'TPS':>{w['TPS'] - 1}} ")
                    elif c == "OK":
                        head.append(f"{'OK%':>{w['OK'] - 1}} ")
                    elif c == "LAT":
                        head.append(f"{'TREND+LAT':<{w['LAT'] + 5}} ")
                    elif c == "STATUS":
                        head.append(f"{'STATUS':<{w['STATUS']}}")
                yield Static(head, classes="colhead", id="colhead")
                yield Vertical(id="table")

            with Vertical(classes="box", id="selbox"):
                with Horizontal(id="selrow"):
                    yield Static(Text(""), id="selid")
                    with Vertical(classes="gauge", id="gaugt"):
                        yield Static(Text(""), classes="gnum", id="gtps")
                        yield Static(Text("TPS", style="bold"), classes="glab", id="gtplab")
                    with Vertical(classes="gauge", id="gaugc"):
                        yield Static(Text(""), classes="gnum", id="gcw")
                        yield Static(Text("CW", style="bold"), classes="glab", id="gcwlab")

            with Vertical(classes="box", id="evbox"):
                yield Static(Text(""), id="evbody")
            yield Static(Text(""), id="toast")
            yield Static(Text(""), id="help", classes="box")

            foot = Text()
            keys = [("t", "test"), ("T", "all"), ("space", "mark"),
                    ("r", "refresh"), ("R", "all"), ("f", "catalog"),
                    ("e", "off/on"), ("d", "del"), ("a", "add"),
                    ("p", "theme"), ("v", "dense"), ("m", "motion"),
                    ("?", "help"), ("q", "quit")]
            for i, (k, lab) in enumerate(keys):
                if i:
                    foot.append("  ", style="dim")
                foot.append(k, style="bold")
                foot.append(" " + lab)
            yield Static(foot, id="foot")

    # ---------- actions ----------
    def _targets(self):
        """Marked rows if any marks, else the cursor row."""
        if self.marked:
            return [r for r in self.rows if r.get("name") in self.marked]
        return [self.rows[self.sel]] if self.rows else []

    def on_key(self, event):
        k = event.key
        ch = getattr(event, "character", None)
        if self.help_open and k not in ("?", "question_mark"):
            self.toggle_help()
            event.stop()
            return
        if k in ("up",):
            event.stop()
            if self.zone == "events":
                self.ev_scroll += 1
                self.update_events()
            else:
                self.sel = max(0, self.sel - 1)
                self.update_selected()
        elif k == "down":
            event.stop()
            if self.zone == "events":
                self.ev_scroll = max(0, self.ev_scroll - 1)
                self.update_events()
            else:
                self.sel = min(len(self.rows) - 1, self.sel + 1)
                self.update_selected()
        elif k == "tab":
            event.stop()
            self.zone = "events" if self.zone == "table" else "table"
            self.ev_scroll = 0
            self.update_events()
        elif k == "g":
            event.stop()
            self.ev_scroll = 0
            self.update_events()
        elif k == "G":
            event.stop()
            self.ev_scroll = 10000
            self.update_events()
        elif k == "space":
            event.stop()
            if self.rows:
                name = self.rows[self.sel].get("name", "?")
                if name in self.marked:
                    self.marked.discard(name)
                    self.toast = f"unmarked {name}"
                else:
                    self.marked.add(name)
                    self.toast = f"marked {name} ({len(self.marked)} total)"
                self.repaint_rows()
                self.update_events()
        elif k == "t":
            event.stop()
            self._test_row()
        elif k == "T":
            event.stop()
            self._test_many()
        elif k == "r":
            event.stop()
            self._refresh_row()
        elif k == "R":
            event.stop()
            self._refresh_all()
        elif k == "f":
            event.stop()
            self._refetch_catalog()
        elif k == "e":
            event.stop()
            self._toggle_disable()
        elif k == "d":
            event.stop()
            self._delete_arm()
        elif k == "a":
            event.stop()
            self.app.push_screen(AddScreen(self.api, self._add_done))
        elif k == "p":
            event.stop()
            self.theme_ix = (self.theme_ix + 1) % len(THEMES)
            self._apply_theme_classes()
            self.toast = f"theme: {THEMES[self.theme_ix]['key']}"
            save_prefs(THEMES[self.theme_ix]["key"], self.dense, self.motion)
            self.poll()
        elif k == "P":
            event.stop()
            self.theme_ix = (self.theme_ix - 1) % len(THEMES)
            self._apply_theme_classes()
            self.toast = f"theme: {THEMES[self.theme_ix]['key']}"
            save_prefs(THEMES[self.theme_ix]["key"], self.dense, self.motion)
            self.poll()
        elif k == "v":
            event.stop()
            self.dense = not self.dense
            self.screen.set_class(self.dense, "dense")
            self.toast = "density: compact" if self.dense else "density: airy"
            save_prefs(THEMES[self.theme_ix]["key"], self.dense, self.motion)
            self.poll()
        elif k == "m":
            event.stop()
            self.motion = not self.motion
            try:
                if self.motion:
                    self._anim_timer.resume()
                else:
                    self._anim_timer.pause()
            except Exception:
                pass
            self.toast = "motion: on" if self.motion else "motion: off (timers paused)"
            save_prefs(THEMES[self.theme_ix]["key"], self.dense, self.motion)
            self.render_wordmark()
            self._set("#mast", self.termbar_text())
            self.update_events()
        elif k in ("?", "question_mark") or ch == "?":
            event.stop()
            self.toggle_help()
        elif k == "q":
            event.stop()
            self.exit()

    def _add_done(self, msg):
        self.toast = msg
        self.poll()

    def _test_row(self):
        if not self.rows:
            return
        r = self.rows[self.sel]
        name = r.get("name", "?")
        self.testing[name] = 0
        self.toast = f"testing {name} …"
        self.repaint_rows()
        self.update_events()

        def run():
            res = self.api("POST", "/api/test",
                           {"base": r.get("base", ""), "key": r.get("key", ""),
                            "model": r.get("model", "")})
            self.testing.pop(name, None)
            ok = res.get("ok")
            self.toast = (f"{name}: OK {int(res.get('ms') or 0)}ms" if ok
                          else f"{name}: FAIL {str(res.get('error', ''))[:70]}")
            self.call_from_thread(self.poll)
        threading.Thread(target=run, daemon=True).start()

    def _test_many(self):
        targets = self._targets()
        if not targets:
            return
        label = ("marked" if self.marked else "all enabled")
        for r in targets:
            self.testing[r.get("name", "?")] = 0
        self.toast = f"testing {len(targets)} rows ({label}) …"
        self.repaint_rows()
        self.update_events()

        def run():
            with_ok = 0
            for r in targets:
                res = self.api("POST", "/api/test",
                               {"base": r.get("base", ""),
                                "key": r.get("key", ""),
                                "model": r.get("model", "")})
                if res.get("ok"):
                    with_ok += 1
                self.testing.pop(r.get("name", "?"), None)
                self.call_from_thread(self.repaint_rows)
            self.toast = f"tested {len(targets)} ({label}): {with_ok} ok"
            self.call_from_thread(self.poll)
        threading.Thread(target=run, daemon=True).start()

    def _refresh_row(self):
        if not self.rows:
            return
        r = self.rows[self.sel]
        prov = r.get("oauth_provider", "")
        if not prov:
            self.toast = f"{r.get('name', '?')}: no token to refresh — press t"
            self.update_events()
            return
        res = self.api("POST", "/api/refresh", {"provider": prov})
        r0 = (res.get("results") or [{}])[0]
        self.toast = f"{prov}: {r0.get('message', 'done')}"
        self.update_events()

    def _refresh_all(self):
        self.toast = "refreshing ALL OAuth tokens …"
        self.update_events()

        def run():
            res = self.api("POST", "/api/refresh", {})
            results = res.get("results") or []
            ok_n = sum(1 for x in results if x.get("ok"))
            self.toast = (f"refreshed {ok_n}/{len(results)} profiles"
                          if results else "no OAuth profiles to refresh")
            self.call_from_thread(self.poll)
        threading.Thread(target=run, daemon=True).start()

    def _refetch_catalog(self):
        self.toast = "re-fetching catalogs (learning context windows) …"
        self.update_events()

        def run():
            res = self.api("POST", "/api/refetch", {}, timeout=120)
            if res.get("ok"):
                self.toast = (f"catalogs: {res.get('bases')} bases, "
                              f"{res.get('ids')} ids, {res.get('cw')} CW known")
            else:
                self.toast = f"refetch failed: {str(res.get('error', ''))[:60]}"
            self.call_from_thread(self.poll)
        threading.Thread(target=run, daemon=True).start()

    def _toggle_disable(self):
        targets = self._targets()
        if not targets:
            return
        # If every target is disabled -> enable; otherwise disable all.
        want = not all(r.get("disabled") for r in targets)
        names = [r.get("name", "?") for r in targets]

        def run():
            done = 0
            for n in names:
                res = self.api("POST", "/api/disable",
                               {"name": n, "disabled": want})
                if res.get("ok"):
                    done += 1
            self.toast = (f"{'disabled' if want else 'enabled'} {done}/{len(names)}"
                          + (" (profiles parked)" if want else ""))
            self.call_from_thread(self.poll)
        threading.Thread(target=run, daemon=True).start()
        self.toast = f"{'disabling' if want else 'enabling'} {len(names)} rows …"
        self.update_events()

    def _delete_arm(self):
        targets = self._targets()
        if not targets:
            return
        names = tuple(r.get("name", "?") for r in targets)
        now = time.time()
        armed = self.del_arm
        if armed[0] == names and now - armed[1] < 4.0:
            self.del_arm = (None, 0.0)

            def run():
                gone = 0
                for n in names:
                    res = self.api("POST", "/api/remove", {"name": n})
                    if res.get("ok"):
                        gone += 1
                    self.marked.discard(n)
                self.toast = f"deleted {gone}/{len(names)} rows"
                self.call_from_thread(self.poll)
            threading.Thread(target=run, daemon=True).start()
        else:
            self.del_arm = (names, now)
            self.toast = (f"press d AGAIN within 4s to delete {len(names)} "
                          f"row(s): {', '.join(names[:3])}"
                          + (" …" if len(names) > 3 else ""))
            self.update_events()


def spark(hist, width=5):
    """Latency history -> block sparkline (mirrors the engine's pure helper).
    Flat history renders a steady mid line (▄), not a bottom bleed."""
    if not hist:
        return " " * width
    vals = hist[-width:]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return ("▄" * len(vals)).ljust(width)
    span = hi - lo
    return "".join(BLOCKS[int((v - lo) / span * (len(BLOCKS) - 1))] for v in vals).ljust(width)


def run_dashboard(port=8790):
    """Entry point called by the core. Blocks until the user quits; the core then
    shuts the server down. Raises ImportError upstream if textual is absent."""
    MissionControl(port).run()


if __name__ == "__main__":
    run_dashboard()

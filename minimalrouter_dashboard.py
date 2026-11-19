"""MinimalRouter — Mission Control dashboard (optional Textual enhancement).

This module is the OPTIONAL enhanced dashboard. It is imported lazily by
minimalrouter.py only when the user runs the interactive TUI; if `textual` is not
installed the import fails and the core falls back to its stdlib curses board.
The router core itself never depends on this file or on Textual.

Design: the locked Mission Control board (studied from opencode + Hermes's own Ink
TUI) — one rounded outer frame, faint internal rules, full-row selection fill,
block-pixel two-tone wordmark, latency sparklines, semantic status colors, 3 themes.

Robustness: offline-tolerant polling, guarded widget queries, grow-only row slots,
theme/density persistence, clean quit (the core shuts the server down after return).
"""
import json
import os
import re
import threading
import urllib.error
import urllib.request

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

BLOCKS = "▁▂▃▄▅▆▇█"
SPIN = "⣾⣽⣻⢿⡿⣟⣯⣷"
PULSE = "◐◑◒◓"
STATUS_KEY = {"ok": "ok", "gated": "bad", "empty": "warn",
              "error": "bad", "unknown": "dim"}

THEMES = [
    dict(key="midnight", bg="#0a0c10", line="#222835", accent="#4fd1c5",
         strong="#e6e9ef", muted="#9aa2b4", dim="#5c6478", sel="#123330",
         ok="#57d9a3", warn="#e0b34f", bad="#e06c75"),
    dict(key="ocean", bg="#0a1018", line="#1e3350", accent="#5ab0ff",
         strong="#e8f0fa", muted="#8aa0bc", dim="#4a6080", sel="#16304f",
         ok="#4fd6a8", warn="#e8c56a", bad="#f07a7a"),
    dict(key="ember", bg="#100d0a", line="#3a2c1d", accent="#ffb454",
         strong="#f5e8d8", muted="#b09878", dim="#6a5a45", sel="#3a2817",
         ok="#a8d96a", warn="#e8a04a", bad="#f0705e"),
]
THEME_KEYS = [t["key"] for t in THEMES]

MR_HOME = os.path.expanduser("~/.minimalrouter")
LOG_PATH = os.path.join(MR_HOME, "minimalrouter.log")
PREF_PATH = os.path.join(MR_HOME, "dashboard.json")


# ---------------------------------------------------------------- wordmark
# 5x7 pixel font rendered with half-blocks (▀▄█): each terminal row = 2 pixel
# rows, giving smooth SOLID letters. Two-tone split like opencode's open/code.
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


# ---------------------------------------------------------------- helpers
def clip(s, w):
    s = str(s)
    return s if len(s) <= w else s[:w - 1] + "…"


def classify(state):
    st = (state or {}).get("status", "unknown")
    if st == "ok":
        return "ok"
    if st != "fail":
        return "unknown"
    err = str((state or {}).get("error", ""))
    if "403" in err:
        return "gated"
    if "402" in err:
        return "empty"
    return "error"


def spark(hist):
    """Latency history -> 5-wide block sparkline; flat history = steady mid line."""
    if not hist:
        return " ─    "
    vals = hist[-5:]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        sp = "▄" * len(vals)
    else:
        sp = "".join(BLOCKS[int((v - lo) / (hi - lo) * 7)] for v in vals)
    return sp.ljust(5)


def load_prefs():
    try:
        with open(PREF_PATH) as f:
            d = json.load(f)
        theme = d.get("theme", "midnight")
        if theme not in THEME_KEYS:
            theme = "midnight"
        return {"theme": theme, "dense": bool(d.get("dense", False))}
    except Exception:
        return {"theme": "midnight", "dense": False}


def save_prefs(theme_key, dense):
    try:
        os.makedirs(MR_HOME, exist_ok=True)
        tmp = PREF_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"theme": theme_key, "dense": bool(dense)}, f)
        os.replace(tmp, PREF_PATH)
    except Exception:
        pass


def tail_events(n=4):
    try:
        with open(LOG_PATH, "r", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[::-1]:
        m = re.match(r"\[(\d{4}-\d\d-\d\d )?(\d\d:\d\d:\d\d)\]", ln)
        if not m:
            continue
        kind = "ok" if " OK " in ln else ("bad" if " FAIL " in ln else None)
        if kind is None:
            continue
        body = ln.split("] ", 1)[-1]
        out.append((m.group(2), kind, body))
        if len(out) >= n:
            break
    return out[::-1]


class MissionControl(App):
    TITLE = "MinimalRouter — Mission Control"
    CSS = """
    Screen { background: #0a0c10; padding: 1 2; }
    #frame { border: round #222835; padding: 1 2; height: 100%; }
    #tablezone { height: 1fr; }
    #table { height: 1fr; overflow-y: auto; }
    Screen.dense #frame { padding: 0 1; }
    .wordmark { height: 1; }
    .wmcompact { display: none; height: 1; }
    Screen.dense .wordmark { display: none; }
    Screen.dense .wmcompact { display: block; }
    #mast { height: 1; margin-bottom: 1; }
    .zone { padding-top: 1; padding-bottom: 1; border-top: solid #222835; }
    Screen.dense .zone { padding-top: 0; padding-bottom: 0; }
    #stats { height: 4; padding-top: 0; padding-bottom: 0; }
    Screen.dense #stats { height: 3; }
    .stat { width: 1fr; padding-left: 2; }
    * { scrollbar-size: 1 1; scrollbar-color: #222835; scrollbar-background: #0a0c10; }
    .theme-ocean * { scrollbar-color: #1e3350; scrollbar-background: #0a1018; }
    .theme-ember * { scrollbar-color: #3a2c1d; scrollbar-background: #100d0a; }
    .statnum { text-style: bold; }
    .statlabel { margin-top: 1; }
    Screen.dense .statlabel { margin-top: 0; }
    .colhead { margin-bottom: 1; }
    .row { height: 1; padding: 0 1; }
    .row.sel { background: #123330; }
    #selected { height: auto; }
    #events { height: auto; }
    #toast { height: 1; margin-top: 1; }
    #foot { height: 1; margin-top: 1; }

    Screen.theme-ocean { background: #0a1018; }
    .theme-ocean #frame { border: round #1e3350; }
    .theme-ocean .zone { border-top: solid #1e3350; }
    .theme-ocean .row.sel { background: #16304f; }

    Screen.theme-ember { background: #100d0a; }
    .theme-ember #frame { border: round #3a2c1d; }
    .theme-ember .zone { border-top: solid #3a2c1d; }
    .theme-ember .row.sel { background: #3a2817; }
    """

    def __init__(self, port=8790):
        super().__init__()
        self.port = port
        prefs = load_prefs()
        self.theme_ix = THEME_KEYS.index(prefs["theme"])
        self.dense = prefs["dense"]
        self.sel = 0
        self.rows = []
        self.nmodels = 0
        self.online = True
        self.row_statics = {}
        self.testing = {}
        self.toast = "ready"
        self.live_ix = 0

    def pal(self):
        return THEMES[self.theme_ix]

    def api(self, method, path, body=None, timeout=30):
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
        self.set_interval(2.0, self.poll)
        self.set_interval(0.15, self.tick_anim)
        self.poll()

    def _apply_theme_classes(self):
        self.screen.set_class(self.theme_ix == 1, "theme-ocean")
        self.screen.set_class(self.theme_ix == 2, "theme-ember")

    def tick_anim(self):
        self.live_ix = (self.live_ix + 1) % len(PULSE)
        self._set("#mast", self.termbar_text())
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
        self.sel = min(self.sel, max(0, len(self.rows) - 1))
        self.render_wordmark()
        self.rebuild_stats()
        self.ensure_slots(len(self.rows))
        for i in range(len(self.row_statics)):
            self.row_statics[f"slot{i}"].display = i < len(self.rows)
        self.repaint_rows()
        self.update_selected()
        self.update_events()

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

    def row_line(self, r, selected):
        p = self.pal()
        s = r.get("state", {}) or {}
        name = r.get("name", "?")
        line = Text()
        line.append("▶ " if selected else "  ")
        if name in self.testing:
            line.append(f"{clip(name, 22):<24}", style=p["strong"])
            line.append(f"{str(r.get('class', '?')).upper():<9}", style=p["muted"])
            line.append(f"{clip(r.get('model', '?'), 30):<32}", style=p["muted"])
            line.append(SPIN[self.testing[name] % len(SPIN)] + " testing",
                        style=f"bold {p['accent']}")
            line.append(" " * 15)
            line.append(f"{'······':<8}", style=p["dim"])
            return line
        cs = classify(s)
        col = p[STATUS_KEY[cs]]
        lat = f"{int(s.get('ms') or 0):>6}ms" if cs != "unknown" else "     — "
        line.append(f"{clip(name, 22):<24}", style=p["strong"])
        line.append(f"{str(r.get('class', '?')).upper():<9}", style=p["muted"])
        line.append(f"{clip(r.get('model', '?'), 30):<32}", style=p["muted"])
        line.append(spark(s.get("hist", [])),
                    style=f"bold {p['accent'] if cs == 'ok' else col}")
        line.append("  ")
        line.append(lat, style=p["dim"])
        line.append("  ")
        line.append(f"{cs.upper():<8}", style=f"bold {col}")
        return line

    def repaint_rows(self):
        for i in range(len(self.row_statics)):
            w = self.row_statics.get(f"slot{i}")
            if w is None or not w.display:
                continue
            if i < len(self.rows):
                w.update(self.row_line(self.rows[i], i == self.sel))
                w.set_class(i == self.sel, "sel")

    # ---------- compose ----------
    def termbar_text(self):
        p = self.pal()
        t = Text()
        t.append(f"  http://127.0.0.1:{self.port}/v1", style=p["muted"])
        t.append("   localhost only · no server auth", style=p["dim"])
        if self.online:
            t.append(f"   {PULSE[self.live_ix]} live", style=f"bold {p['ok']}")
        else:
            t.append("   ○ OFFLINE — retrying", style=f"bold {p['bad']}")
        return t

    def compact_mark(self):
        p = self.pal()
        t = Text()
        t.append("  MinimalRouter", style=f"bold {p['strong']}")
        t.append(".", style=f"bold {p['accent']}")
        return t

    def render_wordmark(self):
        p = self.pal()
        rows = wordmark_rows("minimalrouter")
        split = split_index("minimalrouter", "minimal")
        for i, r in enumerate(rows):
            t = Text()
            t.append(r[:split], style=p["muted"])
            t.append(r[split:], style=f"bold {p['strong']}")
            self._set(f"#wm{i}", t)

    def compose(self) -> ComposeResult:
        p = self.pal()
        with Vertical(id="frame"):
            for i in range(4):
                yield Static(Text(""), id=f"wm{i}", classes="wordmark")
            yield Static(self.compact_mark(), id="wmcompact", classes="wmcompact")
            yield Static(self.termbar_text(), id="mast")

            with Horizontal(classes="zone", id="stats"):
                for i, lab in enumerate(["ROWS", "MODELS", "SERVING", "BLOCKED"]):
                    with Vertical(classes="stat", id=f"stat{i}"):
                        yield Static(Text("0", style=f"bold {p['strong']}"), id=f"statnum{i}")
                        yield Static(Text(f" {lab}", style=f"bold {p['dim']}"), id=f"statlab{i}")

            with Vertical(classes="zone", id="tablezone"):
                head = Text()
                head.append(f"  {'ROW':<24}{'CLASS':<9}{'MODEL':<32}{'TREND':<7}   {'LAT':>8}   {'STATUS':<8}",
                            style=f"bold {p['dim']}")
                yield Static(head, classes="colhead")
                yield Vertical(id="table")

            with Vertical(classes="zone", id="selected"):
                yield Static(Text(""), id="selbody")
            with Vertical(classes="zone", id="events"):
                yield Static(Text(""), id="evbody")
            yield Static(Text(""), id="toast")

            foot = Text()
            foot.append("  t", style=f"bold {p['accent']}").append(" test   ", style=p["muted"])
            foot.append("r", style=f"bold {p['accent']}").append(" refresh   ", style=p["muted"])
            foot.append("↑↓", style=f"bold {p['accent']}").append(" select   ", style=p["muted"])
            foot.append("p", style=f"bold {p['accent']}").append(" theme   ", style=p["muted"])
            foot.append("v", style=f"bold {p['accent']}").append(" density   ", style=p["muted"])
            foot.append("q", style=f"bold {p['accent']}").append(" quit", style=p["muted"])
            yield Static(foot, id="foot")

    def rebuild_stats(self):
        p = self.pal()
        cls = [classify(r.get("state", {})) for r in self.rows]
        blocked = cls.count("gated") + cls.count("empty")
        vals = [(len(self.rows), "strong"), (self.nmodels, "strong"),
                (cls.count("ok"), "ok"), (blocked, "bad" if blocked else "dim")]
        for i, (num, key) in enumerate(vals):
            self._set(f"#statnum{i}", Text(f"{num}", style=f"bold {p[key]}"))

    def update_selected(self):
        p = self.pal()
        r = self.rows[self.sel] if self.rows else None
        s = Text()
        s.append("  SELECTED", style=f"bold {p['accent']}")
        if r:
            st = r.get("state", {}) or {}
            cs = classify(st)
            s.append(f"\n  {r.get('name', '?')}", style=f"bold {p['strong']}")
            s.append(f"  ·  {str(r.get('class', '?')).upper()}  ·  {r.get('model', '?')}", style=p["muted"])
            s.append(f"\n  {r.get('base', '?')}", style=p["dim"])
            s.append(f"  ·  key {r.get('key_src', '-')}", style=p["dim"])
            s.append(f"\n  {cs.upper()}", style=f"bold {p[STATUS_KEY[cs]]}")
            s.append((f"  ·  {int(st.get('ms') or 0)}ms" if cs != "unknown" else "  ·  —"), style=p["muted"])
            s.append("  ·  " + (st.get("error") or "no error"), style=p["dim"])
        self._set("#selbody", s)
        self.repaint_rows()

    def update_events(self):
        p = self.pal()
        e = Text()
        e.append("  EVENTS", style=f"bold {p['accent']}")
        evs = tail_events(4)
        if not evs:
            e.append("\n  (no traffic yet)", style=p["dim"])
        for hh, kind, body in evs:
            e.append(f"\n  {hh} ", style=p["dim"])
            e.append(("OK   " if kind == "ok" else "FAIL "), style=p["ok" if kind == "ok" else "bad"])
            body = re.sub(r"^(OK|FAIL)\s+", "", body)
            e.append(body[:90], style=p["muted"])
        self._set("#evbody", e)
        self._set("#toast", Text(f"  ▸ {self.toast}", style=p["accent"]))

    # ---------- actions ----------
    def on_key(self, event):
        if event.key == "up":
            self.sel = max(0, self.sel - 1); self.update_selected(); event.stop()
        elif event.key == "down":
            self.sel = min(len(self.rows) - 1, self.sel + 1); self.update_selected(); event.stop()
        elif event.key == "t":
            self._test_row(); event.stop()
        elif event.key == "r":
            self._refresh_row(); event.stop()
        elif event.key == "p":
            self.theme_ix = (self.theme_ix + 1) % len(THEMES)
            self._apply_theme_classes()
            self.toast = f"theme: {THEMES[self.theme_ix]['key']}"
            save_prefs(THEMES[self.theme_ix]["key"], self.dense)
            self.poll(); event.stop()
        elif event.key == "v":
            self.dense = not self.dense
            self.screen.set_class(self.dense, "dense")
            self.toast = "density: compact" if self.dense else "density: airy"
            save_prefs(THEMES[self.theme_ix]["key"], self.dense)
            event.stop()
        elif event.key == "q":
            event.stop()
            self.exit()

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

    def _refresh_row(self):
        if not self.rows:
            return
        r = self.rows[self.sel]
        prov = r.get("oauth_provider", "")
        if not prov:
            self.toast = f"{r.get('name', '?')}: no token to refresh — press t"
            self.update_events(); return
        res = self.api("POST", "/api/refresh", {"provider": prov})
        r0 = (res.get("results") or [{}])[0]
        self.toast = f"{prov}: {r0.get('message', 'done')}"
        self.update_events()


def run_dashboard(port=8790):
    """Entry point called by the core. Blocks until the user quits; the core then
    shuts the server down. Raises ImportError upstream if textual is absent."""
    MissionControl(port).run()


if __name__ == "__main__":
    run_dashboard()

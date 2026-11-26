#!/bin/sh
# MinimalRouter installer. The router engine is a single stdlib file; the installer
# ALSO sets up the Mission Control dashboard (Textual) in a dedicated venv so the
# beautiful board works out of the box — one install, no extra steps, the way
# opencode / Hermes / commandcode ship their TUIs. If the venv or Textual can't be
# set up (offline / no pip), it falls back to the built-in curses board.
# Pass --curses to skip the dashboard venv entirely (zero-dep mode).
# Usage:  curl -fsSL https://raw.githubusercontent.com/muntasirrmahdi/minimalrouter/main/install.sh | sh
# Or from a checkout:  sh install.sh [--curses] [--systemd]

set -eu

REPO="muntasirrmahdi/minimalrouter"
BRANCH="main"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"
DEST="$HOME/.minimalrouter"
BIN="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }

WITH_TEXTUAL=1
WITH_SYSTEMD=0
for arg in "$@"; do
    case "$arg" in
        --curses) WITH_TEXTUAL=0 ;;
        --textual) WITH_TEXTUAL=1 ;;
        --systemd) WITH_SYSTEMD=1 ;;
        *) say "note: ignoring unknown option '$arg'" ;;
    esac
done

# ---- requirements ----------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    say "error: python3 not found. Install Python 3.8 or newer and re-run."
    exit 1
fi

PY_OK=$(python3 - <<'PY'
import sys
print("1" if sys.version_info >= (3, 8) else "0")
PY
)
if [ "$PY_OK" != "1" ]; then
    say "error: Python 3.8 or newer is required (found $(python3 -V 2>&1))."
    exit 1
fi

mkdir -p "$DEST" "$BIN"

# ---- fetch the files (local checkout, or from the repo) --------------------
fetch() {
    # $1 = filename
    if [ -f "./$1" ]; then
        cp "./$1" "$DEST/$1"
    else
        curl -fsSL "$RAW/$1" -o "$DEST/$1"
    fi
}

fetch minimalrouter.py
# Optional enhanced dashboard (Textual). The core falls back to curses if the
# module or its dependency is absent, so this file is safe to ship always.
fetch minimalrouter_dashboard.py
fetch rows.example.json
fetch oauth-providers.example.json
# The zcode translator loads this identity asset; without it a fresh install
# sends an empty identity and the vendor answers 405 code 3012.
mkdir -p "$DEST/bridges"
fetch bridges/zcode-identity.json
# Learned entries must survive an upgrade: seed the registry only once.
if [ ! -f "$DEST/provider-registry.json" ]; then fetch provider-registry.json; fi
chmod 600 "$DEST/minimalrouter.py" 2>/dev/null || true
# Existing users get the new OAuth-capable code, but their personal rows stay untouched.
if [ -f "$DEST/oauth.json" ]; then chmod 600 "$DEST/oauth.json"; fi

if [ ! -f "$DEST/rows.json" ]; then
    cp "$DEST/rows.example.json" "$DEST/rows.json"
    say "seeded $DEST/rows.json from the example rows"
fi
chmod 600 "$DEST/rows.json"
if [ -f "$DEST/oauth.json" ]; then chmod 600 "$DEST/oauth.json"; fi

# ---- dashboard environment -------------------------------------------------
# Bundle the Mission Control board the way opencode/Hermes ship their TUIs: the
# installer brings the dependency, so ONE install gives the beautiful board. A
# dedicated venv makes it independent of whichever python3 happens to be on PATH.
DASH_PY=""
if [ "$WITH_TEXTUAL" = "1" ]; then
    VENV="$DEST/.venv"
    if python3 -m venv "$VENV" >/dev/null 2>&1 || uv venv "$VENV" >/dev/null 2>&1; then
        if "$VENV/bin/python3" -m pip install textual >/dev/null 2>&1 \
           || uv pip install --python "$VENV/bin/python3" textual >/dev/null 2>&1; then
            DASH_PY="$VENV/bin/python3"
        fi
    fi
    if [ -n "$DASH_PY" ]; then
        say "dashboard: Mission Control (Textual) ready"
    else
        say "dashboard: Textual setup unavailable — using the built-in curses board"
    fi
fi

# ---- launcher --------------------------------------------------------------
# Pin the launcher to the dashboard venv when present, so the board renders the
# same regardless of the user's PATH python3.
if [ -n "$DASH_PY" ]; then
    printf '#!/bin/sh\nexec "%s" "%s/minimalrouter.py" "$@"\n' "$DASH_PY" "$DEST" > "$BIN/minimalrouter"
else
    printf '#!/bin/sh\nexec python3 "%s/minimalrouter.py" "$@"\n' "$DEST" > "$BIN/minimalrouter"
fi
chmod +x "$BIN/minimalrouter"

# ---- optional systemd user unit -------------------------------------------
# A user unit fires at login, not at boot; that is the honest target for a
# personal localhost router. The installer writes and reloads the unit but
# NEVER enables it: starting an always-on service is the user's decision.
if [ "$WITH_SYSTEMD" = "1" ]; then
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/minimalrouter.service" <<UNIT
[Unit]
Description=MinimalRouter — localhost OpenAI-compatible pool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$BIN/minimalrouter serve
Environment=MINIMALROUTER_PORT=8790
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        say "systemd: user unit written to $UNIT_DIR/minimalrouter.service"
        say "systemd: enable it with: systemctl --user enable --now minimalrouter"
    else
        say "systemd: unit written to $UNIT_DIR/minimalrouter.service (systemctl absent)"
    fi
fi

# ---- report ----------------------------------------------------------------
say ""
say "MinimalRouter installed"
say "  program : $DEST/minimalrouter.py"
say "  rows    : $DEST/rows.json"
say "  command : $BIN/minimalrouter"
say ""
case ":$PATH:" in
    *":$BIN:"*) ;;
    *)
        say "note: $BIN is not on your PATH. Add it with:"
        say "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && exec \$SHELL"
        say ""
        ;;
esac
say "next steps"
say "  1. export the keys your rows name, for example: export GROQ_API_KEY=..."
say "  2. run: minimalrouter"
say "  3. press a to add a provider, t to test one, q to quit"
say ""
say "server only: minimalrouter serve"
say ""
if [ -n "$DASH_PY" ]; then
    say "dashboard: Mission Control board (Textual) — p themes, v density"
else
    say "dashboard: built-in curses board (zero deps)"
fi

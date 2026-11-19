#!/bin/sh
# MinimalRouter installer: stdlib core, no required dependencies.
# The enhanced Mission Control dashboard is optional (pip install textual);
# without it the built-in curses dashboard is used. Pass --textual to install it.
# Usage:  curl -fsSL https://raw.githubusercontent.com/muntasirrmahdi/minimalrouter/main/install.sh | sh
# Or from a checkout:  sh install.sh [--textual]

set -eu

REPO="muntasirrmahdi/minimalrouter"
BRANCH="main"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"
DEST="$HOME/.minimalrouter"
BIN="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }

WITH_TEXTUAL=0
for arg in "$@"; do
    case "$arg" in
        --textual) WITH_TEXTUAL=1 ;;
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

# ---- launcher --------------------------------------------------------------
printf '#!/bin/sh\nexec python3 "%s/minimalrouter.py" "$@"\n' "$DEST" > "$BIN/minimalrouter"
chmod +x "$BIN/minimalrouter"

# ---- optional enhanced dashboard ------------------------------------------
if [ "$WITH_TEXTUAL" = "1" ]; then
    say "installing Textual for the enhanced Mission Control dashboard..."
    # NOTE: no --user. --user installs into the user site, which virtualenvs
    # exclude; we must install into the SAME python3 that will run the launcher.
    if python3 -m pip install textual >/dev/null 2>&1; then
        say "  Textual installed — run 'minimalrouter' for the enhanced board."
    elif command -v uv >/dev/null 2>&1 && uv pip install --python python3 textual >/dev/null 2>&1; then
        say "  Textual installed (via uv) — run 'minimalrouter' for the enhanced board."
    else
        say "  note: could not install textual; the curses dashboard will be used."
        say "  try manually:  python3 -m pip install textual"
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
say "enhanced dashboard (optional): pip install textual   (or: sh install.sh --textual)"
say "  without it the built-in curses dashboard is used — the core needs no deps"

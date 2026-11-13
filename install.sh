#!/bin/sh
# MinimalRouter installer: one file, no dependencies.
# Usage:  curl -fsSL https://raw.githubusercontent.com/muntasirrmahdi/minimalrouter/main/install.sh | sh
# Or from a checkout:  sh install.sh

set -eu

REPO="muntasirrmahdi/minimalrouter"
BRANCH="main"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"
DEST="$HOME/.minimalrouter"
BIN="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }

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
fetch rows.example.json
chmod 600 "$DEST/minimalrouter.py" 2>/dev/null || true
# Existing users get the new OAuth-capable code, but their personal rows stay untouched.
if [ -f "$DEST/oauth.json" ]; then chmod 600 "$DEST/oauth.json"; fi

if [ ! -f "$DEST/rows.json" ]; then
    cp "$DEST/rows.example.json" "$DEST/rows.json"
    say "seeded $DEST/rows.json from the example rows"
fi
chmod 600 "$DEST/rows.json"

# ---- launcher --------------------------------------------------------------
printf '#!/bin/sh\nexec python3 "%s/minimalrouter.py" "$@"\n' "$DEST" > "$BIN/minimalrouter"
chmod +x "$BIN/minimalrouter"

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

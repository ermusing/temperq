#!/bin/sh
# Update temperq's code on a Pi where install.sh has already run, and restart the service.
# Fast: copies the package into the venv without pip, so nothing is downloaded or built.
# Dependencies, config files and the systemd unit are left alone; if pyproject.toml's
# dependencies changed, this refuses and you need install.sh instead.
# Run from an up-to-date checkout of this repo:  sudo deploy/update.sh
set -eu
umask 022

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/temperq
PY="$APP_DIR/.venv/bin/python"

if [ ! -x "$PY" ]; then
    echo "No venv at $APP_DIR/.venv; run deploy/install.sh first." >&2
    exit 1
fi

# Refuse if the dependencies differ from what's installed: new code may need them.
"$PY" - "$REPO_DIR/pyproject.toml" <<'EOF'
import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, requires


def normalize(req):
    req = req.replace(" ", "")
    name = re.match(r"[A-Za-z0-9._-]+", req).group()
    specs = tuple(sorted(req[len(name):].split(",")))
    return re.sub(r"[-_.]+", "-", name).lower(), specs


with open(sys.argv[1], "rb") as f:
    wanted = {normalize(r) for r in tomllib.load(f)["project"]["dependencies"]}
try:
    installed = {normalize(r) for r in requires("temperq") or [] if "extra==" not in r.replace(" ", "")}
except PackageNotFoundError:
    sys.exit("temperq isn't installed in the venv; run deploy/install.sh first.")
if wanted != installed:
    for name, specs in sorted(wanted ^ installed):
        side = "pyproject.toml" if (name, specs) in wanted else "installed"
        print(f"  {side}: {name}{','.join(specs)}", file=sys.stderr)
    sys.exit("Dependencies changed; run deploy/install.sh instead.")
EOF

PKG_DIR="$("$PY" -c 'import os, temperq; print(os.path.dirname(temperq.__file__))')"
case "$PKG_DIR" in
    "$APP_DIR"/.venv/*) ;;
    *) echo "temperq is imported from $PKG_DIR, not the venv's site-packages; refusing." >&2; exit 1 ;;
esac

# Stage and byte-compile the new code first, so a broken copy never replaces a working one.
STAGE="$PKG_DIR.new"
rm -rf "$STAGE" "$PKG_DIR.old"
cp -R "$REPO_DIR/src/temperq" "$STAGE"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +
if ! "$PY" -m compileall -q "$STAGE" >/dev/null; then
    rm -rf "$STAGE"
    echo "The new code doesn't compile; nothing was changed." >&2
    exit 1
fi
mv "$PKG_DIR" "$PKG_DIR.old"
mv "$STAGE" "$PKG_DIR"
rm -rf "$PKG_DIR.old"
echo "Updated $PKG_DIR from $REPO_DIR ($(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" describe --always --dirty 2>/dev/null || echo 'unknown version'))."

# Restart only if it's running; a stopped service stays stopped.
if systemctl is-active --quiet temperq; then
    systemctl restart temperq
    echo "Restarted temperq. Follow it with:  journalctl -u temperq -f"
else
    echo "temperq isn't running; start it with:  sudo systemctl start temperq"
fi

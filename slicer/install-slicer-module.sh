#!/bin/sh
# Register the SpaceMouseNav module in 3D Slicer by adding it to the
# "Additional module paths" setting. Close Slicer first, then start it again.
# Usage: slicer/install-slicer-module.sh [path/to/Slicer]
set -e
SLICER="${1:-$(command -v Slicer || true)}"
if [ -z "$SLICER" ]; then
    echo "Slicer not found on PATH; pass its path: $0 /path/to/Slicer" >&2
    exit 1
fi
MODULE_DIR="$(cd "$(dirname "$0")/SpaceMouseNav" && pwd)"
SCRIPT="$(mktemp --suffix=.py)"
trap 'rm -f "$SCRIPT"' EXIT
cat > "$SCRIPT" <<PY
# Slicer 5 reads module paths from the revision-specific settings file.
s = slicer.app.revisionUserSettings()
paths = s.value("Modules/AdditionalPaths") or []
if isinstance(paths, str):
    paths = [paths]
paths = [p for p in paths if p]
if "$MODULE_DIR" not in paths:
    paths.append("$MODULE_DIR")
    s.setValue("Modules/AdditionalPaths", paths)
    s.sync()
# Drop the entry older versions of this script left in the wrong file.
u = slicer.app.userSettings()
if u.contains("Modules/AdditionalPaths"):
    u.remove("Modules/AdditionalPaths")
    u.sync()
print("SpaceMouseNav registered in Slicer:", s.fileName())
PY
"$SLICER" --no-splash --no-main-window --disable-cli-modules \
    --python-script "$SCRIPT" --exit-after-startup 2>/dev/null | grep SpaceMouseNav
echo "Restart Slicer; settings are in Modules > Utilities > SpaceMouse Navigation."

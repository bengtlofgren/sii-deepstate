#!/usr/bin/env bash
# Build a standalone macOS .app bundle for the DeepState Overview GUI.
set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    if [[ -f ../.venv/bin/activate ]]; then
        # shellcheck disable=SC1091
        source ../.venv/bin/activate
    else
        echo "No active venv and ../.venv not found. Create one with:" >&2
        echo "  python -m venv .venv && source .venv/bin/activate" >&2
        echo "  pip install -r requirements.txt" >&2
        exit 1
    fi
fi

pip install -q -r ../requirements.txt
pip install -q -r ../requirements-build.txt

rm -rf build dist "DeepState Overview.spec"

pyinstaller \
    --windowed \
    --noconfirm \
    --name "DeepState Overview" \
    --collect-all playwright \
    --collect-all geopy \
    --collect-all certifi \
    --add-data "../deepstate_screenshot.py:." \
    main.py

echo
echo "Build complete:"
echo "  $(pwd)/dist/DeepState Overview.app"
echo
echo "Distribute by zipping or dragging that .app to another Mac."

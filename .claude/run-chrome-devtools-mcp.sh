#!/usr/bin/env bash
# Launches the chrome-devtools MCP server via npx.
# Resolves `node` without hardcoding a specific nvm-managed version:
#   1. If `node` is already on PATH, use it.
#   2. Otherwise source nvm (if available) and use the default Node.
set -e

if ! command -v node >/dev/null 2>&1; then
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    if [ -s "$NVM_DIR/nvm.sh" ]; then
        # shellcheck disable=SC1091
        . "$NVM_DIR/nvm.sh"
    fi
fi

if ! command -v npx >/dev/null 2>&1; then
    echo "error: npx not found on PATH — install Node.js (>=18) or nvm" >&2
    exit 1
fi

exec npx -y chrome-devtools-mcp@latest "$@"

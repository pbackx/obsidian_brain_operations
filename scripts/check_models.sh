#!/usr/bin/env bash
# ==============================================================================
# check_models.sh - Monitor Antigravity CLI (agy) Version & Model Entitlements
# ==============================================================================
# Queries the live backend model allowlist via `agy models`, compares it against
# a local baseline snapshot (logs/models_snapshot.txt), and reports any newly
# unlocked or removed models for the active Google account tier.
#
# Usage:
#   ./scripts/check_models.sh [--notify-vault]
# ==============================================================================

set -euo pipefail

if [[ -d "$HOME/.local/bin" && ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    export PATH="$HOME/.local/bin:$PATH"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$BASE_DIR/config/settings.env"

if [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
fi

VAULT_LOCAL="${VAULT_LOCAL:-$HOME/vault_sync/ObsidianVault}"
LOG_DIR="$BASE_DIR/logs"
SNAPSHOT_FILE="$LOG_DIR/models_snapshot.txt"
NOTIFY_VAULT=false

if [[ "${1:-}" == "--notify-vault" ]]; then
    NOTIFY_VAULT=true
fi

mkdir -p "$LOG_DIR"

if ! command -v agy >/dev/null 2>&1; then
    echo "ERROR: 'agy' CLI binary not found in PATH." >&2
    exit 1
fi

AGY_VERSION="$(agy --version 2>/dev/null | head -n 1 | tr -d '\r')"
CURRENT_MODELS="$(agy models 2>/dev/null | tr -d '\r' | sed '/^[[:space:]]*$/d' | sort)"

if [[ -z "$CURRENT_MODELS" ]]; then
    echo "WARNING: 'agy models' returned an empty list (check authentication or network)." >&2
    exit 1
fi

echo "============================================================"
echo " Antigravity CLI Model Entitlement Check ($(date '+%Y-%m-%d %H:%M:%S'))"
echo " CLI Version: v${AGY_VERSION}"
echo "============================================================"
echo "$CURRENT_MODELS"
echo "------------------------------------------------------------"

if [[ ! -f "$SNAPSHOT_FILE" ]]; then
    echo "$CURRENT_MODELS" > "$SNAPSHOT_FILE"
    echo "Baseline snapshot initialized at: $SNAPSHOT_FILE"
    exit 0
fi

ADDED_MODELS="$(comm -13 "$SNAPSHOT_FILE" <(echo "$CURRENT_MODELS"))"
REMOVED_MODELS="$(comm -23 "$SNAPSHOT_FILE" <(echo "$CURRENT_MODELS"))"

if [[ -z "$ADDED_MODELS" && -z "$REMOVED_MODELS" ]]; then
    echo "Status: No model entitlement changes detected since last snapshot."
else
    echo "Status: Model entitlement changes detected!"
    if [[ -n "$ADDED_MODELS" ]]; then
        echo ""
        echo "[+] Newly Unlocked Models:"
        echo "$ADDED_MODELS" | sed 's/^/    + /'
    fi
    if [[ -n "$REMOVED_MODELS" ]]; then
        echo ""
        echo "[-] Removed Models:"
        echo "$REMOVED_MODELS" | sed 's/^/    - /'
    fi

    # Update snapshot
    echo "$CURRENT_MODELS" > "$SNAPSHOT_FILE"

    # Optionally append an alert to the Obsidian Nightly Log
    NIGHTLY_LOG="$VAULT_LOCAL/Brain_Operations/00_Nightly_Log.md"
    if [[ "$NOTIFY_VAULT" == "true" && -f "$NIGHTLY_LOG" ]]; then
        {
            echo ""
            echo "### Model Entitlement Update ($(date '+%Y-%m-%d %H:%M %Z'))"
            echo "- **Antigravity CLI Version:** \`v${AGY_VERSION}\`"
            if [[ -n "$ADDED_MODELS" ]]; then
                echo "- **Newly Unlocked Models:**"
                while IFS= read -r line; do
                    echo "  - \`$line\`"
                done <<< "$ADDED_MODELS"
            fi
            if [[ -n "$REMOVED_MODELS" ]]; then
                echo "- **Removed Models:**"
                while IFS= read -r line; do
                    echo "  - \`$line\`"
                done <<< "$REMOVED_MODELS"
            fi
        } >> "$NIGHTLY_LOG"
        echo "Logged entitlement update to: $NIGHTLY_LOG"
    fi
fi

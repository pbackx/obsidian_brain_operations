#!/usr/bin/env bash
# ==============================================================================
# run_nightly.sh - Antigravity Nightly Autonomous Task Runner
# ==============================================================================

set -euo pipefail

# ------------------------------------------------------------------------------
# 0. Environment Setup: Load NVM and Node for Non-Interactive Shells (Systemd/Cron)
# ------------------------------------------------------------------------------
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
    # shellcheck disable=SC1090
    source "$NVM_DIR/nvm.sh"
fi

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
SYNC_METHOD="${SYNC_METHOD:-obsidian-headless}"
VAULT_REMOTE="${VAULT_REMOTE:-}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/vault_sync/backups}"
MAX_TASKS="${MAX_TASKS:-3}"
EFFORT="${EFFORT:-high}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1800}"

LOG_DIR="$BASE_DIR/logs"
mkdir -p "$LOG_DIR"
TIMESTAMP="$(date +"%Y-%m-%d_%H%M%S")"
LOG_FILE="$LOG_DIR/run_${TIMESTAMP}.log"

# Log all output to terminal and log file
exec > >(tee -a "$LOG_FILE") 2>&1

echo "============================================================"
echo " Starting Antigravity Nightly Run at $(date)"
echo " Vault Local:   $VAULT_LOCAL"
echo " Sync Method:   $SYNC_METHOD"
echo " Node Version:  $(command -v node >/dev/null && node -v || echo 'Not found')"
echo " Obsidian CLI:  $(command -v ob >/dev/null && echo 'Ready' || echo 'Not installed / not in PATH')"
echo " Max Tasks:     $MAX_TASKS"
echo " Effort:        $EFFORT"
echo " Log File:      $LOG_FILE"
echo "============================================================"

# ------------------------------------------------------------------------------
# Step 1: Pre-run Sync
# ------------------------------------------------------------------------------
case "$SYNC_METHOD" in
    "obsidian-headless")
        if command -v ob >/dev/null 2>&1; then
            echo "[1/4] Running pre-turn Obsidian Sync (ob sync)..."
            ob sync --path "$VAULT_LOCAL" || {
                echo "WARNING: Pre-run ob sync encountered an issue, proceeding with local files."
            }
        else
            echo "WARNING: SYNC_METHOD is 'obsidian-headless' but 'ob' CLI is not found in PATH."
            echo "Skipping sync pull. Check NVM setup or run: npm install -g obsidian-headless"
        fi
        ;;
    "rclone")
        if [[ -n "$VAULT_REMOTE" ]] && command -v rclone >/dev/null 2>&1; then
            echo "[1/4] Syncing down from remote ($VAULT_REMOTE) with rclone..."
            mkdir -p "$VAULT_LOCAL" "$BACKUP_DIR"
            rclone sync "$VAULT_REMOTE" "$VAULT_LOCAL" \
                --backup-dir "$BACKUP_DIR/$(date +%Y-%m-%d)" \
                --verbose
        else
            echo "Skipping rclone pull (remote not configured or rclone missing)."
        fi
        ;;
    *)
        echo "[1/4] Sync method is set to '$SYNC_METHOD'. Running against local vault without pre-sync."
        ;;
esac

# Ensure local vault directory exists
if [[ ! -d "$VAULT_LOCAL" ]]; then
    echo "ERROR: Local vault directory does not exist: $VAULT_LOCAL"
    echo "Please set up your vault directory or adjust VAULT_LOCAL in $CONFIG_FILE"
    exit 1
fi

# ------------------------------------------------------------------------------
# Step 2: Run Quick Scan Check
# ------------------------------------------------------------------------------
echo "[2/4] Scanning vault for pending #ai tasks..."
python3 "$SCRIPT_DIR/vault_tool.py" scan --vault "$VAULT_LOCAL" --limit "$MAX_TASKS"

# ------------------------------------------------------------------------------
# Step 3: Execute Antigravity Agent
# ------------------------------------------------------------------------------
echo "[3/4] Launching Antigravity CLI in headless print mode..."
cd "$BASE_DIR"

PROMPT="Activate skill obsidian-nightly-operator. Vault directory is $VAULT_LOCAL. Write all output strictly in English. Run the nightly triage, execute steps for up to $MAX_TASKS prioritized tasks, generate dossiers in Brain_Operations/Dossiers/, update original notes with progress links, and update Brain_Operations/00_Nightly_Log.md."

if agy -p "$PROMPT" \
    --effort "$EFFORT" \
    --dangerously-skip-permissions \
    --print-timeout "${TIMEOUT_SECONDS}s"; then
    echo "Antigravity execution completed successfully."
else
    echo "WARNING: Antigravity execution reported non-zero status or timed out."
fi

# ------------------------------------------------------------------------------
# Step 4: Post-run Sync
# ------------------------------------------------------------------------------
case "$SYNC_METHOD" in
    "obsidian-headless")
        if command -v ob >/dev/null 2>&1; then
            echo "[4/4] Pushing changes to remote Obsidian Sync (ob sync)..."
            ob sync --path "$VAULT_LOCAL" || {
                echo "WARNING: Post-run ob sync encountered an issue."
            }
            echo "Obsidian Sync complete."
        fi
        ;;
    "rclone")
        if [[ -n "$VAULT_REMOTE" ]] && command -v rclone >/dev/null 2>&1; then
            echo "[4/4] Syncing changes back up to remote ($VAULT_REMOTE) with rclone..."
            rclone sync "$VAULT_LOCAL" "$VAULT_REMOTE" --verbose
            echo "rclone push complete."
        fi
        ;;
    *)
        echo "[4/4] Post-run sync skipped ($SYNC_METHOD)."
        ;;
esac

echo "============================================================"
echo " Nightly run finished at $(date)"
echo "============================================================"

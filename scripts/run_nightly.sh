#!/usr/bin/env bash
# ==============================================================================
# run_nightly.sh - Antigravity Nightly Autonomous Task Runner
# ==============================================================================

# Wrap the entire script in a brace group `{ ...; exit $?; }` so Bash parses
# the full script into memory at startup. This prevents byte-offset corruption
# if an autonomous agent modifies run_nightly.sh in-place during a nightly run.
{
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
    set -a
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
    set +a
fi

VAULT_LOCAL="${VAULT_LOCAL:-$HOME/vault_sync/ObsidianVault}"
SYNC_METHOD="${SYNC_METHOD:-obsidian-headless}"
VAULT_REMOTE="${VAULT_REMOTE:-}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/vault_sync/backups}"
MAX_TASKS="${MAX_TASKS:-3}"
AGENT_RUNNER="${AGENT_RUNNER:-agy}"
PI_MODEL="${PI_MODEL:-openrouter/anthropic/claude-sonnet-latest}"
PI_OPENROUTER_PROVIDER="${PI_OPENROUTER_PROVIDER:-}"
EFFORT="${EFFORT:-high}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-10800}"
TASK_TIMEOUT="${TASK_TIMEOUT:-1800}"
MIN_TASK_TIMEOUT="${MIN_TASK_TIMEOUT:-120}"
MAX_TASK_TIMEOUT="${MAX_TASK_TIMEOUT:-7200}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-14}"

# Quota-aware model routing & automatic fallback settings
QUOTA_AUTO_DOWNGRADE="${QUOTA_AUTO_DOWNGRADE:-true}"
PRIMARY_MODEL="${PRIMARY_MODEL:-gemini-4-argon-high}"
ARGON_MEDIUM_BELOW_PCT="${ARGON_MEDIUM_BELOW_PCT:-0}"
ARGON_RESERVE_PCT="${ARGON_RESERVE_PCT:-20}"
FALLBACK_MODELS="${FALLBACK_MODELS:-claude-opus-5-5-high,gemini-3.8-flash-high,claude-sonnet-5-5-high,gemini-3.8-flash-medium}"
FALLBACK_MIN_PCT="${FALLBACK_MIN_PCT:-5}"

# Optional Freewrite Postbox (Google Drive / NAS) ingestion settings
FREEWRITE_ENABLED="${FREEWRITE_ENABLED:-false}"
FREEWRITE_SOURCE_DIR="${FREEWRITE_SOURCE_DIR:-$HOME/vault_sync/freewrite_postbox}"
FREEWRITE_RCLONE_REMOTE="${FREEWRITE_RCLONE_REMOTE:-}"
FREEWRITE_VAULT_SUBDIR="${FREEWRITE_VAULT_SUBDIR:-Freewrite}"
FREEWRITE_FOLDERS="${FREEWRITE_FOLDERS:-}"
FREEWRITE_AI_FOLDERS="${FREEWRITE_AI_FOLDERS:-}"

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
echo " Pi CLI:        $(command -v pi >/dev/null && pi --version 2>/dev/null || echo 'Not installed / not in PATH')"
echo " Default Runner: $AGENT_RUNNER (Pi default model: $PI_MODEL)"
echo " Max Tasks:     $MAX_TASKS"
echo " Time Budget:   ${TIMEOUT_SECONDS}s total (default ${TASK_TIMEOUT}s/task, max ${MAX_TASK_TIMEOUT}s/task)"
echo " Effort:        $EFFORT"
echo " Auto Fallback: $QUOTA_AUTO_DOWNGRADE (Primary: $PRIMARY_MODEL, Argon Reserve: ${ARGON_RESERVE_PCT}%)"
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
# Step 1b: Optional Freewrite Postbox Ingestion (Google Drive / NAS -> Vault)
# ------------------------------------------------------------------------------
if [[ "${FREEWRITE_ENABLED,,}" == "true" ]]; then
    echo "[1b/4] Checking Freewrite Postbox inbox..."
    if [[ -n "$FREEWRITE_RCLONE_REMOTE" ]] && command -v rclone >/dev/null 2>&1; then
        mkdir -p "$FREEWRITE_SOURCE_DIR"
        echo "       Pulling Freewrite drafts from $FREEWRITE_RCLONE_REMOTE -> $FREEWRITE_SOURCE_DIR..."
        rclone copy "$FREEWRITE_RCLONE_REMOTE" "$FREEWRITE_SOURCE_DIR" || {
            echo "WARNING: rclone copy from $FREEWRITE_RCLONE_REMOTE encountered an issue."
        }
    fi
    if [[ -d "$FREEWRITE_SOURCE_DIR" ]]; then
        python3 "$SCRIPT_DIR/freewrite_ingest.py" \
            --source "$FREEWRITE_SOURCE_DIR" \
            --vault "$VAULT_LOCAL" \
            --subdir "$FREEWRITE_VAULT_SUBDIR" \
            ${FREEWRITE_FOLDERS:+--folders "$FREEWRITE_FOLDERS"} \
            ${FREEWRITE_AI_FOLDERS:+--ai-folders "$FREEWRITE_AI_FOLDERS"} || {
            echo "WARNING: Freewrite ingestion encountered an error."
        }
    else
        echo "WARNING: FREEWRITE_ENABLED is true, but source directory not found: $FREEWRITE_SOURCE_DIR"
    fi
fi

# ------------------------------------------------------------------------------
# Step 2: Overview Scan, Archive Sweep & Live Quota Check
# ------------------------------------------------------------------------------
echo "[2/4] Archiving any task notes marked status: archived -> TODO/Archive/..."
python3 "$SCRIPT_DIR/vault_tool.py" archive --vault "$VAULT_LOCAL" || {
    echo "WARNING: Archive sweep encountered an issue."
}

echo "[2a/4] Rotating Brain_Operations/00_Nightly_Log.md (${LOG_RETENTION_DAYS}-day rolling window)..."
python3 "$SCRIPT_DIR/vault_tool.py" rotate-log --vault "$VAULT_LOCAL" --keep-days "$LOG_RETENTION_DAYS" || {
    echo "WARNING: Nightly log rotation encountered an issue."
}

echo "[2b/4] Scanning vault for pending status: ai-queue tasks..."
python3 "$SCRIPT_DIR/vault_tool.py" scan --vault "$VAULT_LOCAL" --limit "$MAX_TASKS"

if [[ -f "$SCRIPT_DIR/cover_tool.py" ]]; then
    echo "[2c/4] Ensuring local SVG emoji covers for all cards on the AI Workflow Board..."
    python3 "$SCRIPT_DIR/cover_tool.py" ensure --vault "$VAULT_LOCAL" || {
        echo "WARNING: Cover ensure sweep encountered an issue."
    }
fi

if [[ -f "$SCRIPT_DIR/check_quota.py" ]]; then
    echo "[2d/4] Checking live Antigravity quota buckets..."
    python3 "$SCRIPT_DIR/check_quota.py" || {
        echo "WARNING: Quota check encountered an issue, continuing with default model settings."
    }
fi

# ------------------------------------------------------------------------------
# Step 3: Sequential Task Loop (Isolated Clean Context per Task)
# ------------------------------------------------------------------------------
echo "[3/4] Starting sequential task loop (up to $MAX_TASKS tasks, ${TIMEOUT_SECONDS}s total budget)..."
cd "$BASE_DIR"

PROCESSED_IDS=""
TASK_COUNT=0
LOOP_START=$(date +%s)

for ((i=1; i<=MAX_TASKS; i++)); do
    NOW=$(date +%s)
    ELAPSED=$((NOW - LOOP_START))
    REMAINING=$((TIMEOUT_SECONDS - ELAPSED))

    if (( REMAINING <= 60 )); then
        echo "------------------------------------------------------------"
        echo " Total nightly time budget (${TIMEOUT_SECONDS}s) reached (${ELAPSED}s elapsed)."
        echo " Stopping loop cleanly so post-run sync can complete."
        echo "------------------------------------------------------------"
        break
    fi

    echo "------------------------------------------------------------"
    echo " Locating next prioritized task ($i/$MAX_TASKS) | Budget: ${ELAPSED}s / ${TIMEOUT_SECONDS}s used"
    echo "------------------------------------------------------------"

    TASK_JSON=$(python3 "$SCRIPT_DIR/vault_tool.py" next --vault "$VAULT_LOCAL" ${PROCESSED_IDS:+--exclude "$PROCESSED_IDS"})

    if [[ -z "$TASK_JSON" || "$TASK_JSON" == "null" || "$TASK_JSON" == "{}" ]]; then
        echo "No more pending status: ai-queue tasks found tonight."
        break
    fi

    # Parse task details safely
    TASK_ID=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('id', ''))" "$TASK_JSON")
    TASK_FILE=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('file', ''))" "$TASK_JSON")
    TASK_TITLE=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('title', ''))" "$TASK_JSON")
    TASK_CATEGORY=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('category', 'general'))" "$TASK_JSON")
    TASK_IS_FOLLOWUP=$(python3 -c "import sys, json; print('true' if json.loads(sys.argv[1]).get('is_followup') else 'false')" "$TASK_JSON")
    TASK_TIMEOUT_RAW=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('timeout') or '')" "$TASK_JSON")
    TASK_CUSTOM_TIMEOUT=$(python3 -c "import sys, json; v = json.loads(sys.argv[1]).get('timeout_seconds'); print(v if isinstance(v, int) and v > 0 else '')" "$TASK_JSON")
    TASK_RUNNER_RAW=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('runner') or '')" "$TASK_JSON")
    TASK_MODEL_RAW=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('model') or '')" "$TASK_JSON")
    TASK_PROVIDER_RAW=$(python3 -c "import sys, json; print(json.loads(sys.argv[1]).get('provider') or '')" "$TASK_JSON")

    # Resolve runner (frontmatter override vs AGENT_RUNNER default 'agy')
    CURRENT_RUNNER="${TASK_RUNNER_RAW:-$AGENT_RUNNER}"
    CURRENT_RUNNER="${CURRENT_RUNNER,,}"
    if [[ "$CURRENT_RUNNER" != "agy" && "$CURRENT_RUNNER" != "pi" ]]; then
        echo ">>> WARNING: Unrecognized runner '$CURRENT_RUNNER' for $TASK_FILE; falling back to 'agy'."
        CURRENT_RUNNER="agy"
    fi
    if [[ -n "$TASK_RUNNER_RAW" ]]; then
        RUNNER_SOURCE="frontmatter override"
    else
        RUNNER_SOURCE="default"
    fi

    # Resolve per-task timeout (frontmatter override vs default TASK_TIMEOUT) with safety bounds
    if [[ -n "$TASK_CUSTOM_TIMEOUT" ]]; then
        CURRENT_TASK_TIMEOUT="$TASK_CUSTOM_TIMEOUT"
        TIMEOUT_SOURCE="frontmatter override: ${TASK_TIMEOUT_RAW}"
    else
        CURRENT_TASK_TIMEOUT="$TASK_TIMEOUT"
        TIMEOUT_SOURCE="default"
    fi
    if (( CURRENT_TASK_TIMEOUT < MIN_TASK_TIMEOUT )); then
        CURRENT_TASK_TIMEOUT="$MIN_TASK_TIMEOUT"
        TIMEOUT_SOURCE="${TIMEOUT_SOURCE}, clamped to min ${MIN_TASK_TIMEOUT}s"
    fi
    if (( CURRENT_TASK_TIMEOUT > MAX_TASK_TIMEOUT )); then
        CURRENT_TASK_TIMEOUT="$MAX_TASK_TIMEOUT"
        TIMEOUT_SOURCE="${TIMEOUT_SOURCE}, capped at max ${MAX_TASK_TIMEOUT}s"
    fi
    if (( REMAINING < CURRENT_TASK_TIMEOUT )); then
        CURRENT_TASK_TIMEOUT="$REMAINING"
        TIMEOUT_SOURCE="${TIMEOUT_SOURCE}, capped at remaining nightly budget"
    fi

    # Resolve runner-specific model before starting the task
    SELECTED_MODEL=""
    AGY_MODEL_ARGS=(--effort "$EFFORT")
    PI_MODEL_ARGS=()
    PI_EXT_ARGS=()
    PI_SELECTED_PROVIDER=""
    PROVIDER_SOURCE=""
    DISPLAY_MODEL=""

    if [[ "$CURRENT_RUNNER" == "pi" ]]; then
        PI_RAW_MODEL="${TASK_MODEL_RAW:-$PI_MODEL}"
        if [[ "$PI_RAW_MODEL" == ollama/* ]]; then
            PI_MODEL_ARGS=(--model "$PI_RAW_MODEL")
            DISPLAY_MODEL="$PI_RAW_MODEL"
        else
            PI_OPENROUTER_MODEL="${PI_RAW_MODEL#openrouter/}"
            PI_MODEL_ARGS=(--provider openrouter --model "$PI_OPENROUTER_MODEL")
            DISPLAY_MODEL="openrouter/${PI_OPENROUTER_MODEL}"

            PI_SELECTED_PROVIDER="${TASK_PROVIDER_RAW:-$PI_OPENROUTER_PROVIDER}"
            if [[ -n "$TASK_PROVIDER_RAW" ]]; then
                PROVIDER_SOURCE="frontmatter override"
            elif [[ -n "$PI_OPENROUTER_PROVIDER" ]]; then
                PROVIDER_SOURCE="default PI_OPENROUTER_PROVIDER"
            fi
            if [[ -n "$PI_SELECTED_PROVIDER" && -f "$SCRIPT_DIR/pi_openrouter_provider.ts" ]]; then
                PI_EXT_ARGS=(-e "$SCRIPT_DIR/pi_openrouter_provider.ts")
            fi
        fi
        if [[ -n "$TASK_MODEL_RAW" ]]; then
            DISPLAY_MODEL="${DISPLAY_MODEL} (frontmatter override)"
        else
            DISPLAY_MODEL="${DISPLAY_MODEL} (default PI_MODEL)"
        fi
    else
        # Keep existing agy quota-aware model resolution logic
        if [[ "${QUOTA_AUTO_DOWNGRADE,,}" == "true" && -f "$SCRIPT_DIR/check_quota.py" ]]; then
            SELECTED_MODEL=$(python3 "$SCRIPT_DIR/check_quota.py" --select-model 2>/dev/null || echo "")
            if [[ -n "$SELECTED_MODEL" ]]; then
                # Model slugs in agy (e.g. gemini-4-argon-high, claude-opus-5-5-high, gemini-3.8-flash-high)
                # already encode their effort level; passing --model sets both model and effort cleanly.
                AGY_MODEL_ARGS=(--model "$SELECTED_MODEL")
            fi
        fi
        DISPLAY_MODEL="${SELECTED_MODEL:-default (--effort $EFFORT)}"
    fi

    echo ">>> [Task $i/$MAX_TASKS] Starting: $TASK_TITLE"
    echo "    File:     $TASK_FILE (category: $TASK_CATEGORY, follow-up: $TASK_IS_FOLLOWUP)"
    echo "    ID:       $TASK_ID"
    echo "    Runner:   $CURRENT_RUNNER ($RUNNER_SOURCE)"
    echo "    Model:    $DISPLAY_MODEL"
    if [[ -n "$PI_SELECTED_PROVIDER" ]]; then
        echo "    Provider: $PI_SELECTED_PROVIDER ($PROVIDER_SOURCE)"
    fi
    echo "    Timeout:  ${CURRENT_TASK_TIMEOUT}s (${TIMEOUT_SOURCE} | Remaining nightly budget: ${REMAINING}s)"

    TASK_PROMPT=$(python3 -c '
import sys, json
t = json.loads(sys.argv[1])
vault = sys.argv[2]
timeout_sec = int(sys.argv[3])
timeout_min = round(timeout_sec / 60, 1)
f = t.get("file", "")
is_followup = t.get("is_followup", False)
summary = t.get("summary", "").strip()
question = t.get("question", "").strip()
remarks = t.get("notes_and_remarks", "").strip()

lines = [
    f"Activate skill obsidian-nightly-operator. Vault directory is {vault}. Write all output strictly in English.",
    f"Process ONLY the single task note at '\''{f}'\''.",
    "Ingest Brain_Operations/MEMORY.md if present.",
    f"Time Budget: You have a hard timeout of {timeout_sec}s (~{timeout_min:g} minutes) for this task. Reserve the final 3-4 minutes to commit/push any code changes, update the task note via vault_tool.py, and append to Brain_Operations/00_Nightly_Log.md so you never get cut off mid-wrap-up.",
]
if is_followup:
    lines.append("")
    lines.append("=== HIGH-PRIORITY FOLLOW-UP DIRECTIVE ===")
    lines.append("This task was previously processed and has been re-queued by the user with new remarks/answers in '\''## Notes & Remarks'\''.")
    if summary:
        lines.append(f"Previous AI Summary: {summary}")
    if question:
        lines.append(f"Previous Question Asked to User: {question}")
    if remarks:
        lines.append("User'\''s '\''## Notes & Remarks'\'' Section (focus especially on the newest/bottom items and how they answer the previous question or redirect the work):")
        lines.append("<<<NOTES_AND_REMARKS")
        lines.append(remarks)
        lines.append("NOTES_AND_REMARKS>>>")
    lines.append("Your #1 priority is to address the user'\''s newest remarks and answers in '\''## Notes & Remarks'\'' rather than repeating prior research.")
    lines.append("=========================================")
    lines.append("")
elif remarks:
    lines.append("")
    lines.append("User'\''s '\''## Notes & Remarks'\'' Section:")
    lines.append("<<<NOTES_AND_REMARKS")
    lines.append(remarks)
    lines.append("NOTES_AND_REMARKS>>>")
    lines.append("")

lines.append(f"Advance the task, update '\''{f}'\'' in-place with your findings/architecture/activity log (keeping '\''## Notes & Remarks'\'' intact at the top), run vault_tool.py update --file '\''{f}'\'' (setting --status '\''completed'\'' or '\''needs-clarification'\'', and optionally --emoji '\''<1-3 fitting emojis>'\''), and append tonight'\''s action to Brain_Operations/00_Nightly_Log.md (do NOT read Brain_Operations/Archive/Nightly_Log_*.md cold storage unless historical log entries older than 14 days are specifically needed).")
print("\n".join(lines))
' "$TASK_JSON" "$VAULT_LOCAL" "$CURRENT_TASK_TIMEOUT")

    TASK_START=$(date +%s)
    if [[ "$CURRENT_RUNNER" == "pi" ]]; then
        if ! command -v pi >/dev/null 2>&1; then
            echo ">>> ERROR: [Task $i/$MAX_TASKS] 'pi' CLI not found in PATH."
        elif [[ "${PI_RAW_MODEL:-}" != ollama/* && -z "${OPENROUTER_API_KEY:-}" ]]; then
            echo ">>> ERROR: [Task $i/$MAX_TASKS] runner 'pi' requires OPENROUTER_API_KEY in environment or config/settings.env."
        else
            if PI_NO_GATE=1 PI_OPENROUTER_PROVIDER="$PI_SELECTED_PROVIDER" timeout --signal=TERM --kill-after=30s "${CURRENT_TASK_TIMEOUT}s" \
                pi -p --approve "${PI_EXT_ARGS[@]}" "${PI_MODEL_ARGS[@]}" "$TASK_PROMPT"; then
                TASK_END=$(date +%s)
                TASK_DURATION=$((TASK_END - TASK_START))
                echo ">>> [Task $i/$MAX_TASKS] Completed successfully in ${TASK_DURATION}s (runner: pi, model: ${DISPLAY_MODEL})."
            else
                PI_RC=$?
                TASK_END=$(date +%s)
                TASK_DURATION=$((TASK_END - TASK_START))
                echo ">>> WARNING: [Task $i/$MAX_TASKS] Pi exited with code ${PI_RC} after ${TASK_DURATION}s (model: ${DISPLAY_MODEL})."
            fi

            # Print concise telemetry & error diagnostics from Pi's session JSONL log
            python3 -c '
import json, os, sys
from pathlib import Path

task_start = int(sys.argv[1])
sessions_root = Path(os.path.expanduser("~/.pi/agent/sessions"))
if not sessions_root.exists():
    sys.exit(0)

candidates = [p for p in sessions_root.rglob("*.jsonl") if p.stat().st_mtime >= task_start - 2]
if not candidates:
    print("    Pi Log:   No session JSONL file created.")
    sys.exit(0)

latest = max(candidates, key=lambda p: p.stat().st_mtime)
turns = 0
tool_calls = []
total_cost = 0.0
last_stop = ""
last_raw_stop = ""
last_err = ""

for line in latest.read_text(encoding="utf-8", errors="replace").splitlines():
    if not line.strip():
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get("type") != "message":
        continue
    msg = ev.get("message", {})
    role = msg.get("role")
    if role == "assistant":
        turns += 1
        usage = msg.get("usage") or {}
        cost = (usage.get("cost") or {}).get("total")
        if isinstance(cost, (int, float)):
            total_cost += cost
        last_stop = msg.get("stopReason") or last_stop
        last_raw_stop = msg.get("rawStopReason") or last_raw_stop
        last_err = msg.get("errorMessage") or last_err
        for part in msg.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "toolCall":
                tool_calls.append(part.get("name", "?"))
    elif role == "toolResult" and msg.get("isError"):
        tname = msg.get("toolName", "?")
        tool_calls.append(f"{tname}[ERR]")

recent_tools = ", ".join(tool_calls[-8:]) if tool_calls else "none"
print(f"    Pi Log:   {latest}")
print(f"    Pi Stats: {turns} turn(s), {len(tool_calls)} tool call(s) ({recent_tools}), cost=${total_cost:.4f}, stop={last_stop}/{last_raw_stop}")
if last_err:
    print(f"    Pi Error: {last_err}")
' "$TASK_START" || true
        fi
    else
        if agy -p "$TASK_PROMPT" \
            "${AGY_MODEL_ARGS[@]}" \
            --dangerously-skip-permissions \
            --print-timeout "${CURRENT_TASK_TIMEOUT}s"; then
            TASK_END=$(date +%s)
            TASK_DURATION=$((TASK_END - TASK_START))
            echo ">>> [Task $i/$MAX_TASKS] Completed successfully in ${TASK_DURATION}s (runner: agy, model: ${SELECTED_MODEL:-default})."
        else
            TASK_END=$(date +%s)
            TASK_DURATION=$((TASK_END - TASK_START))
            echo ">>> WARNING: [Task $i/$MAX_TASKS] Reported non-zero status or timed out after ${TASK_DURATION}s (runner: agy, model: ${SELECTED_MODEL:-default})."

            # If auto-downgrade is enabled and the failure happened quickly (e.g., mid-task quota exhaustion
            # rather than a full task timeout), attempt one automatic fallback retry on the next model bucket.
            if [[ "${QUOTA_AUTO_DOWNGRADE,,}" == "true" && -n "$SELECTED_MODEL" ]] && (( TASK_DURATION < CURRENT_TASK_TIMEOUT - 30 )); then
                RETRY_MODEL=$(python3 "$SCRIPT_DIR/check_quota.py" --select-model --exclude-models "$SELECTED_MODEL" 2>/dev/null || echo "")
                if [[ -n "$RETRY_MODEL" && "$RETRY_MODEL" != "$SELECTED_MODEL" ]]; then
                    echo ">>> [Task $i/$MAX_TASKS] Retrying automatically with fallback model: $RETRY_MODEL"
                    RETRY_START=$(date +%s)
                    if agy -p "$TASK_PROMPT" \
                        --model "$RETRY_MODEL" \
                        --dangerously-skip-permissions \
                        --print-timeout "${CURRENT_TASK_TIMEOUT}s"; then
                        RETRY_END=$(date +%s)
                        echo ">>> [Task $i/$MAX_TASKS] Fallback retry on $RETRY_MODEL succeeded in $((RETRY_END - RETRY_START))s."
                    else
                        RETRY_END=$(date +%s)
                        echo ">>> WARNING: [Task $i/$MAX_TASKS] Fallback retry on $RETRY_MODEL also failed after $((RETRY_END - RETRY_START))s."
                    fi
                fi
            fi
        fi
    fi

    # Track processed task ID to prevent duplicate runs
    if [[ -z "$PROCESSED_IDS" ]]; then
        PROCESSED_IDS="$TASK_ID"
    else
        PROCESSED_IDS="$PROCESSED_IDS,$TASK_ID"
    fi
    TASK_COUNT=$((TASK_COUNT + 1))
done

TOTAL_ELAPSED=$(( $(date +%s) - LOOP_START ))
echo "============================================================"
echo " Finished processing $TASK_COUNT task(s) in ${TOTAL_ELAPSED}s."
echo "============================================================"

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
exit $?
}

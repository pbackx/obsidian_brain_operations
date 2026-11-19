#!/usr/bin/env bash
# ==============================================================================
# run_pi.sh - Launch Pi with config/settings.env exported into the environment
# ==============================================================================
# Supports optional --or-provider <slug[,slug2]> (or PI_OPENROUTER_PROVIDER=...)
# to restrict OpenRouter upstream providers (e.g. --or-provider nebius).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CALLER_OR_PROVIDER="${PI_OPENROUTER_PROVIDER:-}"
CLI_OR_PROVIDER=""
PASS_ARGS=()

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --or-provider|--openrouter-provider)
            if [[ "$#" -lt 2 ]]; then
                echo "ERROR: $1 requires a provider slug (e.g. --or-provider nebius)" >&2
                exit 2
            fi
            CLI_OR_PROVIDER="$2"
            shift 2
            ;;
        --or-provider=*|--openrouter-provider=*)
            CLI_OR_PROVIDER="${1#*=}"
            shift
            ;;
        *)
            PASS_ARGS+=("$1")
            shift
            ;;
    esac
done

# shellcheck disable=SC1091
source "$SCRIPT_DIR/load_env.sh"

EFFECTIVE_OR_PROVIDER="${CLI_OR_PROVIDER:-${CALLER_OR_PROVIDER:-${PI_OPENROUTER_PROVIDER:-}}}"
if [[ -n "$EFFECTIVE_OR_PROVIDER" ]]; then
    export PI_OPENROUTER_PROVIDER="$EFFECTIVE_OR_PROVIDER"
fi

PI_ARGS=()
if [[ -n "${PI_OPENROUTER_PROVIDER:-}" && -f "$SCRIPT_DIR/pi_openrouter_provider.ts" ]]; then
    PI_ARGS+=(-e "$SCRIPT_DIR/pi_openrouter_provider.ts")
fi

exec pi ${PI_ARGS[@]+"${PI_ARGS[@]}"} ${PASS_ARGS[@]+"${PASS_ARGS[@]}"}

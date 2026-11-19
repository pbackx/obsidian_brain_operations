#!/usr/bin/env bash
# ==============================================================================
# run_pi.sh - Launch Pi with config/settings.env exported into the environment
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/load_env.sh"

PI_ARGS=()
if [[ -n "${PI_OPENROUTER_PROVIDER:-}" && -f "$SCRIPT_DIR/pi_openrouter_provider.ts" ]]; then
    PI_ARGS+=(-e "$SCRIPT_DIR/pi_openrouter_provider.ts")
fi

exec pi ${PI_ARGS[@]+"${PI_ARGS[@]}"} "$@"

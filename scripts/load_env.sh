#!/usr/bin/env bash
# ==============================================================================
# load_env.sh - Load and export environment variables from config/settings.env
# ==============================================================================
# Usage:
#   source scripts/load_env.sh             # Export variables into current shell
#   ./scripts/load_env.sh <cmd> [args...]  # Run a command with exported variables

_load_brain_ops_env() {
    local script_dir base_dir config_file was_allexport
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    base_dir="$(dirname "$script_dir")"
    config_file="${BRAIN_OPS_CONFIG_FILE:-$base_dir/config/settings.env}"

    if [[ ! -f "$config_file" ]]; then
        echo "ERROR: Config file not found: $config_file" >&2
        return 1
    fi

    case $- in
        *a*) was_allexport=1 ;;
        *)   was_allexport=0 ;;
    esac

    set -a
    # shellcheck disable=SC1090
    source "$config_file"
    if [[ "$was_allexport" -eq 0 ]]; then
        set +a
    fi
}

if (return 0 2>/dev/null); then
    _load_brain_ops_env
    unset -f _load_brain_ops_env
else
    set -euo pipefail
    _load_brain_ops_env
    unset -f _load_brain_ops_env
    if [[ "$#" -gt 0 ]]; then
        exec "$@"
    else
        echo "Note: To export variables into your current shell, run:"
        echo "  source scripts/load_env.sh"
        echo "Or pass a command directly:"
        echo "  ./scripts/load_env.sh pi"
        echo "  ./scripts/run_pi.sh"
    fi
fi

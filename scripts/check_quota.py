#!/usr/bin/env python3
"""
check_quota.py - Antigravity CLI (agy) Live Quota Inspector & Model Router

Queries live account quota buckets via `agy -p "/usage" --output-format json`
(zero token consumption), displays per-bucket remaining percentages and local
reset times, and selects the optimal model for `run_nightly.sh` with automatic
downgrade/fallback when Gemini Models quota drops below configured thresholds.

Usage:
  ./scripts/check_quota.py                        # Human-readable table + active routing decision
  ./scripts/check_quota.py --json                 # Full JSON report (buckets + routing decision)
  ./scripts/check_quota.py --select-model         # Print only the selected model slug (for scripts)
  ./scripts/check_quota.py --markdown             # Print concise Markdown summary for Obsidian logs
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Map model prefixes to the `command.data.groups[].name` returned by `agy -p "/usage"`
MODEL_GROUP_MAP = {
    "gemini-": "Gemini Models",
    "claude-": "Claude and GPT models",
    "gpt-": "Claude and GPT models",
}


def load_env_file(env_path: Path) -> Dict[str, str]:
    """Parse simple KEY=VALUE lines from config/settings.env if present."""
    env_vars: Dict[str, str] = {}
    if not env_path.is_file():
        return env_vars
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        env_vars[key] = val
    return env_vars


def get_setting(key: str, default: str, file_env: Dict[str, str]) -> str:
    """Resolve configuration from environment first, then settings.env, then default."""
    return os.environ.get(key, file_env.get(key, default))


def find_agy_binary() -> str:
    """Locate the `agy` CLI binary."""
    local_bin = Path.home() / ".local" / "bin" / "agy"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    which_agy = shutil.which("agy")
    if which_agy:
        return which_agy
    raise FileNotFoundError("Could not find 'agy' binary in ~/.local/bin or PATH.")


def fetch_usage_json(agy_bin: str, timeout: int = 25) -> Dict[str, Any]:
    """Run `agy -p "/usage" --output-format json` and parse the JSON response."""
    proc = subprocess.run(
        [agy_bin, "-p", "/usage", "--output-format", "json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(
            f"`agy -p /usage` returned empty stdout (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    # Locate the JSON object line in stdout
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            data = json.loads(line)
            if data.get("status") != "SUCCESS":
                raise RuntimeError(f"`agy -p /usage` reported error: {data.get('error', data)}")
            return data
    raise RuntimeError(f"Failed to parse JSON from `agy -p /usage`: {stdout[:300]}")


def format_reset_time(iso_str: Optional[str]) -> Tuple[str, str]:
    """Convert UTC ISO-8601 timestamp to local time string and relative countdown."""
    if not iso_str:
        return ("N/A", "N/A")
    try:
        dt_utc = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone()
        now_local = datetime.datetime.now(datetime.timezone.utc).astimezone()
        delta = dt_local - now_local
        total_seconds = max(0, int(delta.total_seconds()))
        days, rem = divmod(total_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        if days > 0:
            rel = f"{days}d {hours}h"
        elif hours > 0:
            rel = f"{hours}h {minutes}m"
        else:
            rel = f"{minutes}m"
        return (dt_local.strftime("%Y-%m-%d %H:%M %Z"), rel)
    except Exception:
        return (iso_str, "N/A")


def parse_groups(usage_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Normalize quota groups and compute effective remaining percentage per group."""
    groups_raw = usage_payload.get("command", {}).get("data", {}).get("groups", [])
    groups: Dict[str, Dict[str, Any]] = {}
    for g in groups_raw:
        name = g.get("name", "Unknown")
        buckets_raw = g.get("buckets", [])
        buckets: List[Dict[str, Any]] = []
        active_pcts: List[float] = []
        weekly_pct: Optional[float] = None
        five_hour_pct: Optional[float] = None
        for b in buckets_raw:
            is_disabled = bool(b.get("disabled", False))
            frac = float(b.get("remaining_fraction", 0.0))
            pct = round(frac * 100.0, 2)
            window = b.get("window", "")
            if not is_disabled:
                active_pcts.append(pct)
                if window == "weekly":
                    weekly_pct = pct
                elif window == "5h":
                    five_hour_pct = pct
            local_str, rel_str = format_reset_time(b.get("reset_time"))
            buckets.append(
                {
                    "id": b.get("id", ""),
                    "name": b.get("name", ""),
                    "window": window,
                    "disabled": is_disabled,
                    "remaining_fraction": frac,
                    "remaining_pct": pct,
                    "reset_time_utc": b.get("reset_time"),
                    "reset_time_local": local_str,
                    "reset_in": rel_str,
                }
            )
        groups[name] = {
            "name": name,
            "description": g.get("description", ""),
            "effective_remaining_pct": min(active_pcts) if active_pcts else 0.0,
            "weekly_remaining_pct": weekly_pct,
            "five_hour_remaining_pct": five_hour_pct,
            "buckets": buckets,
        }
    return groups


def group_for_model(model_slug: str) -> Optional[str]:
    """Return the quota group name corresponding to a model slug."""
    slug = model_slug.strip().lower()
    for prefix, group_name in MODEL_GROUP_MAP.items():
        if slug.startswith(prefix):
            return group_name
    return None


def select_model(
    groups: Dict[str, Dict[str, Any]],
    primary_model: str,
    gemini_stepdown_below_pct: float,
    gemini_stepdown_model: str,
    gemini_reserve_pct: float,
    fallback_models: List[str],
    fallback_min_pct: float,
    excluded: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """
    Determine which model to run based on live quota buckets and configured thresholds.
    """
    excluded_set = excluded or set()
    primary_group_name = group_for_model(primary_model)
    primary_group = groups.get(primary_group_name or "", {})
    primary_eff_pct = float(primary_group.get("effective_remaining_pct", 0.0))
    primary_reserve = gemini_reserve_pct if primary_group_name == "Gemini Models" else fallback_min_pct

    # Case 1: Primary model is allowed and its pool is at or above reserve threshold
    if primary_model not in excluded_set and primary_eff_pct >= primary_reserve:
        if (
            primary_group_name == "Gemini Models"
            and gemini_stepdown_below_pct > gemini_reserve_pct
            and primary_eff_pct < gemini_stepdown_below_pct
            and gemini_stepdown_model
            and gemini_stepdown_model != primary_model
            and gemini_stepdown_model not in excluded_set
        ):
            return {
                "selected_model": gemini_stepdown_model,
                "tier_status": "stepped-down-gemini",
                "group": primary_group_name,
                "effective_remaining_pct": primary_eff_pct,
                "reason": (
                    f"Gemini Models effective quota ({primary_eff_pct:.1f}%) is below "
                    f"GEMINI_STEPDOWN_BELOW_PCT ({gemini_stepdown_below_pct:.0f}%) but above "
                    f"GEMINI_RESERVE_PCT ({gemini_reserve_pct:.0f}%); stepped down to "
                    f"'{gemini_stepdown_model}' to conserve quota."
                ),
            }
        return {
            "selected_model": primary_model,
            "tier_status": "primary",
            "group": primary_group_name,
            "effective_remaining_pct": primary_eff_pct,
            "reason": (
                f"{primary_group_name or 'Primary'} effective quota ({primary_eff_pct:.1f}%) "
                f"is at or above reserve threshold ({primary_reserve:.0f}%)."
            ),
        }

    # Case 2: Primary bucket is below reserve threshold (or primary excluded) -> walk fallback chain
    for candidate in fallback_models:
        cand = candidate.strip()
        if not cand or cand in excluded_set:
            continue
        cand_group_name = group_for_model(cand)
        cand_group = groups.get(cand_group_name or "", {})
        cand_eff_pct = float(cand_group.get("effective_remaining_pct", 0.0))
        if cand_eff_pct >= fallback_min_pct:
            why_prefix = (
                f"Primary '{primary_model}' was excluded"
                if primary_model in excluded_set
                else f"Primary '{primary_model}' quota ({primary_eff_pct:.1f}%) is below reserve threshold ({primary_reserve:.0f}%)"
            )
            return {
                "selected_model": cand,
                "tier_status": "fallback",
                "group": cand_group_name,
                "effective_remaining_pct": cand_eff_pct,
                "reason": (
                    f"{why_prefix}; automatically routed to '{cand}' "
                    f"({cand_group_name}: {cand_eff_pct:.1f}% remaining)."
                ),
            }

    # Ultimate fallback if everything is below threshold
    non_excluded_fallbacks = [m for m in fallback_models if m not in excluded_set]
    last_resort = non_excluded_fallbacks[-1] if non_excluded_fallbacks else primary_model
    return {
        "selected_model": last_resort,
        "tier_status": "exhausted-last-resort",
        "group": group_for_model(last_resort),
        "effective_remaining_pct": 0.0,
        "reason": "All configured quota buckets are below reserve thresholds; using last-resort model.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Antigravity CLI (agy) quota buckets and select optimal model."
    )
    parser.add_argument("--json", action="store_true", help="Output full report as JSON")
    parser.add_argument(
        "--select-model",
        action="store_true",
        help="Output only the selected model slug for shell scripting",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Output a Markdown quota summary table",
    )
    parser.add_argument(
        "--exclude-models",
        default="",
        help="Comma-separated list of models to exclude (e.g. after a mid-run failure)",
    )
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent.parent
    file_env = load_env_file(base_dir / "config" / "settings.env")

    primary_model = get_setting("PRIMARY_MODEL", "gemini-4-carbon-medium", file_env)
    legacy_stepdown = get_setting("ARGON_MEDIUM_BELOW_PCT", "0", file_env)
    gemini_stepdown_below_pct = float(
        get_setting("GEMINI_STEPDOWN_BELOW_PCT", legacy_stepdown, file_env)
    )
    gemini_stepdown_model = get_setting(
        "GEMINI_STEPDOWN_MODEL", "gemini-4-hydrogen-medium", file_env
    )
    legacy_reserve = get_setting("ARGON_RESERVE_PCT", "20", file_env)
    gemini_reserve_pct = float(get_setting("GEMINI_RESERVE_PCT", legacy_reserve, file_env))
    fallback_min_pct = float(get_setting("FALLBACK_MIN_PCT", "5", file_env))
    fallback_models_str = get_setting(
        "FALLBACK_MODELS",
        "claude-opus-5-5-high,claude-sonnet-5-5-high,gemini-4-hydrogen-medium,gemini-3.8-flash-medium",
        file_env,
    )
    fallback_models = [m.strip() for m in fallback_models_str.split(",") if m.strip()]
    excluded = {m.strip() for m in args.exclude_models.split(",") if m.strip()}

    try:
        agy_bin = find_agy_binary()
        usage_payload = fetch_usage_json(agy_bin)
        groups = parse_groups(usage_payload)
        decision = select_model(
            groups=groups,
            primary_model=primary_model,
            gemini_stepdown_below_pct=gemini_stepdown_below_pct,
            gemini_stepdown_model=gemini_stepdown_model,
            gemini_reserve_pct=gemini_reserve_pct,
            fallback_models=fallback_models,
            fallback_min_pct=fallback_min_pct,
            excluded=excluded,
        )
    except Exception as exc:
        if args.select_model:
            non_excluded_fallbacks = [m for m in fallback_models if m not in excluded]
            fallback_choice = (
                non_excluded_fallbacks[0]
                if (primary_model in excluded and non_excluded_fallbacks)
                else primary_model
            )
            print(fallback_choice)
            return 0
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.select_model:
        print(decision["selected_model"])
        return 0

    now_str = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    report = {
        "checked_at": now_str,
        "config": {
            "primary_model": primary_model,
            "gemini_stepdown_below_pct": gemini_stepdown_below_pct,
            "gemini_stepdown_model": gemini_stepdown_model,
            "gemini_reserve_pct": gemini_reserve_pct,
            "fallback_models": fallback_models,
            "fallback_min_pct": fallback_min_pct,
        },
        "decision": decision,
        "groups": groups,
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    ordered_group_names = list(groups.keys())

    if args.markdown:
        print(f"### Antigravity Quota Snapshot ({now_str})")
        print("| Quota Group | Bucket | Window | Remaining | Resets At (Local) | In |")
        print("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for g_name in ordered_group_names:
            g = groups[g_name]
            for b in g["buckets"]:
                if b.get("disabled"):
                    rem_col = "*disabled*"
                    reset_col = "—"
                    in_col = "—"
                else:
                    rem_col = f"**{b['remaining_pct']:.1f}%**"
                    reset_col = b["reset_time_local"]
                    in_col = b["reset_in"]
                print(
                    f"| **{g_name}** | {b['name']} (`{b['id']}`) | `{b['window']}` | "
                    f"{rem_col} | {reset_col} | {in_col} |"
                )
        print(
            f"\n- **Active Model Selection:** `{decision['selected_model']}` "
            f"({decision['reason']})"
        )
        return 0

    # Default human-readable output
    print("=" * 78)
    print(f" Antigravity CLI Quota & Model Router Status ({now_str})")
    print("=" * 78)
    print(f"{'Quota Group':24s} {'Bucket':28s} {'Rem %':>8s}  {'Resets At (Local)'}")
    print("-" * 78)
    for g_name in ordered_group_names:
        g = groups[g_name]
        for b in g["buckets"]:
            if b.get("disabled"):
                print(f"{g_name:24s} {b['name']:28s} {'disabled':>8s}  —")
            else:
                print(
                    f"{g_name:24s} {b['name']:28s} {b['remaining_pct']:7.1f}%  "
                    f"{b['reset_time_local']} ({b['reset_in']})"
                )
    print("-" * 78)
    print(f" Primary Model:      {primary_model} (Gemini Reserve: {gemini_reserve_pct:.0f}%)")
    if gemini_stepdown_below_pct > 0:
        print(
            f" Gemini Step-Down:   < {gemini_stepdown_below_pct:.0f}% -> {gemini_stepdown_model}"
        )
    print(f" Fallback Chain:     {', '.join(fallback_models)} (Min: {fallback_min_pct:.0f}%)")
    print(f" Selected Model:     {decision['selected_model']} [{decision['tier_status']}]")
    print(f" Routing Reason:     {decision['reason']}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

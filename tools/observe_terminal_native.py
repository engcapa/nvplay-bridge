"""Read-only, content-free status of a native campaign and its bridge monitors."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import time
import urllib.request

from checkpoint_terminal_native import event_summary
from eval_terminal_native import process_identity
from monitor_bridge import LogTail, snapshot
from report_terminal_native import summarize


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def observe(base: Path, checkpoint: Path | None, health_url: str, lookback: float) -> dict:
    report = summarize(base)
    records = LogTail(base / "bridge-debug.jsonl").read()
    now = time.time()
    bridge = snapshot(records, now=now, since=now - lookback)
    preparation = [row for row in records if row.get("event") == "browser_prepare_retry"
                   and row.get("ts", 0) >= now - lookback]
    campaign = read_json(base / "campaign-state.json")
    resource = read_json(base / "resource-monitor-state.json")
    result = {
        "observed_at": datetime.fromtimestamp(now).astimezone().isoformat(),
        "completed": len(report["attempts"]), "totals": report["totals"],
        "results": [{key: row[key] for key in ("task", "stage", "effort", "repeat", "artifact_score",
                     "end_to_end_success", "agent_seconds", "bridge_errors")}
                    for row in report["attempts"]],
        "pending": report["pending"],
        "active": [{key: row[key] for key in ("id", "reasoning_effort", "phase", "attempt",
                    "retry_count", "age_seconds", "activity_age_seconds", "counts") if key in row}
                   for row in bridge["active"]],
        "recent_errors": [event_summary(row) for row in bridge["recent_errors"]],
        "upstream_errors_in_lookback": bridge["events"].get("browser_http_error", 0),
        "retries_in_lookback": bridge["events"].get("upstream_retry", 0),
        "preparation_retries_in_lookback": len(preparation),
        "recent_preparation_retries": [{**event_summary(row), "tab_id": row.get("tab_id")}
                                        for row in preparation[-10:]],
        "lookback_seconds": lookback,
        "campaign": {key: campaign[key] for key in ("status", "task", "repeat") if key in campaign},
        "campaign_error_present": (base / "campaign-error.json").exists(),
    }
    transition = read_json(base / "transport-transition-state.json")
    if transition:
        result["transport_transition"] = {key: transition[key] for key in (
            "status", "at", "current_agent_signalled") if key in transition}
    if resource:
        result["resources"] = {key: resource[key] for key in ("disk_free_bytes", "memory_rss_bytes",
            "memory_file_cache_bytes", "memory_limit_bytes", "memory_events", "memory_allocation_failures")
                               if key in resource}
        result["resource_sample_age_seconds"] = round(now - resource["sampled_at"], 1)
    if checkpoint and checkpoint.is_file():
        result["checkpoint_age_seconds"] = round(now - read_json(checkpoint)["checkpoint_at"], 1)
    sync = read_json(base / "checkpoint-git-sync.json")
    if sync:
        result["checkpoint_git_sync"] = {key: sync[key] for key in (
            "attempted_at", "commit", "pushed", "returncode", "error_type") if key in sync}
    processes = {}
    for filename in ("campaign-state.json", "bridge-runtime.json", "brave-process.json",
                     "checkpoint-process.json", "bridge-monitor-process.json",
                     "cdp-observer-process.json", "resource-monitor-process.json",
                     "browser-supervisor-process.json",
                     "transport-transition-process.json"):
        recorded = read_json(base / filename)
        pid = recorded.get("pid")
        actual = process_identity(pid) if isinstance(pid, int) and pid > 0 else None
        expected = {key: recorded[key] for key in ("pid", "boot_id", "start_ticks") if key in recorded}
        if filename == "campaign-state.json" and recorded.get("controller_identity"):
            expected = recorded["controller_identity"]
        # Older controllers put their boot/start identity in each attempt owner.
        if filename == "campaign-state.json" and len(expected) < 3:
            owners = [read_json(base / row["attempt"] / "owner.json") for row in report["pending"]]
            expected = next((owner for owner in owners if owner.get("pid") == pid), expected)
        identity_available = len(expected) == 3
        processes[filename] = {"pid": pid, "pid_alive": actual is not None,
                               "identity_matches": actual == expected if identity_available else None}
    result["processes"] = processes
    try:
        http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with http.open(health_url, timeout=5) as response:
            health = json.load(response)
        result["health"] = {key: health[key] for key in ("status", "model", "browser_output_tokens",
                                                       "browser_timeouts") if key in health}
        result["health"]["pool_states"] = dict(Counter(health.get("browser_pool", {}).get("tabs", {}).values()))
    except Exception as error:
        result["health"] = {"status": "unreachable", "error_type": type(error).__name__}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--health-url", default="http://127.0.0.1:8083/health")
    parser.add_argument("--lookback", type=float, default=300)
    args = parser.parse_args()
    if args.lookback <= 0:
        parser.error("--lookback must be positive")
    print(json.dumps(observe(args.base.resolve(strict=True), args.checkpoint, args.health_url, args.lookback),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()

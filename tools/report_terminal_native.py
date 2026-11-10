"""Summarize native evaluation evidence without publishing benchmark contents."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time

from native_eval_support import write_json


def usage_totals(requests) -> dict:
    totals = Counter()
    for request in requests:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[key] += request.get(key, 0)
        totals["cached_prompt_tokens"] += request.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        totals["reasoning_tokens"] += request.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
    return dict(totals)


def transport_evidence(records: dict[str, list[dict]] | None, request_ids: list[str] | None) -> dict | None:
    """Count failed HTTP attempts without mistaking cumulative request age for attempt time."""
    if records is None or request_ids is None:
        return None
    failures, durations, completed = Counter(), [], []
    usages, partial_usage, missing_usage = [], 0, 0
    output_limits = set()
    for request_id in request_ids:
        starts = {}
        latest_usage, final_usage = None, False
        for event in records.get(request_id, []):
            kind = event.get("event")
            attempt = event.get("attempt")
            if event.get("usage"):
                latest_usage = event["usage"]
            if kind in ("browser_done", "browser_error", "browser_cancelled"):
                final_usage = kind == "browser_done" and bool(event.get("usage"))
            if kind == "browser_start":
                starts[attempt] = event["ts"]
                if isinstance(event.get("max_tokens"), int):
                    output_limits.add(event["max_tokens"])
            elif kind == "browser_http_error":
                failures[str(event["status"])] += 1
                if attempt in starts and event["ts"] >= starts[attempt]:
                    durations.append(event["ts"] - starts[attempt])
            elif kind == "browser_done" and isinstance(event.get("sec"), (int, float)):
                completed.append(event["sec"])
        if latest_usage:
            usages.append(latest_usage)
            partial_usage += not final_usage
        else:
            missing_usage += 1
    return {"max_output_tokens": sorted(output_limits),
            "http_status_counts": dict(failures),
            "timed_http_failures": len(durations),
            "http_failed_attempt_seconds": round(sum(durations), 3),
            "completed_requests": len(completed),
            "completed_request_seconds": round(sum(completed), 3),
            "longest_completed_request_seconds": max(completed, default=None),
            "observed_usage": usage_totals(usages),
            "requests_with_partial_usage": partial_usage,
            "requests_without_observed_usage": missing_usage}


def summarize(base: Path) -> dict:
    groups, pending, rows = {}, [], []
    records = None
    log = base / "bridge-debug.jsonl"
    if log.is_file():
        records = {}
        for line in log.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("id"):
                records.setdefault(event["id"], []).append(event)
    for directory in sorted((base / "runs").glob("*/*")):
        result = directory / "result.json"
        if not result.is_file():
            status = directory / "status.json"
            progress = {"attempt": str(directory.relative_to(base)),
                        **(json.loads(status.read_text()) if status.is_file() else {})}
            if progress.get("started_at"):
                progress["elapsed_seconds"] = round(time.time() - progress["started_at"], 1)
            log = directory / "cc.jsonl"
            calls = []
            if log.is_file():
                for line in log.read_text(errors="replace").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "assistant":
                        calls.extend(block.get("name") for block in event.get("message", {}).get("content", [])
                                     if block.get("type") == "tool_use")
            progress.update(tool_calls=len(calls), last_tool=calls[-1] if calls else None)
            pending.append(progress)
            continue
        data = json.loads(result.read_text())
        usage = usage_totals(data["bridge"]["usage_by_request"].values())
        infra = data.get("infrastructure_error") or data["grade"]["infrastructure_error"]
        retried_ids = {event["id"] for event in data["bridge"]["retries"]}
        failed_ids = {event["id"] for event in [*data["bridge"]["errors"], *data["bridge"]["cancelled"]]}
        failed_ids.update(data["bridge"]["unfinished"])
        row = {"task": data["task"], "stage": data["stage"], "effort": data["effort"],
               "repeat": data["repeat"], "budget_seconds": data["budget_seconds"],
               "artifact_score": data["grade"]["score"], "infrastructure_error": infra,
               "end_to_end_success": data["end_to_end_success"],
               "agent_seconds": data["agent"]["duration_seconds"],
               "agent_timed_out": data["agent"]["timed_out"],
               "cc_subtype": (data["cc"] or {}).get("subtype"),
               "cc_turns": (data["cc"] or {}).get("num_turns"),
               "logical_requests": data["bridge"]["logical_requests"],
               "upstream_attempts": data["bridge"]["upstream_attempts"],
               "bridge_errors": len(data["bridge"]["errors"]),
               "bridge_errors_after_content": sum(event.get("committed") is True
                                                   for event in data["bridge"]["errors"]),
               "bridge_errors_before_content": sum(event.get("committed") is False
                                                    for event in data["bridge"]["errors"]),
               "bridge_retries": len(data["bridge"]["retries"]),
               "http_failures_retried": sum(isinstance(event.get("status"), int) and event["status"] >= 400
                                            for event in data["bridge"]["retries"]),
               "requests_recovered_after_retry": len(retried_ids - failed_ids),
               "bridge_cancelled": len(data["bridge"]["cancelled"]),
               "bridge_unfinished": data["bridge"]["unfinished"],
               "transport": transport_evidence(records, data["bridge"].get("request_ids")),
               "usage": dict(usage), "source_head": data["source_head"]}
        rows.append(row)
        key = f'{row["stage"]}/{row["budget_seconds"]}/{row["effort"]}'
        groups.setdefault(key, []).append(row)
    totals = {}
    for key, attempts in groups.items():
        valid = [row for row in attempts if not row["infrastructure_error"]]
        n = len(valid)
        transport = [row["transport"] for row in attempts if row["transport"] is not None]
        # Keep every operational attempt in the end-to-end denominator.
        totals[key] = {"attempts": len(attempts), "valid_grades": n,
                       "infrastructure_errors": len(attempts) - n,
                       "artifact_passes": sum(row["artifact_score"] == 1 for row in valid),
                       "artifact_pass_at_1": sum(row["artifact_score"] == 1 for row in valid) / n if n else None,
                       "end_to_end_passes": sum(row["end_to_end_success"] for row in attempts),
                       "end_to_end_pass_rate": sum(row["end_to_end_success"] for row in attempts) / len(attempts),
                       "agent_seconds": round(sum(row["agent_seconds"] for row in attempts), 3),
                       "logical_requests": sum(row["logical_requests"] for row in attempts),
                       "bridge_errors": sum(row["bridge_errors"] for row in attempts),
                       "bridge_errors_after_content": sum(row["bridge_errors_after_content"] for row in attempts),
                       "bridge_errors_before_content": sum(row["bridge_errors_before_content"] for row in attempts),
                       "bridge_retries": sum(row["bridge_retries"] for row in attempts),
                       "http_failures_retried": sum(row["http_failures_retried"] for row in attempts),
                       "requests_recovered_after_retry": sum(row["requests_recovered_after_retry"] for row in attempts),
                       "cancelled_requests": sum(row["bridge_cancelled"] for row in attempts),
                       "transport": {
                           "attempts_with_evidence": len(transport),
                           "max_output_tokens": sorted({limit for item in transport
                                                        for limit in item["max_output_tokens"]}),
                           "http_status_counts": dict(sum((Counter(item["http_status_counts"])
                                                            for item in transport), Counter())),
                           "timed_http_failures": sum(item["timed_http_failures"] for item in transport),
                           "http_failed_attempt_seconds": round(sum(item["http_failed_attempt_seconds"]
                                                                     for item in transport), 3),
                           "completed_requests": sum(item["completed_requests"] for item in transport),
                           "observed_usage": dict(sum((Counter(item["observed_usage"])
                                                       for item in transport), Counter())),
                           "requests_with_partial_usage": sum(item["requests_with_partial_usage"] for item in transport),
                           "requests_without_observed_usage": sum(item["requests_without_observed_usage"] for item in transport),
                           "longest_completed_request_seconds": max(
                               (item["longest_completed_request_seconds"] for item in transport
                                if item["longest_completed_request_seconds"] is not None), default=None)},
                       "usage": dict(sum((Counter(row["usage"]) for row in attempts), Counter()))}
    return {"generated_at": time.time(), "label": "CC + Kimi + bridge native adaptation; not AA Intelligence Index",
            "totals": totals, "attempts": rows, "pending": pending}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = summarize(args.base)
    if args.output:
        write_json(args.output, result)
    print(json.dumps({"totals": result["totals"], "pending": result["pending"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

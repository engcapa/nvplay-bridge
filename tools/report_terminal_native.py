"""Summarize native evaluation evidence without publishing benchmark contents."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time

from native_eval_support import write_json


def summarize(base: Path) -> dict:
    groups, pending, rows = {}, [], []
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
        usage = Counter()
        for request in data["bridge"]["usage_by_request"].values():
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage[key] += request.get(key, 0)
            usage["cached_prompt_tokens"] += request.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            usage["reasoning_tokens"] += request.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
        infra = data.get("infrastructure_error") or data["grade"]["infrastructure_error"]
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
               "bridge_retries": len(data["bridge"]["retries"]),
               "bridge_cancelled": len(data["bridge"]["cancelled"]),
               "bridge_unfinished": data["bridge"]["unfinished"],
               "usage": dict(usage), "source_head": data["source_head"]}
        rows.append(row)
        key = f'{row["stage"]}/{row["budget_seconds"]}/{row["effort"]}'
        groups.setdefault(key, []).append(row)
    totals = {}
    for key, attempts in groups.items():
        valid = [row for row in attempts if not row["infrastructure_error"]]
        n = len(valid)
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
                       "bridge_retries": sum(row["bridge_retries"] for row in attempts),
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

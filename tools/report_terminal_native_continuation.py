"""Combine hash-linked checkpoints without replacing any completed position."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import time

from native_eval_support import write_json


REPO = Path(__file__).resolve().parents[1]
ROW_KEYS = ("task", "stage", "effort", "repeat", "artifact_score", "end_to_end_success",
            "agent_seconds", "agent_timed_out", "logical_requests", "upstream_attempts",
            "bridge_errors", "bridge_retries", "bridge_cancelled", "usage", "source_head")


def position(row):
    return row["task"], row["repeat"], row["effort"]


def counts(rows):
    valid = [row for row in rows if row["artifact_score"] is not None
             and not row["infrastructure_error_present"]]
    return {"completed_positions": len(rows), "valid_grades": len(valid),
            "artifact_passes": sum(row["artifact_score"] == 1 for row in valid),
            "end_to_end_passes": sum(row["end_to_end_success"] is True for row in rows),
            "infrastructure_errors": sum(row["infrastructure_error_present"] for row in rows)}


def combine(checkpoint: Path, repo: Path = REPO) -> dict:
    sources, visited, documents = [], set(), {}

    def visit(path, expected_hash=None):
        path = path.resolve(strict=True)
        if path in visited:
            raise ValueError("checkpoint inheritance contains a cycle")
        visited.add(path)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if expected_hash is not None and digest != expected_hash:
            raise ValueError(f"checkpoint hash mismatch: {path.name}")
        data = json.loads(raw)
        documents[path] = data
        continuation = data.get("protocol", {}).get("continuation", {})
        inherited = []
        if continuation:
            source = Path(continuation["source_checkpoint"])
            if not source.is_absolute():
                source = repo / source
            if not source.is_file():
                source = path.parent / source.name
            inherited = visit(source, continuation["source_checkpoint_sha256"])
            expected = continuation["preserved_positions"]
            if (len(inherited) != continuation["preserved_completed_attempts"]
                    or len(expected) != len(inherited)
                    or {position(row) for row in expected} != {position(row) for row in inherited}):
                raise ValueError(f"inherited positions differ from the frozen plan: {path.name}")
        local = data["results"]["attempts"]
        if data.get("new_formal_attempts_completed", len(local)) != len(local):
            raise ValueError(f"local completed count mismatch: {path.name}")
        rows = inherited + [{**{key: row[key] for key in ROW_KEYS if key in row},
                              "infrastructure_error_present": bool(row.get("infrastructure_error")),
                              "source_checkpoint": path.name} for row in local]
        if len({position(row) for row in rows}) != len(rows):
            raise ValueError("duplicate completed position; reruns must remain separate evidence")
        if data.get("combined_plan_positions_completed", len(rows)) != len(rows):
            raise ValueError(f"combined completed count mismatch: {path.name}")
        sources.append({"checkpoint": path.name, "sha256": digest,
                        "local_completed_positions": len(local), "combined_completed_positions": len(rows)})
        return rows

    rows = visit(checkpoint)
    root = documents[checkpoint.resolve()]
    protocol = root.get("protocol", {})
    continuation = protocol.get("continuation", {})
    planned = continuation.get("total_plan_positions", root["new_formal_attempts_planned"])
    formal = protocol.get("formal_native_subset", {})
    if "schedule" in formal:
        schedule = (continuation.get("preserved_positions", []) + formal["schedule"]
                    + formal.get("preserved_local_schedule", []))
        expected = {position(row) for row in schedule}
        if len(expected) != len(schedule) or len(expected) != planned:
            raise ValueError("frozen schedule does not describe every position exactly once")
        if not {position(row) for row in rows} <= expected:
            raise ValueError("completed position is outside the frozen schedule")
    if len(rows) > planned:
        raise ValueError("more completed positions than planned")
    groups = defaultdict(list)
    for row in rows:
        groups[f'{row["stage"]}/{row["effort"]}'].append(row)
    return {"generated_at": time.time(), "planned_positions": planned,
            "remaining_positions": planned - len(rows), "totals": counts(rows),
            "totals_by_stage_and_effort": {key: counts(value) for key, value in sorted(groups.items())},
            "checkpoint_chain": sources, "attempts": rows,
            "scope": "Completed positions from the original hash-linked checkpoints; no best-rerun selection. "
                     "Pending and administrative interruptions receive no grade. Stages retain their original "
                     "client, bridge and context policies; combined counts are descriptive."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = combine(args.checkpoint.resolve())
    write_json(args.output.resolve(), result)
    print(json.dumps({"output": str(args.output), "remaining_positions": result["remaining_positions"],
                      **result["totals"]}))


if __name__ == "__main__":
    main()

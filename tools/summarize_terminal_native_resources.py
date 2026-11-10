"""Summarize a stable prefix of the campaign's shared-container resource samples."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

from native_eval_support import write_json


def summarize(source: Path, prefix_bytes: int | None = None) -> dict:
    with source.open("rb") as stream:
        content = stream.read(prefix_bytes if prefix_bytes is not None else -1)
    # A concurrent append may leave the final JSON record incomplete.
    content = content[:content.rfind(b"\n") + 1]
    samples = [json.loads(line) for line in content.splitlines() if line.strip()]
    if not samples:
        raise ValueError("resource log has no complete samples")
    versions = {sample["cgroup_version"] for sample in samples}
    limits = {sample["memory_limit_bytes"] for sample in samples}
    if len(versions) != 1 or len(limits) != 1:
        raise ValueError("resource environment changed; summarize each environment separately")
    if any(right["sampled_at"] < left["sampled_at"] for left, right in zip(samples, samples[1:])):
        raise ValueError("resource samples are not chronological")
    first, last = samples[0], samples[-1]
    result = {
        "summarized_at": time.time(),
        "scope": "Shared container resource samples; not per-attempt accounting; coverage limited to recorded samples",
        "sample_count": len(samples),
        "first_sample_at": first["sampled_at"], "last_sample_at": last["sampled_at"],
        "source_prefix_sha256": hashlib.sha256(content).hexdigest(),
        "source_prefix_bytes": len(content),
        "cgroup_version": versions.pop(), "memory_limit_bytes": limits.pop(),
        "min_disk_free_bytes": min(sample["disk_free_bytes"] for sample in samples),
        "peak_memory_usage_bytes": max(sample["memory_usage_bytes"] for sample in samples),
        "peak_memory_rss_bytes": max(sample["memory_rss_bytes"] for sample in samples),
        "peak_file_cache_bytes": max(sample["memory_file_cache_bytes"] for sample in samples),
        "oom_events_start": first["memory_events"], "oom_events_end": last["memory_events"],
    }
    counter_paths = [("cpu_stat", "nr_throttled")]
    if result["cgroup_version"] == 1:
        result["memory_allocation_failures_start"] = first["memory_allocation_failures"]
        result["memory_allocation_failures_end"] = last["memory_allocation_failures"]
        counter_paths += [("cpu_stat", "throttled_time"), ("memory_allocation_failures",)]
        time_field, divisor = "throttled_time", 1e9
        counter_paths.append(("memory_events", "oom_kill"))
    else:
        time_field, divisor = "throttled_usec", 1e6
        counter_paths += [("cpu_stat", time_field)]
        counter_paths += [("memory_events", key) for key in ("oom", "oom_kill", "max")]
    for path in counter_paths:
        values = []
        for sample in samples:
            value = sample
            for key in path:
                value = value[key]
            values.append(value)
        if any(right < left for left, right in zip(values, values[1:])):
            raise ValueError(f"counter reset at {'.'.join(path)}; summarize each counter lifetime separately")
    result["cpu_throttled_periods_delta"] = last["cpu_stat"]["nr_throttled"] - first["cpu_stat"]["nr_throttled"]
    result["cpu_throttled_seconds_delta"] = (last["cpu_stat"][time_field] - first["cpu_stat"][time_field]) / divisor
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prefix-bytes", type=int)
    args = parser.parse_args()
    if args.prefix_bytes is not None and args.prefix_bytes <= 0:
        parser.error("--prefix-bytes must be positive")
    result = summarize(args.base / "resource-monitor.jsonl", args.prefix_bytes)
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

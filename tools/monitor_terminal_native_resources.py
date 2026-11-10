"""Record host resource pressure beside a native campaign without touching agents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import time

from native_eval_support import write_json


def integers(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    return {key: int(value) for key, value in
            (line.split() for line in path.read_text().splitlines())}


def collect(base: Path) -> dict:
    usage = shutil.disk_usage(base)
    result = {"sampled_at": time.time(), "disk_total_bytes": usage.total,
              "disk_free_bytes": usage.free, "scope": "shared container; not per-attempt accounting"}
    cgroup = Path("/sys/fs/cgroup")
    if (cgroup / "memory.current").is_file():
        result["cgroup_version"] = 2
        memory = integers(cgroup / "memory.stat")
        result["memory_events"] = integers(cgroup / "memory.events")
        for field, filename in (("memory_usage_bytes", "memory.current"),
                                ("memory_limit_bytes", "memory.max")):
            value = (cgroup / filename).read_text().strip()
            result[field] = int(value) if value != "max" else None
        result["memory_rss_bytes"] = memory.get("anon", 0)
        result["memory_file_cache_bytes"] = memory.get("file", 0)
        result["memory_inactive_file_bytes"] = memory.get("inactive_file", 0)
        result["cpu_stat"] = integers(cgroup / "cpu.stat")
    elif (cgroup / "memory/memory.usage_in_bytes").is_file():
        result["cgroup_version"] = 1
        memory = integers(cgroup / "memory/memory.stat")
        for field, filename in (("memory_usage_bytes", "memory.usage_in_bytes"),
                                ("memory_limit_bytes", "memory.limit_in_bytes"),
                                ("memory_allocation_failures", "memory.failcnt")):
            result[field] = int((cgroup / "memory" / filename).read_text())
        result["memory_rss_bytes"] = memory.get("total_rss", memory.get("rss", 0))
        result["memory_file_cache_bytes"] = memory.get("total_cache", memory.get("cache", 0))
        result["memory_inactive_file_bytes"] = memory.get("total_inactive_file", 0)
        result["memory_events"] = integers(cgroup / "memory/memory.oom_control")
        result["cpu_stat"] = integers(cgroup / "cpu/cpu.stat")
    state = base / "campaign-state.json"
    if state.is_file():
        data = json.loads(state.read_text())
        result["campaign"] = {key: data[key] for key in ("status", "task", "repeat") if key in data}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    base = args.base.resolve(strict=True)
    while True:
        sample = collect(base)
        with (base / "resource-monitor.jsonl").open("a") as stream:
            stream.write(json.dumps(sample, sort_keys=True) + "\n")
        write_json(base / "resource-monitor-state.json", sample)
        if args.once or sample.get("campaign", {}).get("status") == "formal_complete":
            print(json.dumps(sample, sort_keys=True), flush=True)
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

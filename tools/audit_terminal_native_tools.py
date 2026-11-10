"""Scan completed CC tool inputs for retrieval candidates without exporting their text."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import time

from native_eval_support import write_json


PATTERNS = {
    "hidden_or_host_path": r"(?:/tests(?:/|\b)|/oracle(?:/|\b)|/solution(?:/|\b)|/home/zhyhang(?:/|\b)|terminal-bench|solution\.sh)",
    "external_retrieval": r"(?:https?://|\b(?:curl|wget|WebFetch|WebSearch)\b|\b(?:requests|httpx)\.(?:get|post|request)\s*\(|urlopen\s*\()",
}


def audit(base: Path) -> dict:
    rows = []
    for result in sorted((base / "runs").glob("*/*/result.json")):
        source = result.parent / "cc.jsonl"
        content = source.read_bytes()
        counts, candidates, invalid = Counter(), [], 0
        for line in content.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                invalid += 1
                continue
            if event.get("type") != "assistant":
                continue
            for block in event.get("message", {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                name = block["name"]
                counts[name] += 1
                encoded = json.dumps(block.get("input", {}), ensure_ascii=False, sort_keys=True)
                categories = [category for category, pattern in PATTERNS.items()
                              if re.search(pattern, encoded, re.IGNORECASE)]
                if categories:
                    candidates.append({"tool_ordinal": counts.total(), "tool": name,
                                       "categories": categories,
                                       "input_sha256": hashlib.sha256(encoded.encode()).hexdigest()})
        rows.append({"attempt": result.parent.name, "tool_counts": dict(counts),
                     "candidate_count": len(candidates), "candidates": candidates,
                     "unparsed_json_lines": invalid,
                     "source_sha256": hashlib.sha256(content).hexdigest()})
    return {"audited_at": time.time(),
            "scope": "Candidate scan of explicit tool inputs; not proof against dynamic behavior; candidates require manual review",
            "patterns": PATTERNS, "attempts": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.base)
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

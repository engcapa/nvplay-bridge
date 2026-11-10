"""Export content-free artifact and verifier checks for completed native attempts."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time
import tomllib

from native_eval_support import file_hashes, write_json


READ_ONLY = {"session-window-debug": ("app/app/types.py", "app/app/DESIGN.md", "app/app/__init__.py")}


def client_terminal(directory: Path, data: dict) -> dict:
    final = {}
    for line in (directory / "cc.jsonl").read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            final = event
    return {"exit_code": data["agent"]["exit_code"], "timed_out": data["agent"]["timed_out"],
            **{key: final.get(key) for key in ("subtype", "is_error", "terminal_reason", "api_error_status")},
            "prompt_too_long": final.get("is_error") is True and str(final.get("result") or "").strip() == "Prompt is too long",
            "reported_context_window_tokens": sorted({model["contextWindow"]
                for model in (final.get("modelUsage") or {}).values() if "contextWindow" in model})}


def review(base: Path, controls: bool = False) -> dict:
    rows = []
    results = sorted((base / "runs").glob("*/*/result.json"))
    if controls:
        results += sorted((base / "controls").glob("*/*/grade.json"))
    for path in results:
        data = json.loads(path.read_text())
        directory = path.parent
        control = path.name == "grade.json"
        task = directory.parent.name if control else data["task"]
        grade = data if control else data["grade"]
        artifacts = json.loads((directory / "artifacts.json").read_text())
        actual = artifacts["hashes"]
        task_config = tomllib.loads((base / "terminal-bench/tasks" / task / "task.toml").read_text())
        prefixes = [item.lstrip("/").rstrip("/") for item in task_config["artifacts"]]
        template = base / "templates" / task / "agent"
        original = {"app/" + relative: digest for relative, digest in file_hashes(template / "app").items()
                    if any("app/" + relative == prefix or ("app/" + relative).startswith(prefix + "/")
                           for prefix in prefixes)}
        changed = sorted(name for name in original.keys() & actual.keys() if original[name] != actual[name])
        added, removed = sorted(actual.keys() - original.keys()), sorted(original.keys() - actual.keys())
        readonly = {name: actual.get(name) == original[name] for name in READ_ONLY.get(task, ())}
        ctrf = directory / "verifier-results/ctrf.json"
        verifier = None
        if ctrf.is_file():
            content = ctrf.read_bytes()
            tests = json.loads(content).get("results", {}).get("tests", [])
            statuses = dict(Counter(test.get("status", "unknown") for test in tests))
            verifier = {"test_count": len(tests), "statuses": statuses,
                        "sha256": hashlib.sha256(content).hexdigest()}
        rows.append({"attempt": str(directory.relative_to(base)), "task": task,
                     "control": directory.name if control else None,
                     "effort": data.get("effort"), "repeat": data.get("repeat"),
                     "artifact_score": grade["score"], "infrastructure_error": grade["infrastructure_error"],
                     "changed_original_files": changed, "added_files": added, "removed_original_files": removed,
                     "declared_read_only_files_unchanged": readonly,
                     "read_only_check_scope": "explicit session-window task constraints" if readonly else "not checked",
                     "artifact_errors_present": bool(artifacts["errors"]), "verifier": verifier})
        if not control:
            rows[-1]["client_terminal"] = client_terminal(directory, data)
    return {"reviewed_at": time.time(), "attempts": rows,
            "scope": "saved copied-artifact hashes and original verifier results; not a semantic code review"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--controls", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("use sudo to read the original root-only verifier evidence")
    result = review(args.base.resolve(), args.controls)
    if args.output:
        write_json(args.output.resolve(), result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

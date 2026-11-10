"""Audit agent inputs and verifier separation without publishing benchmark files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

from eval_terminal_native import Evaluation, RECIPES
from native_eval_support import file_hashes, write_json


def audit(base: Path) -> dict:
    evaluation = Evaluation(base)
    results = []
    for name in evaluation.plan["pilot_tasks"]:
        evaluation.verify_source(name)
        template = base / "templates" / name
        agent, verifier = template / "agent", template / "verifier"
        expected = {}
        for source, destination in RECIPES[name]["copies"].items():
            original = evaluation.dataset / name / "environment" / source
            if original.is_dir():
                expected.update({str(Path(destination) / relative): digest for relative, digest in file_hashes(original).items()})
            else:
                expected[destination] = hashlib.sha256(original.read_bytes()).hexdigest()
        actual = {str(Path("app") / relative): digest for relative, digest in file_hashes(agent / "app").items()}
        no_hidden_roots = all(not (agent / directory).exists() for directory in ("tests", "solution", "terminal-bench"))
        empty_home = not (agent / "home/agent/.claude").exists()
        result = {"task": name, "input_files": len(actual), "original_input_files_match": actual == expected,
                  "hidden_roots_absent_from_agent": no_hidden_roots, "fresh_cc_home": empty_home,
                  "verifier_has_tests": (verifier / "tests/test.sh").is_file(),
                  "agent_and_verifier_distinct_directories": agent.stat().st_ino != verifier.stat().st_ino,
                  "agent_proc_empty": not any((agent / "proc").iterdir()),
                  "input_manifest_sha256": hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()}
        result["passed"] = all(result[key] for key in (
            "original_input_files_match", "hidden_roots_absent_from_agent", "fresh_cc_home",
            "verifier_has_tests", "agent_and_verifier_distinct_directories", "agent_proc_empty"))
        results.append(result)
    return {"audited_at": time.time(), "passed": all(row["passed"] for row in results), "tasks": results,
            "scope": "prepared templates and pinned task inputs; runtime UID allocation is recorded per attempt"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    base = args.base.resolve()
    result = audit(base)
    write_json(args.output or base / "isolation-audit.json", result)
    print(json.dumps(result, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

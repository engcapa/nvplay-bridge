"""Persist public benchmark progress without prompts, answers, or credentials."""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import time
import urllib.request

from eval_terminal_native import process_identity
from monitor_bridge import LogTail, snapshot
from native_eval_support import write_json
from report_terminal_native import summarize


LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REPO = Path(__file__).resolve().parents[1]
SAFE_EVENT_KEYS = ("event", "ts", "id", "attempt", "reasoning_effort", "status", "sec", "committed",
                   "counts", "usage", "phase", "method", "error_type", "max_tokens")


def fingerprint(path: Path, base: Path) -> dict:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.relative_to(base)), "sha256": digest, "size": path.stat().st_size}


def event_summary(event: dict) -> dict:
    result = {key: event[key] for key in SAFE_EVENT_KEYS if key in event}
    if event.get("error"):
        result["error_sha256"] = hashlib.sha256(str(event["error"]).encode()).hexdigest()
    return result


def campaign_progress(base: Path) -> dict:
    state = base / "campaign-state.json"
    if not state.is_file():
        return {"controller_alive": False}
    data = json.loads(state.read_text())
    pid = data.get("pid")
    actual = process_identity(pid) if isinstance(pid, int) and pid > 0 else None
    expected = data.get("controller_identity")
    if expected is None and actual is not None:
        # Earlier controllers saved their boot/start identity on each attempt.
        for path in sorted((base / "runs").glob("*/*/owner.json")):
            owner = json.loads(path.read_text())
            if owner == actual:
                expected = owner
                break
    return {"controller_alive": actual is not None and actual == expected,
            **{key: data[key] for key in ("status", "task", "repeat") if key in data}}


def collect(base: Path, health_url: str) -> dict:
    now = time.time()
    result = {"schema_version": 2, "checkpoint_at": now,
              "run_directory": str(base), "raw_logs_survive_cleanup": False,
              "historical_archive": "docs/evidence/cc-bridge-native-2026-11-10.json",
              "historical_completed_observations_preserved": 12,
              "historical_administrative_interruptions_excluded": 2,
              "external_proxy": "http://127.0.0.1:31128",
              "local_bridge_and_cdp_direct": True, "other_projects_in_scope": False}
    plan_path = base / "evaluation-plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        result["protocol"] = {key: plan[key] for key in (
            "source_commit", "source_archive_sha256", "cc_version", "model", "efforts", "pilot_tasks",
            "formal_repeats", "formal_task_budget_seconds", "formal_native_subset",
            "restart_relationship", "bridge_runtime_by_stage", "continuation", "protocol_history") if key in plan}
        result["task_manifest_sha256"] = {
            task: hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            for task, files in plan.get("task_identities", {}).items()}
    else:
        result["status"] = "restoring_environment"
    for filename, key in (("bridge-runtime.json", "bridge_runtime"),
                           ("native-base-manifest.json", "native_environment"),
                           ("download-manifest.json", "downloads"),
                           ("wheel-manifest.json", "wheels"),
                           ("host-resources.json", "host_resources"),
                           ("checkpoint-git-sync.json", "previous_checkpoint_git_sync"),
                           ("smoke-result.json", "smoke")):
        path = base / filename
        if path.exists():
            result[key] = json.loads(path.read_text())
    controls = {}
    for path in sorted((base / "controls").glob("*/result.json")):
        data = json.loads(path.read_text())
        controls[path.parent.name] = {"valid": data.get("valid"), **{
            mode: {key: data.get(mode, {}).get(key) for key in ("score", "infrastructure_error")}
            for mode in ("baseline", "oracle")}}
    result["controls"] = controls
    result["interrupted_attempts_excluded_from_grades"] = [
        json.loads(path.read_text()) for path in sorted((base / "runs-interrupted").glob("*/*/interruption.json"))]
    report = summarize(base)
    result["results"] = report
    protocol = result.get("protocol", {})
    formal = protocol.get("formal_native_subset", {})
    schedule = formal.get("schedule")
    # A transport fix may move the remaining slots to a new stage. Keep the
    # completed local slots in the total without scheduling them again.
    preserved = formal.get("preserved_local_schedule", [])
    expected = (len(schedule) + len(preserved) if schedule is not None else
                len(protocol.get("pilot_tasks", [])) * 2 * protocol.get("formal_repeats", 3))
    result["new_formal_attempts_planned"] = expected or 30
    result["new_formal_attempts_completed"] = len(report["attempts"])
    prior = protocol.get("continuation", {}).get("preserved_completed_attempts", 0)
    result["preserved_prior_attempts_completed"] = prior
    result["combined_plan_positions_completed"] = prior + len(report["attempts"])
    result["campaign"] = campaign_progress(base)
    pause_path = base / "pause-state.json"
    pause = json.loads(pause_path.read_text()) if pause_path.is_file() else {}
    if pause:
        result["administrative_pause"] = {key: pause[key] for key in (
            "status", "paused_at", "completed", "interrupted_attempts", "reason") if key in pause}
    if report["pending"]:
        result["status"] = "formal_running"
    elif report["attempts"] and len(report["attempts"]) == expected:
        result["status"] = "formal_complete"
    elif result["campaign"]["controller_alive"] and result["campaign"].get("status") == "formal_running":
        result["status"] = "formal_running"
    elif (report["attempts"] and pause.get("status") == "paused_for_reboot"
          and pause.get("completed") == len(report["attempts"])):
        result["status"] = "formal_paused"
    elif report["attempts"]:
        result["status"] = "formal_incomplete"
    elif controls:
        result["status"] = ("controls_validated" if len(controls) == len(result.get("protocol", {}).get("pilot_tasks", []))
                            and all(value["valid"] for value in controls.values()) else "validating_controls")
    else:
        result.setdefault("status", "preparing_environment")
    try:
        with LOCAL_HTTP.open(health_url, timeout=5) as response:
            data = json.load(response)
        result["bridge_health"] = {key: data[key] for key in (
            "status", "model", "upstream_kind", "history_reasoning", "browser_output_tokens", "browser_timeouts")
            if key in data}
        pool = data.get("browser_pool", {})
        result["bridge_health"]["pool_states"] = dict(Counter(pool.get("tabs", {}).values()))
        result["bridge_health"]["recovering_tabs"] = pool.get("recovering", [])
    except Exception as error:
        result["bridge_health"] = {"status": "unreachable", "error_type": type(error).__name__}
    activity = snapshot(LogTail(base / "bridge-debug.jsonl").read(), now=now, since=now - 300)
    result["bridge_activity"] = {"events_last_300s": activity["events"], "active": activity["active"],
                                  **{key: [event_summary(event) for event in activity[key]] for key in (
                                      "recent_errors", "recent_upstream_errors", "recent_retries")}}
    evidence = []
    patterns = ("*.json", "*-regression.log", "*validation.log", "controls/*/result.json",
                "runs/*/*/identity.json", "runs/*/*/request-protocol.json", "runs/*/*/artifacts.json",
                "runs/*/*/grade.json", "runs/*/*/result.json")
    for path in sorted({path for pattern in patterns for path in base.glob(pattern) if path.is_file()}):
        evidence.append(fingerprint(path, base))
    result["evidence_fingerprints"] = evidence
    archive_patterns = ("runs/*/*/*-root-archive.json", "controls/*/*/*-root-archive.json", "smoke/*/*-root-archive.json")
    result["compressed_root_evidence"] = [{"directory": str(path.parent.relative_to(base)), **json.loads(path.read_text())}
        for path in sorted({path for pattern in archive_patterns for path in base.glob(pattern)})]
    return result


def save(base: Path, output: Path, health_url: str, commit: bool, push: bool = False):
    if push and not commit:
        raise ValueError("pushing checkpoints requires committing them")
    with (base / "checkpoint.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = collect(base, health_url)
        write_json(output, data)
        if commit:
            repo = REPO
            relative = str(output.resolve().relative_to(repo))
            git = ["git"]
            if os.geteuid() == 0 and repo.stat().st_uid != 0:
                # Provisioning uses root; Git objects must remain writable by
                # the repository owner and use that owner's normal identity.
                git = ["runuser", "-u", pwd.getpwuid(repo.stat().st_uid).pw_name, "--", "git"]
            # Commit this file alone, even if an operator has unrelated staged edits.
            subprocess.run([*git, "add", "--", relative], cwd=repo, check=True)
            changed = subprocess.run([*git, "diff", "--cached", "--quiet", "--", relative], cwd=repo)
            if changed.returncode not in (0, 1):
                raise RuntimeError("git checkpoint diff failed")
            if changed.returncode:
                subprocess.run([*git, "commit", "--only", "-m",
                                f'Checkpoint CC bridge evaluation: {data["new_formal_attempts_completed"]}/{data["new_formal_attempts_planned"]}, {data["status"]}',
                                "--", relative], cwd=repo, check=True)
            if push:
                # A temporary push failure must not terminate a running CC task.
                # Every following checkpoint retries, even without a new commit.
                sync = {"attempted_at": time.time(), "commit": subprocess.check_output(
                    [*git, "rev-parse", "HEAD"], cwd=repo, text=True).strip()}
                with (base / "checkpoint-push.log").open("ab") as log:
                    try:
                        pushed = subprocess.run([*git, "-c", "http.proxy=http://127.0.0.1:31128",
                                                 "push", "--porcelain", "origin", "HEAD"], cwd=repo,
                                                stdout=log, stderr=subprocess.STDOUT, timeout=60)
                        sync.update(pushed=pushed.returncode == 0, returncode=pushed.returncode)
                    except subprocess.TimeoutExpired:
                        sync.update(pushed=False, error_type="TimeoutExpired")
                write_json(base / "checkpoint-git-sync.json", sync)
                print(json.dumps({"checkpoint_push": sync}), flush=True)
        print(json.dumps({"checkpoint": str(output), "status": data["status"],
                          "completed": data["new_formal_attempts_completed"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--health-url", default="http://127.0.0.1:8083/health")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true", help="Push after every commit; retry on the next checkpoint")
    parser.add_argument("--interval", type=float, default=0, help="Zero saves once; otherwise follow until interrupted")
    args = parser.parse_args()
    if args.interval < 0:
        parser.error("interval must be nonnegative")
    if args.push and not args.commit:
        parser.error("--push requires --commit")
    while True:
        save(args.base.resolve(), args.output.resolve(), args.health_url, args.commit, args.push)
        if not args.interval:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

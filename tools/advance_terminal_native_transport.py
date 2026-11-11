"""Wait for natural completion, validate a bridge rollout, then continue frozen slots."""
from __future__ import annotations

import argparse
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import time

from archive_terminal_native_roots import archive_root, completed_roots
from checkpoint_terminal_native import campaign_progress, collect, save
from eval_terminal_native import process_identity
from monitor_bridge import LogTail, snapshot
from native_eval_support import clone_root, write_json
from pause_terminal_native_services import identify, stop
from report_terminal_native import summarize
from review_terminal_native_results import review
from start_terminal_native_services import MODULES, await_json


REPO = Path(__file__).resolve().parents[1]


def module_hashes():
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in MODULES}


def carry_cc_runtime(plan, from_stage, to_stage):
    """A bridge-only rollout must retain the exact native CLI and host execution."""
    for key in ("cc_runtime_by_stage", "cc_versions_by_stage"):
        if from_stage in plan.get(key, {}):
            plan[key][to_stage] = deepcopy(plan[key][from_stage])


def promote_smoke(base, rollout, from_stage):
    """Keep the old smoke, then expose the actual rollout smoke to the campaign."""
    history = base / "smoke-history" / f"{from_stage}--{time.time_ns()}"
    history.mkdir(parents=True)
    for name in ("smoke", "smoke-result.json"):
        if (base / name).exists():
            (base / name).rename(history / name)
    (rollout / "smoke").rename(base / "smoke")
    shutil.copy2(rollout / "smoke-result.json", base / "smoke-result.json")
    return history


def validated_source(path):
    data = json.loads(path.read_text())
    if not data.get("passed") or data.get("source_module_sha256") != module_hashes():
        raise RuntimeError("bridge modules do not match the passing source validation")
    return data


def remaining_schedule(formal, report):
    """Preserve failures as completed positions, without silently adding a rerun."""
    key = lambda row: (row["task"], row["repeat"], row["effort"])
    scheduled = formal["schedule"] + formal.get("preserved_local_schedule", [])
    expected = {key(row) for row in scheduled}
    if len(expected) != len(scheduled):
        raise ValueError("duplicate scheduled position")
    done = {key(row) for row in report["attempts"]}
    if len(done) != len(report["attempts"]) or not done <= expected:
        raise ValueError("completed local attempts do not match the frozen schedule")
    remaining = [row for row in formal["schedule"] if key(row) not in done]
    preserved = [{name: row[name] for name in ("task", "repeat", "effort", "stage")}
                 for row in report["attempts"]]
    return remaining, preserved


def commit_evidence(base, output):
    owner = pwd.getpwuid(REPO.stat().st_uid).pw_name
    git = ["runuser", "-u", owner, "--", "git"] if os.geteuid() == 0 and REPO.stat().st_uid else ["git"]
    relative = str(output.relative_to(REPO))
    with (base / "checkpoint.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        subprocess.run([*git, "add", "--", relative], cwd=REPO, check=True)
        changed = subprocess.run([*git, "diff", "--cached", "--quiet", "--", relative], cwd=REPO)
        if changed.returncode == 1:
            subprocess.run([*git, "commit", "--only", "-m", "Checkpoint validated bridge transport rollout",
                            "--", relative], cwd=REPO, check=True)
        elif changed.returncode:
            raise RuntimeError("cannot inspect staged transition evidence")
        # A failed push remains visible and the regular checkpoint loop retries.
        result = {"at": time.time()}
        try:
            pushed = subprocess.run([*git, "-c", "http.proxy=http://127.0.0.1:31128", "push", "origin", "HEAD"],
                                    cwd=REPO, timeout=60)
            result.update(pushed=pushed.returncode == 0, returncode=pushed.returncode)
        except subprocess.TimeoutExpired:
            result.update(pushed=False, error_type="TimeoutExpired")
        write_json(base / "transport-transition-push.json", result)


def start_process(base, name, command, *, env=None, ordinary_user=False):
    options = {}
    if ordinary_user:
        options.update(user=REPO.stat().st_uid, group=REPO.stat().st_gid, extra_groups=[])
    with (base / f"{name}.log").open("ab") as log:
        child = subprocess.Popen(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True, **options)
    record = {**process_identity(child.pid), "started_at": time.time(), "args": command}
    write_json(base / f"{name}-process.json", record)
    return child


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    validation_args = parser.add_mutually_exclusive_group(required=True)
    validation_args.add_argument("--validated-smoke", type=Path)
    validation_args.add_argument("--validated-source", type=Path,
                                 help="Passing offline source validation; live smoke runs after the boundary")
    parser.add_argument("--checkpoint-output", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--from-stage", required=True)
    parser.add_argument("--to-stage", required=True)
    parser.add_argument("--resume-before-deploy", action="store_true",
                        help="Resume a failed preflight only if the original runtime is still in place")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("use sudo for verified rootfs archival and ordinary-UID CC setup")
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", name) for name in (args.from_stage, args.to_stage)):
        parser.error("invalid stage name")
    base = args.base.resolve(strict=True)
    output, evidence = args.checkpoint_output.resolve(), args.evidence_output.resolve()
    output.relative_to(REPO)
    evidence.relative_to(REPO)
    os.chdir(REPO)
    # A second coordinator must never stop or relaunch the first one's services.
    instance_lock = (base / "transport-transition.lock").open("a+")
    fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (base / "transport-transition-complete.json").exists():
        raise RuntimeError("transition already completed; use the running campaign")
    state_path = base / "transport-transition-state.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    if args.resume_before_deploy:
        before = json.loads((base / "transport-before-checkpoint.json").read_text())
        if (previous.get("status") != "archiving_completed_attempts"
                or campaign_progress(base)["controller_alive"]
                or any((base / name).exists() for name in ("transport-rollout-smoke",
                                                          "bridge-runtime-before-transport-fix.json"))
                or before["bridge_runtime"] != json.loads((base / "bridge-runtime.json").read_text())):
            raise RuntimeError("the recorded boundary is no longer safe to resume before deployment")
    elif previous and previous["status"] != "waiting_for_natural_completion":
        raise RuntimeError("previous transition already reached the deployment boundary; inspect before resuming")
    plan_path = base / "evaluation-plan.json"
    initial_plan = plan_path.read_bytes()
    plan = json.loads(initial_plan)
    formal = plan["formal_native_subset"]
    if formal["stage"] != args.from_stage or args.to_stage in plan["bridge_runtime_by_stage"]:
        raise RuntimeError("source or destination stage does not match the plan")
    if args.resume_before_deploy and before["bridge_runtime"] != plan["bridge_runtime_by_stage"][args.from_stage]:
        raise RuntimeError("recorded source-stage runtime changed since the predeployment checkpoint")
    if args.validated_source:
        validated_runtime = validated_source(args.validated_source.resolve(strict=True))
    else:
        validation = args.validated_smoke.resolve(strict=True)
        validated_runtime = json.loads((validation / "bridge-runtime.json").read_text())
        if (not json.loads((validation / "validation-complete.json").read_text()).get("passed")
                or not json.loads((validation / "smoke-result.json").read_text()).get("passed")
                or module_hashes() != validated_runtime["source_module_sha256"]):
            raise RuntimeError("current bridge modules do not match the successfully validated service")
    write_json(base / "transport-transition-process.json", {**process_identity(os.getpid()), "started_at": time.time(),
        "from_stage": args.from_stage, "to_stage": args.to_stage,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    while campaign_progress(base)["controller_alive"]:
        write_json(base / "transport-transition-state.json", {"status": "waiting_for_natural_completion",
                   "at": time.time(), "current_agent_signalled": False})
        time.sleep(15)
    report = summarize(base)
    if report["pending"] or snapshot(LogTail(base / "bridge-debug.jsonl").read())["active"]:
        raise RuntimeError("unfinished agents or bridge requests remain; preserve and investigate")
    if plan_path.read_bytes() != initial_plan or module_hashes() != validated_runtime["source_module_sha256"]:
        raise RuntimeError("plan or bridge modules changed while waiting")
    remaining, preserved = remaining_schedule(formal, report)
    if not preserved:
        raise RuntimeError("no completed local position at the requested boundary")
    error = base / "campaign-error.json"
    boundary_error = json.loads(error.read_text()) if error.exists() else None
    if boundary_error and boundary_error.get("error") != (
            "runner source changed after initialization; start a new process before another attempt"):
        raise RuntimeError("unexpected preceding controller error; investigate before rollout")
    record = {"started_at": time.time(), "from_stage": args.from_stage, "to_stage": args.to_stage,
              "agent_interrupted": False, "preserved_completed_local_slots": preserved,
              "remaining_schedule": remaining, "before_stage_results": report,
              "artifact_review": review(base), "validated_candidate_runtime": validated_runtime}
    old_runtime = json.loads((base / "bridge-runtime.json").read_text())
    health_url = f'http://127.0.0.1:{old_runtime["bridge_port"]}/health'
    write_json(base / "transport-before-checkpoint.json", collect(base, health_url))
    # Persist the completed grade before any lengthy archival or deployment.
    save(base, output, health_url, commit=True, push=True)
    write_json(base / "transport-transition-state.json", {"status": "archiving_completed_attempts", "at": time.time()})
    for root in list(completed_roots(base)):
        archive_root(root)
    service = identify(base, "bridge-runtime.json", "nvplay_bridge:app")
    if service is None:
        raise RuntimeError("recorded old bridge is not running")
    # Stop only after natural completion, inspection, and verified archives.
    with (base / "checkpoint.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        checkpoint_service = identify(base, "checkpoint-process.json", "checkpoint_terminal_native.py")
        if checkpoint_service:
            record["stopped_checkpoint"] = stop(checkpoint_service)
    # The controller and periodic writer share checkpoint-push.log. Run both
    # with the same permissions; save() still runs Git as the repository owner.
    checkpoint = start_process(base, "checkpoint", [sys.executable, "-u", "tools/watch_checkpoint_terminal_native.py",
        "--base", str(base), "--output", str(output), "--health-url", health_url,
        "--commit", "--push"])
    write_json(state_path, {"status": "switching_bridge", "at": time.time()})
    write_json(base / "bridge-runtime-before-transport-fix.json", old_runtime)
    record["stopped_bridge"] = stop(service)
    settings = dict(old_runtime["configuration"])
    settings["BROWSER_DEBUG_LOG"] = str(base / "bridge-debug.jsonl")
    child = start_process(base, "bridge-server", [sys.executable, "-m", "uvicorn", "nvplay_bridge:app",
        "--host", "127.0.0.1", "--port", str(old_runtime["bridge_port"]), "--timeout-keep-alive", "75"],
        env={**os.environ, **settings, "HTTP_PROXY": "http://127.0.0.1:31128",
             "HTTPS_PROXY": "http://127.0.0.1:31128", "ALL_PROXY": "http://127.0.0.1:31128",
             "NO_PROXY": "127.0.0.1,localhost"}, ordinary_user=True)
    runtime = {**old_runtime, **process_identity(child.pid), "started_at": time.time(), "configuration": settings,
        "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_module_sha256": module_hashes()}
    health_url = f'http://127.0.0.1:{runtime["bridge_port"]}/health'
    runtime.pop("health_at_start", None)
    # The administrative timer must own the new PID even during readiness.
    write_json(base / "bridge-runtime.json", runtime)
    runtime["health_at_start"] = await_json(health_url, child)
    write_json(base / "bridge-runtime.json", runtime)
    write_json(base / "transport-transition-state.json", {"status": "validating_actual_rollout", "at": time.time()})
    # Smoke this actual deployment with the unchanged model/context policy.
    rollout = base / "transport-rollout-smoke"
    rollout.mkdir()
    clone_root(base / "native-base", rollout / "native-base")
    smoke_plan = deepcopy(plan)
    smoke_plan["formal_native_subset"]["stage"] = args.to_stage
    smoke_plan["bridge_runtime_by_stage"][args.to_stage] = runtime
    carry_cc_runtime(smoke_plan, args.from_stage, args.to_stage)
    write_json(rollout / "evaluation-plan.json", smoke_plan)
    write_json(rollout / "next-uid.json", {"next": 28020})
    (rollout / "bridge-debug.jsonl").symlink_to(base / "bridge-debug.jsonl")
    for effort in ("high", "max"):
        (rollout / f"settings-formal-{effort}.json").write_bytes((base / f"settings-formal-{effort}.json").read_bytes())
    with (rollout / "smoke-controller.log").open("ab") as log:
        subprocess.run([sys.executable, str(REPO / "tools/smoke_terminal_native.py"), "--base", str(rollout)],
                       cwd=REPO, stdout=log, stderr=subprocess.STDOUT, check=True)
    for root in list(completed_roots(rollout)):
        archive_root(root)
    smoke_result = json.loads((rollout / "smoke-result.json").read_text())
    record["actual_rollout_smoke"] = [{"effort": row["effort"], "passed": row["passed"],
        "process": row["process"], "logical_requests": row["bridge"]["logical_requests"],
        "errors": len(row["bridge"]["errors"]), "retries": len(row["bridge"]["retries"]),
        "unfinished": row["bridge"]["unfinished"], "reported_context_windows": row["reported_context_windows"]}
        for row in smoke_result["efforts"]]
    plan.setdefault("protocol_history", []).append({"recorded_at": time.time(),
        "formal_native_subset": deepcopy(formal), "source_plan_sha256": hashlib.sha256(initial_plan).hexdigest(),
        "reason": "Validated bridge rollout at a natural boundary; all completed outcomes retain their original stage"})
    formal.update(stage=args.to_stage, frozen_at=time.time(), schedule=remaining, preserved_local_schedule=preserved,
                  live_validation={"path": str(rollout.relative_to(base)), "passed": True,
                    "smoke_result_sha256": hashlib.sha256((rollout / "smoke-result.json").read_bytes()).hexdigest()})
    plan["bridge_runtime_by_stage"][args.to_stage] = runtime
    carry_cc_runtime(plan, args.from_stage, args.to_stage)
    record["previous_smoke_archive"] = str(promote_smoke(base, rollout, args.from_stage).relative_to(base))
    write_json(plan_path, plan)
    if error.exists():
        record["preceding_controller_error"] = boundary_error
        error.rename(base / "campaign-error-before-transport-fix.json")
    record.update(status="validated_rollout_starting_remaining_slots", runtime=runtime, completed_at=time.time())
    write_json(evidence, record)
    save(base, output, health_url, commit=True, push=True)
    commit_evidence(base, evidence)
    campaign = start_process(base, "campaign-launch", [sys.executable, "-u", "tools/run_terminal_native_campaign.py",
        "--base", str(base), "--checkpoint-output", str(output), "--push-checkpoints", "--resume"])
    write_json(base / "transport-transition-complete.json", {"at": time.time(), "from_stage": args.from_stage,
        "to_stage": args.to_stage, "preserved_local_slots": len(preserved), "remaining_slots": len(remaining),
        "checkpoint_process": process_identity(checkpoint.pid), "campaign_process": process_identity(campaign.pid)})
    write_json(base / "transport-transition-state.json", {"status": "remaining_campaign_started", "at": time.time()})
    save(base, output, health_url, commit=True, push=True)
    print(json.dumps({"transition_complete": True, "remaining_slots": len(remaining)}), flush=True)


if __name__ == "__main__":
    main()

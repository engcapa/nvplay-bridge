"""Run the frozen native subset with immediate Git checkpoints and bounded disk use."""

from __future__ import annotations

import argparse
from concurrent.futures import as_completed, ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time

import eval_terminal_native as runner
from archive_terminal_native_roots import archive_root, completed_roots
from checkpoint_terminal_native import save
from native_eval_support import allocate_uid, write_json


def bridge_restart_requested(base, stage):
    path = base / "bridge-restart-request.json"
    if not path.is_file():
        return False
    request = json.loads(path.read_text())
    return request.get("requested_by_user") is True and request.get("from_stage") == stage


def run_serial_efforts(base, stage, efforts, attempt, finish):
    """Do not queue another host CC while a requested bridge restart is pending."""
    for effort in efforts:
        if bridge_restart_requested(base, stage):
            return True
        result = attempt(effort)
        if result is not None:
            finish(result)
    return bridge_restart_requested(base, stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--checkpoint-output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--recover-interrupted", action="store_true")
    parser.add_argument("--retry-controls", action="store_true")
    parser.add_argument("--push-checkpoints", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("native setup requires sudo; CC uses independent ordinary UIDs")
    if args.recover_interrupted and not args.resume:
        parser.error("--recover-interrupted requires --resume")
    base = args.base.resolve()
    if not (base / "native-base-ready.json").exists():
        parser.error("finish and validate native-base provisioning first")
    repo = Path(__file__).resolve().parents[1]
    os.chdir(repo)
    runner.TOOLING_SOURCES += tuple(Path(__file__).with_name(name) for name in (
        "run_terminal_native_campaign.py", "archive_terminal_native_roots.py", "checkpoint_terminal_native.py"))
    evaluation = runner.Evaluation(base)
    formal = evaluation.plan["formal_native_subset"]
    stage, budget = formal["stage"], formal["budget_seconds"]
    health_url = f'http://127.0.0.1:{evaluation.plan["bridge_runtime_by_stage"][stage]["bridge_port"]}/health'

    def checkpoint(status, **details):
        write_json(base / "campaign-state.json", {"status": status, "updated_at": time.time(),
                   "pid": os.getpid(), "controller_identity": runner.process_identity(os.getpid()),
                   "stage": stage, "source_identity": evaluation.identity(stage), **details})
        save(base, args.checkpoint_output.resolve(), health_url, commit=True, push=args.push_checkpoints)

    try:
        for name in evaluation.plan["pilot_tasks"]:
            checkpoint("preparing", task=name)
            evaluation.prepare(name)
        for name in evaluation.plan["pilot_tasks"]:
            checkpoint("validating_controls", task=name)
            evaluation.control(name, retry=args.retry_controls)
            checkpoint("control_validated", task=name)
            # Complete baseline/oracle filesystems remain restorable in verified archives.
            for root in list(completed_roots(base)):
                archive_root(root)
        smoke = base / "smoke-result.json"
        if smoke.exists():
            smoke_data = json.loads(smoke.read_text())
            if not smoke_data.get("passed"):
                raise RuntimeError("previous smoke failed; preserve and diagnose it before another smoke")
            if stage in evaluation.plan.get("cc_runtime_by_stage", {}):
                runtime = evaluation.plan["cc_runtime_by_stage"][stage]
                if (smoke_data.get("stage") != stage or any(
                        row.get("source_identity", {}).get("cc_runtime") != runtime
                        for row in smoke_data.get("efforts", [])) or len(smoke_data.get("efforts", [])) != 2):
                    raise RuntimeError("live smoke belongs to another CC runtime/stage")
        else:
            checkpoint("validating_live_tools")
            subprocess.run(["python3", "tools/smoke_terminal_native.py", "--base", str(base)], check=True)
        checkpoint("live_tools_validated")
        for root in list(completed_roots(base)):
            archive_root(root)
        for name, repeat, efforts in runner.attempt_batches(evaluation.plan, stage, formal["repeats_per_effort"]):
            if bridge_restart_requested(base, stage):
                checkpoint("waiting_for_bridge_restart")
                return
            checkpoint("formal_running", task=name, repeat=repeat)

            def attempt(effort):
                if args.resume and evaluation.resume_attempt(name, effort, stage, repeat,
                                                             budget, args.recover_interrupted):
                    return None
                return evaluation.attempt(name, effort, stage, repeat, budget, allocate_uid(base))

            def finish(result):
                checkpoint("formal_running", task=name, repeat=repeat,
                           completed_effort=result["effort"], score=result["grade"]["score"])
                directory = base / "runs" / stage / f'{name}--{result["effort"]}--r{repeat}'
                for role in ("agent-root", "verifier-root"):
                    root = directory / role
                    if root.exists():
                        archive_root(root)

            if formal.get("agent_concurrency", 2) == 1:
                if run_serial_efforts(base, stage, efforts, attempt, finish):
                    checkpoint("waiting_for_bridge_restart")
                    return
                continue
            with ThreadPoolExecutor(max_workers=formal.get("agent_concurrency", 2)) as pool:
                futures = []
                for effort in efforts:
                    if args.resume and evaluation.resume_attempt(name, effort, stage, repeat,
                                                                 budget, args.recover_interrupted):
                        continue
                    futures.append(pool.submit(evaluation.attempt, name, effort, stage, repeat,
                                               budget, allocate_uid(base)))
                for future in as_completed(futures):
                    result = future.result()
                    finish(result)
        checkpoint("formal_complete")
    except Exception as error:
        write_json(base / "campaign-error.json", {"at": time.time(), "error_type": type(error).__name__,
                                                   "error": str(error)})
        raise


if __name__ == "__main__":
    main()

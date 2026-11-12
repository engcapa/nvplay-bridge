"""Install a validated CC idle fix after natural completion, smoke, then resume.

The active campaign must already have a request to stop scheduling at a natural
boundary. This coordinator never signals CC or restarts the bridge/browser.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time

from archive_terminal_native_roots import archive_root, completed_roots
from checkpoint_terminal_native import campaign_progress, save, smoke_summary
from eval_terminal_native import process_identity
from monitor_bridge import LogTail, snapshot
from native_eval_support import write_json
from report_terminal_native import summarize


REPO = Path(__file__).resolve().parents[1]


def git_command():
    owner = pwd.getpwuid(REPO.stat().st_uid).pw_name
    return ["runuser", "-u", owner, "--", "git"] if os.geteuid() == 0 else ["git"]


def install_candidate(base, validation):
    expected = validation["candidate_source_sha256"]
    git = git_command()
    commit = validation["candidate_commit"]
    for name, digest in expected.items():
        source = subprocess.check_output([*git, "show", f"{commit}:{name}"], cwd=REPO)
        if hashlib.sha256(source).hexdigest() != digest:
            raise RuntimeError("candidate commit differs from its passing validation")
    with (base / "checkpoint.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not all(hashlib.sha256((REPO / name).read_bytes()).hexdigest() == digest
                   for name, digest in expected.items() if (REPO / name).exists()) or any(
                not (REPO / name).exists() for name in expected):
            for arguments in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
                if subprocess.run([*git, *arguments, "--", *expected], cwd=REPO).returncode:
                    raise RuntimeError("candidate files contain local changes; refusing to overwrite")
            result = subprocess.run([*git, "cherry-pick", "--no-edit", commit], cwd=REPO)
            if result.returncode:
                subprocess.run([*git, "cherry-pick", "--abort"], cwd=REPO, check=True)
                raise RuntimeError("candidate could not be applied cleanly")
        if any(hashlib.sha256((REPO / name).read_bytes()).hexdigest() != digest
               for name, digest in expected.items()):
            raise RuntimeError("installed candidate hashes differ from the passing source")
        return subprocess.check_output([*git, "rev-parse", "HEAD"], cwd=REPO, text=True).strip()


def proxy_connects():
    # A CONNECT plus an HTTP response tests reachability; the live smoke tests
    # authentication, actual model output and tool delivery separately.
    result = subprocess.run(["curl", "--proxy", "http://127.0.0.1:31128", "--noproxy", "",
        "--connect-timeout", "10", "--max-time", "15", "--silent", "--output", "/dev/null",
        "--write-out", "%{http_connect},%{http_code}", "https://build.nvidia.com"],
        capture_output=True, text=True)
    values = result.stdout.split(",")
    return result.returncode == 0 and len(values) == 2 and values[0] == "200" and values[1] != "000"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--checkpoint-output", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("use sudo for the existing protected evaluation evidence")
    os.chdir(REPO)
    base = args.base.resolve(strict=True)
    output, evidence = args.checkpoint_output.resolve(), args.evidence_output.resolve()
    output.relative_to(REPO)
    evidence.relative_to(REPO)
    validation = json.loads(args.validation.read_text())
    if not validation.get("passed") or not validation.get("fresh_live_high_and_max_tool_smoke_required"):
        raise RuntimeError("passing source validation and a fresh live smoke requirement are mandatory")
    instance = (base / "stream-idle-transition.lock").open("a+")
    fcntl.flock(instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state_path = base / "stream-idle-transition-state.json"
    if state_path.exists():
        raise RuntimeError("a transition already exists; inspect its saved phase before resuming")
    request = json.loads((base / "bridge-restart-request.json").read_text())
    plan = json.loads((base / "evaluation-plan.json").read_text())
    if (request.get("from_stage") != plan["formal_native_subset"]["stage"]
            or request.get("to_stage") != validation["next_stage"]
            or request.get("current_agent_must_not_be_signalled") is not True):
        raise RuntimeError("natural boundary request does not match the validated transition")
    runtime = json.loads((base / "bridge-runtime.json").read_text())
    health = f'http://127.0.0.1:{runtime["bridge_port"]}/health'
    record = {"started_at": time.time(), "current_agent_signalled": False,
              "bridge_restarted": False, "candidate_commit": validation["candidate_commit"],
              "from_stage": request["from_stage"], "to_stage": request["to_stage"]}
    write_json(base / "stream-idle-transition-process.json", {
        **process_identity(os.getpid()), "started_at": time.time(),
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})

    def state(phase):
        if record.get("status") == phase:
            return
        record.update(status=phase, updated_at=time.time())
        write_json(state_path, record)
        write_json(evidence, record)
        relative = str(evidence.relative_to(REPO))
        git = git_command()
        with (base / "checkpoint.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            subprocess.run([*git, "add", "--", relative], cwd=REPO, check=True)
            subprocess.run([*git, "commit", "--only", "-m", f"Checkpoint CC idle transition: {phase}",
                            "--", relative], cwd=REPO, check=True)

    try:
        while campaign_progress(base)["controller_alive"]:
            state("waiting_for_natural_completion")
            time.sleep(15)
        report = summarize(base)
        if report["pending"] or snapshot(LogTail(base / "bridge-debug.jsonl").read())["active"]:
            raise RuntimeError("unfinished task or bridge request remains after the controller exit")
        if (base / "campaign-error.json").exists() or (base / "pause-state.json").exists():
            raise RuntimeError("campaign error or administrative pause requires inspection before transition")
        if json.loads((base / "bridge-runtime.json").read_text()) != runtime:
            raise RuntimeError("bridge runtime changed while waiting for the natural boundary")
        record["preserved_results"] = report
        state("archiving_completed_attempts")
        save(base, output, health, commit=True, push=True)
        for root in list(completed_roots(base)):
            archive_root(root)
        record["installed_commit"] = install_candidate(base, validation)
        state("configuring_extended_stream_idle")
        subprocess.run([sys.executable, "tools/configure_terminal_native_stream_idle.py", "--base", str(base),
                        "--stage", request["to_stage"]], check=True)
        while not proxy_connects():
            state("waiting_for_proxy_before_live_smoke")
            time.sleep(15)
        state("validating_actual_live_tools")
        save(base, output, health, commit=True, push=True)
        subprocess.run([sys.executable, "tools/smoke_terminal_native.py", "--base", str(base)], check=True)
        smoke = json.loads((base / "smoke-result.json").read_text())
        if (not smoke.get("passed") or smoke.get("stage") != request["to_stage"]
                or len(smoke.get("efforts", [])) != 2
                or {row["effort"] for row in smoke["efforts"]} != {"high", "max"}
                or not all(row.get("passed") for row in smoke["efforts"])):
            raise RuntimeError("actual smoke did not pass in the new stage")
        record["actual_smoke"] = smoke_summary(smoke)
        for root in list(completed_roots(base)):
            archive_root(root)
        state("validated_starting_remaining_slots")
        save(base, output, health, commit=True, push=True)
        with (base / "campaign-launch-after-stream-idle.log").open("ab") as log:
            campaign = subprocess.Popen([sys.executable, "-u", "tools/run_terminal_native_campaign.py",
                "--base", str(base), "--checkpoint-output", str(output), "--push-checkpoints", "--resume"],
                cwd=REPO, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        record["campaign_process"] = process_identity(campaign.pid)
        state("remaining_campaign_started")
        save(base, output, health, commit=True, push=True)
    except Exception as error:
        record.update(error_type=type(error).__name__, error_sha256=hashlib.sha256(str(error).encode()).hexdigest())
        state("failed_preserved_for_review")
        raise


if __name__ == "__main__":
    main()

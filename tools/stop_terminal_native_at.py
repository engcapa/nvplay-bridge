"""At a user-requested reboot deadline, stop only this evaluation and push its checkpoint."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import time

from checkpoint_terminal_native import save
from eval_terminal_native import process_identity
from monitor_bridge import LogTail, snapshot
from native_eval_support import bridge_activity, write_json
from native_path_cc import return_agent_root, runtime_root
from pause_terminal_native_services import SERVICES, identify, stop
from report_terminal_native import summarize


def read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def scoped_processes(uids: set[int], roots: set[str], sessions: set[int]):
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or not (identity := process_identity(int(proc.name))):
            continue
        try:
            fields = next(line for line in (proc / "status").read_text().splitlines() if line.startswith("Uid:"))
            uid = int(fields.split()[1])
            if uid in uids or (sessions and os.getsid(identity["pid"]) in sessions):
                yield identity
            elif uid == 0 and roots:
                args = set(os.fsdecode(arg) for arg in (proc / "cmdline").read_bytes().split(b"\0"))
                if args & roots or os.readlink(proc / "root") in roots:
                    yield identity
        except (OSError, StopIteration):
            continue


def stop_attempt_processes(uids: set[int], roots: set[str], sessions: set[int]):
    signalled = set()
    for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 3)):
        until = time.monotonic() + grace
        while time.monotonic() < until:
            processes = list(scoped_processes(uids, roots, sessions))
            if not processes:
                return sorted(signalled)
            for expected in processes:
                pid = expected["pid"]
                try:
                    fd = os.pidfd_open(pid)
                    try:
                        if process_identity(pid) == expected:
                            signal.pidfd_send_signal(fd, sig)
                            signalled.add(pid)
                    finally:
                        os.close(fd)
                except ProcessLookupError:
                    continue
            time.sleep(0.2)
    if list(scoped_processes(uids, roots, sessions)):
        raise RuntimeError("evaluation processes remain after the reboot stop")
    return sorted(signalled)


def capture_incomplete(base: Path, directory: Path, observation: dict, *, deadline: float,
                       reason: str = "user requested container reboot") -> dict:
    stage = directory.parent.name
    task, effort, repeat = directory.name.rsplit("--", 2)
    archive = base / "runs-interrupted" / stage / f"{directory.name}--{time.time_ns()}"
    entry = {"status": "administratively_interrupted", "score": None, "excluded_from_model_grades": True,
             "recorded_at": time.time(), "requested_stop_at": deadline, "reason": reason,
             "task": task, "effort": effort, "stage": stage, "repeat": int(repeat.removeprefix("r")),
             "original_attempt": str(directory.relative_to(base)), "archive": str(archive.relative_to(base)),
             "progress_at_stop": {key: observation[key] for key in ("status", "started_at", "elapsed_seconds",
                                  "tool_calls", "last_tool") if key in observation}}
    started = observation.get("started_at") or read(directory / "agent-state.json").get("started_at")
    if started is not None:
        telemetry = bridge_activity(base / "bridge-debug.jsonl", started, time.time(), effort)
        entry["bridge"] = {key: telemetry[key] for key in ("logical_requests", "upstream_attempts")}
        entry["bridge"].update({key: len(telemetry[key]) for key in ("errors", "retries", "cancelled", "unfinished")})
    archive.parent.mkdir(parents=True, exist_ok=True)
    write_json(directory / "interruption.json", entry)
    directory.rename(archive)
    return entry


def pause(base: Path, output: Path, deadline: float) -> dict:
    stopped = []
    scheduler_sessions = set()

    def stop_scheduler(service: dict):
        pid = service["identity"]["pid"]
        try:
            if os.getsid(pid) == pid and process_identity(pid) == service["identity"]:
                # These detached schedulers own their sessions. Include their
                # provisioning, archive and Git helpers in process cleanup.
                scheduler_sessions.add(pid)
            stopped.append(stop(service))
        except ProcessLookupError:
            if process_identity(pid) == service["identity"]:
                raise

    # Stop schedulers first so a signal to an agent cannot cause grading or
    # start another slot. Only use current, recorded kernel identities.
    for filename, command in (("transport-transition-process.json", "advance_terminal_native_transport.py"),
                              ("campaign-launch-process.json", "run_terminal_native_campaign.py")):
        record = read(base / filename)
        if not all(key in record for key in ("pid", "boot_id", "start_ticks")):
            continue
        if service := identify(base, filename, command):
            stop_scheduler(service)
    current = read(base / "campaign-state.json").get("controller_identity")
    if current and process_identity(current["pid"]) == current:
        service = identify(base, "campaign-state.json", "run_terminal_native_campaign.py")
        if not service or service["identity"] != current:
            raise RuntimeError("cannot verify the campaign scheduler")
        stop_scheduler(service)

    report = summarize(base)
    directories = [base / row["attempt"] for row in report["pending"]]
    # Include isolated deployment smoke if the deadline falls during rollout.
    for smoke in (base / "smoke", base / "transport-rollout-smoke" / "smoke"):
        if smoke.is_dir() and not (smoke.parent / "smoke-result.json").exists():
            directories.extend(path for path in smoke.iterdir() if path.is_dir())
    uids, roots = set(), set()
    for directory in directories:
        for role in ("agent", "verifier"):
            root, native_uid = runtime_root(directory) if role == "agent" else (directory / "verifier-root", None)
            if native_uid is not None:
                uids.add(native_uid)
            if root.is_symlink():
                raise RuntimeError("refusing to interrupt an aliased evaluation root")
            if root.is_dir():
                roots.add(str(root))
            state = read(directory / f"{role}-state.json")
            if not state:
                continue
            args = state["command"]
            launch_root = read(directory / "runtime-root.json").get("root") if native_uid is not None else None
            if str(root) not in args and (not launch_root or launch_root not in args):
                raise RuntimeError("attempt process record does not match its root")
            for arg in args:
                if arg.startswith(("--userspec=", "--reuid=")):
                    uid = int(arg.split("=", 1)[1].split(":", 1)[0])
                    if role == "agent":
                        if not 20000 <= uid <= 29999:
                            raise RuntimeError("refusing to signal an unreserved agent UID")
                        if native_uid is not None and uid != native_uid:
                            raise RuntimeError("native CC process UID does not match its root marker")
                        uids.add(uid)
    signalled = stop_attempt_processes(uids, roots, scheduler_sessions)
    for directory in directories:
        if (directory / "runtime-root.json").is_file():
            return_agent_root(directory)
    # Give a periodic checkpoint already writing Git state time to finish;
    # take the lock before stopping it so it cannot start a new write.
    with (base / "checkpoint.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if service := identify(base, "checkpoint-process.json", "checkpoint_terminal_native.py"):
            stopped.append(stop(service))
    log = base / "bridge-debug.jsonl"
    until = time.monotonic() + 7
    while (active := snapshot(LogTail(log).read())["active"]) and time.monotonic() < until:
        time.sleep(0.2)

    for filename, command in SERVICES:
        if filename == "checkpoint-process.json":
            continue
        if service := identify(base, filename, command):
            stopped.append(stop(service))
    interrupted = [capture_incomplete(base, base / row["attempt"], row, deadline=deadline)
                   for row in report["pending"]]
    state = {"status": "paused_for_reboot", "paused_at": time.time(), "requested_stop_at": deadline,
             "reason": "user requested stop for container reboot",
             "completed": len(report["attempts"]), "interrupted_attempts": len(interrupted),
             "interrupted_positions": interrupted, "signalled_attempt_pids": signalled,
             "stopped_services": stopped, "active_bridge_requests_before_shutdown": len(active)}
    write_json(base / "pause-state.json", state)
    runtime = read(base / "bridge-runtime.json")
    save(base, output, f'http://127.0.0.1:{runtime["bridge_port"]}/health', commit=True, push=True)
    sync = read(base / "checkpoint-git-sync.json")
    if not sync.get("pushed"):
        save(base, output, f'http://127.0.0.1:{runtime["bridge_port"]}/health',
             commit=True, push=True, changes_only=True)
        sync = read(base / "checkpoint-git-sync.json")
    result = {"paused": True, "completed_local": state["completed"], "interrupted": len(interrupted),
              "final_checkpoint_pushed": bool(sync.get("pushed")), "checkpoint_commit": sync.get("commit"),
              "paused_at": state["paused_at"]}
    write_json(base / "reboot-stop-result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--checkpoint-output", type=Path, required=True)
    parser.add_argument("--stop-at", type=float, required=True, help="Unix timestamp set by the operator")
    args = parser.parse_args()
    if os.geteuid() != 0 or not math.isfinite(args.stop_at) or args.stop_at <= 0:
        parser.error("use sudo and a valid absolute stop timestamp")
    base = args.base.resolve(strict=True)
    output = args.checkpoint_output.resolve(strict=True)
    output.relative_to(Path(__file__).resolve().parents[1])
    with (base / "reboot-stop.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (base / "pause-state.json").exists():
            raise RuntimeError("a reboot pause is already recorded")
        write_json(base / "reboot-stop-request.json", {"armed_at": time.time(), "stop_at": args.stop_at,
                   "identity": process_identity(os.getpid()), "reason": "user requested container reboot"})
        while (remaining := args.stop_at - time.time()) > 0:
            time.sleep(min(10, remaining))
        result = pause(base, output, args.stop_at)
        print(json.dumps(result), flush=True)
        if not result["final_checkpoint_pushed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

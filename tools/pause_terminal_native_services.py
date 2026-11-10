"""Stop this campaign's services only after agents, grading, and archives finish."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import time

from archive_terminal_native_roots import completed_roots
from checkpoint_terminal_native import campaign_progress
from eval_terminal_native import process_identity
from monitor_bridge import LogTail, snapshot
from native_eval_support import write_json
from report_terminal_native import summarize


SERVICES = (
    ("checkpoint-process.json", "checkpoint_terminal_native.py"),
    ("resource-monitor-process.json", "monitor_terminal_native_resources.py"),
    ("cdp-observer-process.json", "monitor_cdp_network.py"),
    ("bridge-monitor-process.json", "monitor_bridge.py"),
    ("bridge-runtime.json", "nvplay_bridge:app"),
    ("brave-process.json", "brave"),
)


def preflight(base: Path) -> tuple[dict, list[str]]:
    report = summarize(base)
    controller = campaign_progress(base)
    active = snapshot(LogTail(base / "bridge-debug.jsonl").read())["active"]
    unarchived = list(completed_roots(base))
    blocked = []
    if controller["controller_alive"]:
        blocked.append("campaign controller is still alive")
    if report["pending"]:
        blocked.append("unfinished attempt directories remain")
    if active:
        blocked.append("bridge requests are still active")
    if unarchived:
        blocked.append("completed rootfs evidence still needs verified archival")
    request_path = base / "pause-request.json"
    if not request_path.is_file():
        blocked.append("no recorded user pause request")
    else:
        request = json.loads(request_path.read_text())
        for effort in ("high", "max"):
            result = base / "runs" / request["stage"] / f'{request["task"]}--{effort}--r{request["repeat"]}' / "result.json"
            if not result.is_file():
                blocked.append(f"requested task {effort} is not complete")
    return {"completed": len(report["attempts"]), "pending": len(report["pending"]),
            "active_bridge_requests": len(active), "unarchived_roots": len(unarchived),
            "controller": controller}, blocked


def identify(base: Path, filename: str, expected_command: str) -> dict | None:
    record_path = base / filename
    if not record_path.is_file():
        return None
    record = json.loads(record_path.read_text())
    pid = record["pid"]
    actual = process_identity(pid)
    if actual is None:
        return None
    proc = Path("/proc") / str(pid)
    args = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    if expected_command not in args:
        raise RuntimeError(f"refusing PID with different command: {filename}")
    if filename == "bridge-runtime.json":
        env = dict(item.split(b"=", 1) for item in (proc / "environ").read_bytes().split(b"\0") if b"=" in item)
        owned = env.get(b"BROWSER_DEBUG_LOG") == str(base / "bridge-debug.jsonl").encode()
    elif filename == "brave-process.json":
        owned = f"--user-data-dir={base}/brave-profile " in args
    else:
        owned = str(base) in args
    if not owned:
        raise RuntimeError(f"refusing process outside this campaign: {filename}")
    if "boot_id" in record and "start_ticks" in record:
        expected = {key: record[key] for key in ("pid", "boot_id", "start_ticks")}
        if expected != actual:
            raise RuntimeError(f"refusing reused PID: {filename}")
    return {"record": filename, "identity": actual}


def stop(service: dict) -> dict:
    expected = service["identity"]
    pid = expected["pid"]
    # Pin the kernel process identity before signalling; never signal a reused PID.
    descriptor = os.pidfd_open(pid)
    try:
        if process_identity(pid) != expected:
            raise RuntimeError(f"process identity changed before stop: {service['record']}")
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        deadline = time.monotonic() + 20
        while process_identity(pid) == expected:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"service did not exit after SIGTERM: {service['record']}")
            time.sleep(0.2)
    finally:
        os.close(descriptor)
    return {**service, "stopped_at": time.time(), "signal": "SIGTERM"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Default only reports blockers and makes no changes")
    args = parser.parse_args()
    base = args.base.resolve(strict=True)
    state, blocked = preflight(base)
    if not args.execute:
        print(json.dumps({"dry_run": True, **state, "blocked": blocked}))
        return
    if os.geteuid() != 0:
        parser.error("use sudo to inspect ownership and stop all campaign monitors")
    if blocked:
        parser.error("; ".join(blocked))
    # Validate every target before stopping any component.
    services = [service for filename, command in SERVICES
                if (service := identify(base, filename, command)) is not None]
    stopped = [stop(service) for service in services]
    write_json(base / "pause-state.json", {
        "status": "paused_for_reboot", "paused_at": time.time(), "completed": state["completed"],
        "interrupted_attempts": 0, "preflight": state, "stopped_services": stopped,
        "reason": "User requested pause after current task for container reboot"})
    print(json.dumps({"paused_for_reboot": True, "completed": state["completed"], "stopped_services": stopped}))


if __name__ == "__main__":
    main()

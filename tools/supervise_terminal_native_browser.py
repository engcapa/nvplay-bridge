"""Restore this evaluation's browser after exit, without interrupting live CC.

Only a recorded browser that has exited and an unavailable local CDP endpoint
authorize a restart. A live browser is never killed. The existing reboot
deadline and pause record take precedence over recovery.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import time
import urllib.request

from advance_terminal_native_transport import start_process
from eval_terminal_native import process_identity
from native_eval_support import write_json
from pause_terminal_native_services import identify, stop


LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def may_recover(base: Path, now: float) -> bool:
    deadline = read(base / "reboot-stop-request.json").get("stop_at")
    return not (base / "pause-state.json").exists() and deadline is not None and now < deadline


def owner_environment(base: Path) -> dict:
    owner = pwd.getpwuid(base.stat().st_uid)
    if owner.pw_uid == 0:
        raise RuntimeError("browser profile must belong to an ordinary repository owner")
    env = {**os.environ, "HOME": owner.pw_dir, "XDG_CONFIG_HOME": owner.pw_dir + "/.config",
           "XDG_CACHE_HOME": owner.pw_dir + "/.cache", "NO_PROXY": "127.0.0.1,localhost"}
    authority = Path(owner.pw_dir) / ".Xauthority"
    if authority.is_file():
        env["XAUTHORITY"] = str(authority)
    return env


def browser_command(base: Path, record: dict, cdp_port: int) -> list:
    command = record.get("args", [])
    if (not command or f"--user-data-dir={base}/brave-profile" not in command
            or f"--remote-debugging-port={cdp_port}" not in command
            or "--proxy-server=http://127.0.0.1:31128" not in command
            or not Path(command[0]).is_file()):
        raise RuntimeError("browser process record does not match the isolated profile, CDP or proxy")
    return command


def tick(base: Path) -> dict:
    now = time.time()
    if not may_recover(base, now):
        return {"status": "stopped_for_deadline_or_pause", "at": now}
    runtime = read(base / "bridge-runtime.json")
    port = runtime["cdp_port"]
    record = read(base / "brave-process.json")
    expected = {key: record[key] for key in ("pid", "boot_id", "start_ticks")}
    actual = process_identity(record["pid"])
    if actual is not None and actual != expected:
        raise RuntimeError("browser PID was reused; refusing automatic recovery")
    command = browser_command(base, record, port)
    try:
        with LOCAL_HTTP.open(f"http://127.0.0.1:{port}/json/version", timeout=3) as response:
            json.load(response)
        cdp_ready = True
    except (OSError, ValueError):
        cdp_ready = False
    if actual is not None:
        state = {"status": "monitoring" if cdp_ready else "live_browser_cdp_unavailable",
                 "at": now, "browser_pid": record["pid"], "cdp_ready": cdp_ready}
    elif cdp_ready:
        state = {"status": "unrecorded_cdp_owner", "at": now, "cdp_ready": True}
    else:
        # Check again immediately before mutation so the deadline cannot race
        # the slower network/identity inspections above.
        if not may_recover(base, time.time()):
            return {"status": "stopped_for_deadline_or_pause", "at": time.time()}
        previous = base / "browser-supervisor-history" / f"browser-{time.time_ns()}.json"
        write_json(previous, record)
        child = start_process(base, "brave", command, env=owner_environment(base), ordinary_user=True)
        state = {"status": "browser_restarted", "at": time.time(), "old_browser_pid": record["pid"],
                 "browser_pid": child.pid, "cdp_ready": False, "previous_record": str(previous.relative_to(base))}
    # CDP observers retain target IDs. Reattach after a browser restart only
    # once the pool has recreated all tabs; otherwise later tabs go unobserved.
    observer = read(base / "cdp-observer-process.json")
    if cdp_ready and observer and observer.get("started_at", 0) < record.get("started_at", 0):
        with LOCAL_HTTP.open(f"http://127.0.0.1:{port}/json/list", timeout=3) as response:
            targets = json.load(response)
        pool_size = int(runtime.get("configuration", {}).get("BROWSER_POOL_SIZE", 5))
        pool_tabs = [target for target in targets if "kpool=" in target.get("url", "")
                     and target.get("webSocketDebuggerUrl")]
        if len(pool_tabs) >= pool_size and may_recover(base, time.time()):
            if service := identify(base, "cdp-observer-process.json", "monitor_cdp_network.py"):
                stop(service)
            if may_recover(base, time.time()):
                child = start_process(base, "cdp-observer", observer["args"],
                                      env=owner_environment(base), ordinary_user=True)
                state["reattached_observer_pid"] = child.pid
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=10)
    args = parser.parse_args()
    if os.geteuid() != 0 or args.interval <= 0:
        parser.error("use sudo and a positive interval")
    base = args.base.resolve(strict=True)
    with (base / "browser-supervisor.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = None
        while True:
            try:
                state = tick(base)
            except Exception as error:
                state = {"status": "supervisor_error", "at": time.time(),
                         "error_type": type(error).__name__,
                         "error_sha256": hashlib.sha256(str(error).encode()).hexdigest()}
            write_json(base / "browser-supervisor-state.json", state)
            key = {k: v for k, v in state.items() if k != "at"}
            if key != previous:
                with (base / "browser-supervisor-events.jsonl").open("a") as stream:
                    stream.write(json.dumps(state) + "\n")
                print(json.dumps(state), flush=True)
                previous = key
            if state["status"] == "stopped_for_deadline_or_pause":
                return
            time.sleep(args.interval)


if __name__ == "__main__":
    main()

"""Filesystem and process helpers for explicitly non-Docker benchmark runs."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import time


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def clone_root(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Reflinks preserve independent inodes; hard links could let a verifier's
    # package installation or a writable file corrupt another attempt.
    subprocess.run(["cp", "-a", "--reflink=auto", str(source), str(target)], check=True)


def copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)


def select_python(root: Path, minor: str) -> None:
    for name in ("python", "python3", "pip", "pip3"):
        target = root / "usr/local/bin" / name
        target.unlink(missing_ok=True)
        executable = "python" + minor if name.startswith("python") else "pip" + minor
        target.symlink_to(f"/opt/python/{minor}/bin/{executable}")
    # Some upstream verifiers intentionally use this absolute Ubuntu path.
    # Provision it in the environment instead of editing the verifier.
    target = root / "usr/bin/python3"
    target.unlink(missing_ok=True)
    target.symlink_to(f"/opt/python/{minor}/bin/python{minor}")


def shell_command(root: Path, command: str, *, uid: int = 0,
                  environment: dict[str, str] | None = None,
                  cpus: int | None = None, cpu_offset: int = 0) -> list[str]:
    env = {
        "PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root" if uid == 0 else "/home/agent",
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        **(environment or {}),
    }
    args = ["chroot", f"--userspec={uid}:{uid}", f"--groups={uid}", str(root),
            "/usr/bin/env", "-i", *(f"{key}={value}" for key, value in env.items())]
    if uid == 0:
        args += ["/usr/bin/setpriv", "--bounding-set=-sys_chroot,-net_raw"]
    args += ["/bin/bash", "-c", command]
    if cpus:
        available = sorted(os.sched_getaffinity(0))
        selected = available[cpu_offset:cpu_offset + cpus]
        if len(selected) != cpus:
            raise ValueError("insufficient disjoint CPUs for paired evaluation")
        args = ["taskset", "-c", ",".join(map(str, selected)), *args]
    return args


def kill_uid(uid: int) -> None:
    """Clean only the unique, otherwise unused UID allocated to an attempt."""
    if uid < 20000 or uid > 29999:
        raise ValueError("attempt cleanup requires a dedicated evaluation UID")
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            lines = (directory / "status").read_text().splitlines()
            actual = next(int(line.split()[1]) for line in lines if line.startswith("Uid:"))
            if actual == uid:
                os.kill(int(directory.name), signal.SIGKILL)
        except (OSError, StopIteration):
            continue


def allocate_uid(base: Path) -> int:
    """Persist allocation across stages so a new run cannot reuse a live UID."""
    with (base / "uid-allocation.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = base / "next-uid.json"
        uid = json.loads(path.read_text())["next"] if path.exists() else 20000
        used = set()
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                used.add(next(int(line.split()[1]) for line in (proc / "status").read_text().splitlines()
                              if line.startswith("Uid:")))
            except (OSError, StopIteration):
                pass
        while uid in used or uid == 29000:  # Reserved for the native smoke.
            uid += 1
        if uid > 29999:
            raise RuntimeError("evaluation UID allocation exhausted")
        write_json(path, {"next": uid + 1})
        return uid


def run_logged(args: list[str], log: Path, timeout: float | None,
               state: Path | None = None, uid: int | None = None) -> dict:
    started = time.time()
    timed_out = False
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as output:
        child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        if state:
            write_json(state, {"pid": child.pid, "started_at": started, "command": args})
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(child.pid, signal.SIGTERM)
            try:
                code = child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                code = child.wait(timeout=5)
        finally:
            if uid is not None:
                kill_uid(uid)
    return {"exit_code": code, "timed_out": timed_out, "timeout_seconds": timeout, "started_at": started,
            "finished_at": time.time(), "duration_seconds": round(time.time() - started, 3)}


def copy_artifact(agent: Path, verifier: Path, absolute: str) -> dict[str, str]:
    relative = Path(absolute.lstrip("/"))
    if not absolute.startswith("/app/") or ".." in relative.parts:
        raise ValueError(f"artifact outside /app: {absolute}")
    source = agent / relative
    # Reject symlink ancestors before asking is_file/is_dir: otherwise an
    # absolute link could resolve against the host during artifact collection.
    for length in range(1, len(relative.parts) + 1):
        candidate = agent.joinpath(*relative.parts[:length])
        if candidate.is_symlink():
            raise ValueError(f"artifact symlink: {absolute}")
    if not source.exists():
        raise FileNotFoundError(absolute)
    files = list(source.rglob("*")) if source.is_dir() else [source]
    for path in files:
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"unsupported artifact entry: {path.relative_to(agent)}")
    destination = verifier / relative
    if source.is_dir():
        copy_tree(source, destination)
        return {str(relative / name): digest for name, digest in file_hashes(source).items()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {str(relative): hashlib.sha256(source.read_bytes()).hexdigest()}


def reward_result(root: Path, process: dict) -> dict:
    path = root / "logs/verifier/reward.txt"
    if process["timed_out"]:
        return {"score": None, "infrastructure_error": "verifier_timeout"}
    if not path.is_file():
        return {"score": None, "infrastructure_error": "missing_reward"}
    value = path.read_text().strip()
    if value not in {"0", "1"}:
        return {"score": None, "infrastructure_error": "invalid_reward"}
    if value == "1" and process["exit_code"] != 0:
        return {"score": None, "infrastructure_error": "reward_exit_disagreement"}
    return {"score": int(value), "infrastructure_error": None}


def cc_result(path: Path) -> dict | None:
    result = None
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            result = {key: event.get(key) for key in
                      ("subtype", "is_error", "num_turns", "duration_ms", "usage",
                       "stop_reason", "session_id", "result", "modelUsage")}
    return result


def bridge_activity(path: Path, started: float, finished: float, effort: str) -> dict:
    records = []
    if path.is_file():
        for line in path.read_text(errors="replace").splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    ids = {item["id"] for item in records if item.get("event") == "browser_start"
           and started <= item.get("ts", 0) <= finished
           and item.get("reasoning_effort") == effort}
    selected = [item for item in records if item.get("id") in ids]
    terminals = {item["id"]: item for item in selected
                 if item.get("event") in {"browser_done", "browser_error", "browser_cancelled"}}
    return {"request_ids": sorted(ids), "logical_requests": len(ids),
            "upstream_attempts": sum(x.get("event") == "browser_start" for x in selected),
            "retries": [x for x in selected if x.get("event") == "upstream_retry"],
            "errors": [x for x in selected if x.get("event") == "browser_error"],
            "cancelled": [x for x in selected if x.get("event") == "browser_cancelled"],
            "unfinished": sorted(ids - terminals.keys()),
            "actual_efforts": sorted({x["reasoning_effort"] for x in selected
                                      if x.get("event") == "browser_start"}),
            "usage_by_request": {key: item.get("usage", {}) for key, item in terminals.items()}}

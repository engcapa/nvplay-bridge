"""Run an exact PATH Claude binary with PRoot and a real, isolated ordinary UID.

PRoot translates paths, but is not a security boundary. Real Unix permissions
protect benchmark sources and verifier roots. No process may retain the private
repository as its host cwd or an inherited descriptor.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from native_eval_support import clone_root, write_json


GUEST_CLI = "/opt/path-claude/claude"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_runtime(spec: dict) -> dict:
    if spec.get("kind") != "path-native-proot":
        raise ValueError("unsupported native CC runtime")
    for path_key, hash_key in (("snapshot_path", "sha256"), ("proot_path", "proot_sha256")):
        path = Path(spec[path_key])
        if not path.is_file() or sha256(path) != spec[hash_key]:
            raise RuntimeError(f"native CC runtime identity changed: {path_key}")
    return dict(spec)


def uid_processes(uid: int) -> list[int]:
    if not 20000 <= uid <= 29999:
        raise ValueError("native CC needs a dedicated evaluation UID")
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            # A zombie cannot access files and will be reaped by its parent.
            if (proc / "stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                continue
            if any(line.startswith("Uid:") and int(line.split()[1]) == uid
                   for line in (proc / "status").read_text().splitlines()):
                found.append(int(proc.name))
        except OSError:
            continue
    return found


def create_agent_root(source: Path, output: Path, uid: int, spec: dict) -> Path:
    verify_runtime(spec)
    if uid_processes(uid):
        raise RuntimeError("native CC UID is already active")
    canonical = output.resolve() / "agent-root"
    if canonical.exists() or canonical.is_symlink() or (output / "runtime-root.json").exists():
        raise FileExistsError(canonical)
    holder = Path(tempfile.mkdtemp(prefix=f"nvplay-bridge-cc-{uid}-", dir="/var/tmp"))
    os.chown(holder, 0, uid)
    holder.chmod(0o750)
    root = holder / "root"
    write_json(output / "runtime-root.json", {"kind": spec["kind"], "uid": uid,
               "root": str(root), "canonical_root": str(canonical), "phase": "preparing"})
    clone_root(source, root)
    os.chown(root, 0, uid)
    root.chmod(0o750)
    target = root / GUEST_CLI.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(spec["snapshot_path"], target)
    target.chmod(0o755)
    if sha256(target) != spec["sha256"]:
        raise RuntimeError("PATH Claude binary changed while copying")
    # The inherited base contains a legacy Node CLI. Every invocation of
    # `claude`, including model-issued shell commands, must select this binary.
    wrapper = root / "usr/local/bin/claude"
    wrapper.unlink(missing_ok=True)
    wrapper.write_text(f'#!/bin/sh\nexec {GUEST_CLI} "$@"\n')
    wrapper.chmod(0o755)
    marker = json.loads((output / "runtime-root.json").read_text())
    write_json(output / "runtime-root.json", {**marker, "phase": "active"})
    return root


def runtime_root(output: Path) -> tuple[Path, int | None]:
    marker_path = output / "runtime-root.json"
    if not marker_path.exists():
        return output / "agent-root", None
    marker = json.loads(marker_path.read_text())
    uid = marker["uid"]
    if not 20000 <= uid <= 29999 or marker.get("kind") != "path-native-proot":
        raise ValueError("invalid native CC root marker")
    if marker["canonical_root"] != str(output.resolve() / "agent-root"):
        raise ValueError("native CC root marker belongs to another attempt")
    if marker["phase"] == "returned":
        return output / "agent-root", uid
    root = Path(marker["root"])
    holder = root.parent
    if (holder.parent != Path("/var/tmp") or not holder.name.startswith(f"nvplay-bridge-cc-{uid}-")
            or root.name != "root" or holder.is_symlink() or root.is_symlink()):
        raise ValueError("invalid native CC external root")
    if holder.stat().st_uid != 0 or holder.stat().st_gid != uid or holder.stat().st_mode & 0o007:
        raise ValueError("native CC root permissions changed")
    return root, uid


def return_agent_root(output: Path) -> Path:
    root, uid = runtime_root(output)
    canonical = output / "agent-root"
    if root == canonical:
        return root
    if uid_processes(uid):
        raise RuntimeError("refusing to move a live native CC root")
    if canonical.exists() or canonical.is_symlink():
        raise FileExistsError(canonical)
    root.rename(canonical)
    root.parent.rmdir()
    marker = json.loads((output / "runtime-root.json").read_text())
    write_json(output / "runtime-root.json", {**marker, "phase": "returned", "returned_at": time.time()})
    return canonical


def shell_command(root: Path, command: str, *, uid: int, spec: dict,
                  environment: dict | None = None, cpus: int | None = None, cpu_offset: int = 0) -> list[str]:
    if not 20000 <= uid <= 29999:
        raise ValueError("native CC must run under a real dedicated ordinary UID")
    env = {"PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/home/agent",
           "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
           **(environment or {})}
    args = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "--no-new-privs",
            spec["proot_path"], "-r", str(root), "-b", "/proc", "-w", "/app",
            "/usr/bin/env", "-i", *(f"{key}={value}" for key, value in env.items()),
            "/bin/bash", "-c", command]
    if cpus:
        available = sorted(os.sched_getaffinity(0))
        selected = available[cpu_offset:cpu_offset + cpus]
        if len(selected) != cpus:
            raise ValueError("insufficient disjoint CPUs for paired evaluation")
        args = ["taskset", "-c", ",".join(map(str, selected)), *args]
    return args

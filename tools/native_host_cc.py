"""Execute CC directly on this host with a private UID, home and Python venv."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from native_eval_support import copy_tree, file_hashes, write_json


KIND = "path-native-host"
APP_PATH = Path("/app")
SOLUTION_PATH = Path("/solution")
LEASE_PATH = Path("/var/tmp/nvplay-bridge-host-app.lock")
_leases = {}


def tree_identity(path: Path) -> dict:
    files = file_hashes(path)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"tree_sha256": digest, "files": len(files)}


def prepare_toolchain(base: Path, snapshot: Path) -> dict:
    toolchain = Path(tempfile.mkdtemp(prefix="nvplay-bridge-host-runtime-", dir="/var/tmp"))
    toolchain.chmod(0o755)
    (toolchain / "bin").mkdir()
    shutil.copy2(snapshot, toolchain / "bin/claude")
    shutil.copy2(base / "native-base/usr/local/bin/node", toolchain / "bin/node")
    for minor in ("3.11", "3.12"):
        copy_tree(base / "native-base/opt/python" / minor, toolchain / "python" / minor)
    result = {"host_toolchain": str(toolchain), "host_cli": str(toolchain / "bin/claude"),
              "host_toolchain_identity": tree_identity(toolchain),
              "host_python": {minor: str(toolchain / "python" / minor / "bin" / f"python{minor}")
                              for minor in ("3.11", "3.12")},
              "host_os_release": Path("/etc/os-release").read_text(),
              "host_gcc_version": subprocess.check_output(["gcc", "--version"], text=True).splitlines()[0],
              "host_libc_version": subprocess.check_output(["ldd", "--version"], text=True).splitlines()[0],
              "agent_concurrency": 1, "app_path": str(APP_PATH), "path_translation": False,
              "host_filesystem_visible": True, "real_proc_binding": False,
              "isolation_boundary": "Real dedicated Unix UID and protected source directories; shared host filesystem, kernel and network"}
    write_json(base / "host-toolchain-manifest.json", result)
    return result


def verify_runtime(spec: dict) -> dict:
    if tree_identity(Path(spec["host_toolchain"])) != spec["host_toolchain_identity"]:
        raise RuntimeError("host toolchain identity changed")
    with Path(spec["host_cli"]).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != spec["sha256"]:
            raise RuntimeError("host Claude identity changed")
    return dict(spec)


def _link(alias: Path, target: Path):
    if alias.exists() or alias.is_symlink():
        raise RuntimeError(f"host path is occupied; refusing to replace it: {alias}")
    alias.symlink_to(target)


def claim_workspace(root: Path):
    descriptor = os.open(LEASE_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    stream = os.fdopen(descriptor, "a+")
    try:
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise RuntimeError("host workspace lock has a different owner")
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _link(APP_PATH, root / "app")
    except BaseException:
        stream.close()
        raise
    _leases[str(root)] = stream


def link_solution(root: Path):
    if str(root) not in _leases:
        raise RuntimeError("host solution requires the active workspace lease")
    _link(SOLUTION_PATH, root / "solution")


def release_workspace(root: Path):
    # Used both by the owning controller and reboot recovery after it has died.
    for alias, target in ((SOLUTION_PATH, root / "solution"), (APP_PATH, root / "app")):
        if alias.is_symlink() and alias.readlink() == target:
            alias.unlink()
        elif alias.exists() or alias.is_symlink():
            raise RuntimeError(f"host workspace alias belongs to another owner: {alias}")
    if stream := _leases.pop(str(root), None):
        stream.close()


def create_agent_root(source: Path, output: Path, uid: int, spec: dict) -> Path:
    from native_path_cc import uid_processes

    verify_runtime(spec)
    if uid_processes(uid):
        raise RuntimeError("host CC UID is already active")
    canonical = output.resolve() / "agent-root"
    if canonical.exists() or canonical.is_symlink() or (output / "runtime-root.json").exists():
        raise FileExistsError(canonical)
    holder = Path(tempfile.mkdtemp(prefix=f"nvplay-bridge-cc-{uid}-", dir="/var/tmp"))
    os.chown(holder, 0, uid)
    holder.chmod(0o750)
    root = holder / "root"
    root.mkdir(mode=0o750)
    os.chown(root, 0, uid)
    write_json(output / "runtime-root.json", {"kind": KIND, "uid": uid, "root": str(root),
               "canonical_root": str(canonical), "phase": "preparing"})
    copy_tree(source / "app", root / "app")
    (root / "home/agent").mkdir(parents=True)
    (root / "etc").mkdir()
    (root / "etc/passwd").write_text("agent:x:1000:1000::/home/agent:/bin/bash\n")
    (root / "etc/group").write_text("agent:x:1000:\n")
    python_link = os.readlink(source / "usr/local/bin/python3")
    minor = Path(python_link).parts[3]
    interpreter = spec["host_python"][minor]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PIP_CONFIG_FILE": "/dev/null"}
    subprocess.run([interpreter, "-m", "venv", str(root / "venv")], env=env, check=True,
                   stdout=subprocess.DEVNULL)
    ready = source.parent / "agent-ready.json"
    packages = json.loads(ready.read_text())["recipe"].get("agent_packages", []) if ready.is_file() else []
    if packages:
        with (output / "host-packages.log").open("wb") as log:
            subprocess.run([str(root / "venv/bin/python"), "-m", "pip", "install", "--no-index",
                            "--find-links", str(source / "opt/native-wheels"), *packages],
                           env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    for directory in (root / "app", root / "home/agent", root / "venv"):
        subprocess.run(["chown", "-R", f"{uid}:{uid}", str(directory)], check=True)
    claim_workspace(root)
    marker = json.loads((output / "runtime-root.json").read_text())
    write_json(output / "runtime-root.json", {**marker, "phase": "active", "python_minor": minor,
               "agent_packages": packages, "host_app_alias": str(APP_PATH)})
    return root


def shell_command(root: Path, command: str, *, uid: int, spec: dict,
                  environment: dict | None = None, cpus: int | None = None, cpu_offset: int = 0) -> list[str]:
    if not 20000 <= uid <= 29999:
        raise ValueError("host CC must use a dedicated ordinary UID")
    env = {"PATH": f"{root}/venv/bin:{spec['host_toolchain']}/bin:/usr/local/bin:/usr/bin:/bin",
           "HOME": str(root / "home/agent"), "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1",
           "PYTHONDONTWRITEBYTECODE": "1", **(environment or {})}
    args = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "--no-new-privs",
            "/usr/bin/env", "-i", "--chdir", str(root), *(f"{key}={value}" for key, value in env.items()),
            "/bin/bash", "--noprofile", "--norc", "-c", command]
    if cpus:
        selected = sorted(os.sched_getaffinity(0))[cpu_offset:cpu_offset + cpus]
        if len(selected) != cpus:
            raise ValueError("insufficient disjoint CPUs for evaluation")
        args = ["taskset", "-c", ",".join(map(str, selected)), *args]
    return args

"""Rebuild the pinned, non-Docker CC benchmark base; private inputs stay in logs/."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import urllib.request
import zipfile

from native_eval_support import select_python, write_json


DOWNLOADS = {
    "terminal-bench.tar.gz": (
        "https://api.github.com/repos/harbor-framework/terminal-bench/tarball/452bf305c6daa62fc59061d22133a7cbc7c1572e",
        "61530c06fc0069abc5917e386e888672e1a10950d40a061fa9c1daf52eb1935f"),
    "ubuntu-base.tar.gz": (
        "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04.5/release/ubuntu-base-24.04.5-base-amd64.tar.gz",
        "e77b6f10c2590cef872b33ee9f635a0e3fd1f57fb074c0e52b5c7f56147a0c86"),
    "claude-code-2.1.110.tgz": (
        "https://registry.npmjs.org/@anthropic-ai/claude-code/-/claude-code-2.1.110.tgz",
        "a9e68dbae2b27893bee13b019cff6417ac9db5947cbc209da1da86b895f76a58"),
}
PYTHONS = {"3.11": "3.11.18", "3.12": "3.12.16", "3.13": "3.13.18"}
NODE_SHA256 = "53fb205ae78805130177e24bcb459a69a1518c8d98f8965f31d85aae7ea840fc"
CLI_SHA256 = "cc686e832fdfb97841608875a918043db5d565a2110821b8fc4cd9fad12ea861"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked(command, log: Path, *, env=None, cwd=None):
    print(json.dumps({"running": shlex.join(map(str, command)), "log": str(log)}), flush=True)
    with log.open("ab") as output:
        subprocess.run(list(map(str, command)), check=True, stdout=output,
                       stderr=subprocess.STDOUT, env=env, cwd=cwd)


def downloads(base: Path, proxy: str):
    for name, (url, expected) in DOWNLOADS.items():
        target = base / name
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".part")
            checked(["curl", "--proxy", proxy, "--connect-timeout", "30", "--max-time", "7200",
                     "--retry", "4", "--fail", "--location", "--silent", "--show-error",
                     "--output", temporary, url], base / "downloads.log")
            if digest(temporary) != expected:
                raise ValueError(f"download checksum mismatch: {name}")
            temporary.rename(target)
        if digest(target) != expected:
            raise ValueError(f"cached download checksum mismatch: {name}")
    dataset = base / "terminal-bench"
    if not dataset.exists():
        temporary = base / "terminal-bench.unpacking"
        temporary.mkdir(exist_ok=True)
        checked(["tar", "-xzf", base / "terminal-bench.tar.gz", "--strip-components=1",
                 "-C", temporary], base / "downloads.log")
        temporary.rename(dataset)
    write_json(base / "download-manifest.json", {
        name: {"url": url, "sha256": expected, "size": (base / name).stat().st_size}
        for name, (url, expected) in DOWNLOADS.items()})


def build_base(base: Path, proxy: str, node: Path, uv_wheel: Path):
    if os.geteuid() != 0:
        raise PermissionError("base provisioning requires sudo; agents run as ordinary UIDs")
    if digest(node) != NODE_SHA256:
        raise ValueError("Node binary differs from the previously recorded v24.13.0 binary")
    root = base / "native-base"
    log = base / "native-base-install.log"
    marker = base / "native-base-ready.json"
    if marker.exists():
        raise FileExistsError(f"base already frozen: {marker}")
    if not root.exists():
        root.mkdir()
        checked(["tar", "-xpf", base / "ubuntu-base.tar.gz", "-C", root], log)
    for name, minor in (("null", 3), ("zero", 5), ("random", 8), ("urandom", 9)):
        target = root / "dev" / name
        if not target.exists():
            checked(["mknod", target, "c", "1", str(minor)], log)
        target.chmod(0o666)
    (root / "tmp").chmod(0o1777)
    shutil.copyfile("/etc/resolv.conf", root / "etc/resolv.conf")
    policy = root / "usr/sbin/policy-rc.d"
    policy.write_text("#!/bin/sh\nexit 101\n")
    policy.chmod(0o755)
    apt_env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    apt = ["chroot", root, "apt-get", "-o", f"Acquire::http::Proxy={proxy}",
           "-o", f"Acquire::https::Proxy={proxy}", "-o", "Acquire::Retries=4",
           "-o", "Acquire::Languages=none"]
    checked(apt + ["update"], log, env=apt_env)
    checked(apt + ["install", "-y", "--no-install-recommends", "build-essential", "make", "patch",
                   "git", "curl", "ca-certificates", "tmux", "file", "util-linux", "procps"], log, env=apt_env)
    if not any(line.startswith("agent:") for line in (root / "etc/passwd").read_text().splitlines()):
        checked(["chroot", root, "useradd", "--uid", "1000", "--create-home", "--shell", "/bin/bash", "agent"], log)
    (root / "app").mkdir(exist_ok=True)
    (root / "usr/local/bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(node, root / "usr/local/bin/node")
    uv_unpack = base / "uv-wheel"
    uv_unpack.mkdir(exist_ok=True)
    with zipfile.ZipFile(uv_wheel) as bundle:
        bundle.extractall(uv_unpack)
    uv = next(path for path in uv_unpack.rglob("uv") if path.is_file())
    uv.chmod(0o755)
    env = {**os.environ, "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
           "NO_PROXY": "127.0.0.1,localhost", "UV_PYTHON_INSTALL_DIR": str(base / "python"),
           "UV_PYTHON_BIN_DIR": str(base / "python-bin")}
    checked([uv, "python", "install", *PYTHONS.values()], base / "python-install.log", env=env)
    for minor, version in PYTHONS.items():
        source = base / "python" / f"cpython-{version}-linux-x86_64-gnu"
        if not source.exists():
            raise FileNotFoundError(f"uv did not install the pinned Python: {source}")
        target = root / "opt/python" / minor
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copytree(source, target, symlinks=True)
    shutil.copy2(uv, root / "usr/local/bin/uv")
    package = root / "opt/claude-code-2.1.110"
    package.mkdir(parents=True, exist_ok=True)
    checked(["tar", "-xzf", base / "claude-code-2.1.110.tgz", "-C", package], log)
    cli = package / "package/cli.js"
    if digest(cli) != CLI_SHA256:
        raise ValueError("official CC CLI differs from the previously recorded source")
    wrapper = root / "usr/local/bin/claude"
    wrapper.write_text('#!/bin/sh\nexec /usr/local/bin/node /opt/claude-code-2.1.110/package/cli.js "$@"\n')
    wrapper.chmod(0o755)
    select_python(root, "3.12")
    # Every task receives its own wheel directory before pip runs. Keep setup offline.
    (root / "etc/pip.conf").write_text("[global]\nno-index = true\nfind-links = /opt/native-wheels\n")
    manifest = {"recorded_at": time.time(), "rootfs_release": "Ubuntu base 24.04.5 amd64",
                "rootfs_sha256": DOWNLOADS["ubuntu-base.tar.gz"][1], "managed_python": PYTHONS,
                "cc_cli_sha256": digest(cli), "node_sha256": digest(root / "usr/local/bin/node"),
                "node_version": "24.13.0", "uv_wheel_sha256": digest(uv_wheel),
                "dpkg": subprocess.check_output(["chroot", root, "dpkg-query", "-W"], text=True),
                "proxy": proxy, "docker_processes_started": 0}
    write_json(base / "native-base-manifest.json", manifest)
    write_json(marker, {"prepared_at": time.time(), "manifest": "native-base-manifest.json"})


def wheels(base: Path, proxy: str):
    cache = base / "wheel-cache"
    cache.mkdir(exist_ok=True)
    env = {**os.environ, "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "NO_PROXY": "127.0.0.1,localhost"}
    sets = [
        ("3.11", ["numpy==1.26.4", "pytest==9.1.1", "pytest-json-ctrf==0.5.2"]),
        ("3.12", ["numpy==1.26.4", "scipy==1.11.4", "pytest==9.1.1", "pytest-json-ctrf==0.5.2"]),
        ("3.12", ["numpy==2.1.3", "scipy==1.14.1"]),
    ]
    for minor, packages in sets:
        checked(["python3", "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
                 "--python-version", minor, "--platform", "manylinux2014_x86_64", "--implementation", "cp",
                 "--abi", "cp" + minor.replace(".", ""), "--dest", cache, *packages],
                base / "wheel-download.log", env=env)
    write_json(cache / "offline-3.12.json", {"created_at": time.time()})
    write_json(base / "wheel-manifest.json", {path.name: {"sha256": digest(path), "size": path.stat().st_size}
                                             for path in sorted(cache.glob("*.whl"))})


def configure(base: Path, proxy: str):
    from copy import deepcopy
    from eval_terminal_native import DEFAULT_PILOT, NODE_NETWORK_DIRECTORY, initialize_plan, network_tree_identity

    path = base / "evaluation-plan.json"
    if path.exists():
        raise FileExistsError("the plan is already frozen; do not replace it")
    repo = Path(__file__).resolve().parents[1]
    historical = json.loads((repo / "docs/evidence/cc-bridge-native-2026-11-10.json").read_text())
    formal = deepcopy(historical["protocol"]["formal_fixed"])
    formal["frozen_at"] = time.time()
    installed = network_tree_identity(NODE_NETWORK_DIRECTORY)
    if installed != {key: formal["node_network_adapter"][key] for key in ("tree_sha256", "files")}:
        raise ValueError("Node network installation differs from the archived protocol")
    runtime = json.loads((base / "bridge-runtime.json").read_text())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{runtime["bridge_port"]}/health', timeout=5) as response:
        health = json.load(response)
    if (health.get("browser_output_tokens") != {"maximum": 131072, "fixed": 131072}
            or health.get("browser_timeouts") != {"total_seconds": 28800, "first_byte_seconds": 3600,
                                                  "idle_seconds": 3600}):
        raise ValueError("live bridge does not implement the frozen 128K/long-wait protocol")
    initialize_plan(base, DEFAULT_PILOT)
    plan = json.loads(path.read_text())
    for task, files in plan["task_identities"].items():
        if hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() != historical["protocol"]["task_manifest_sha256"][task]:
            raise ValueError(f"restored task differs from historical manifest: {task}")
    plan["formal_native_subset"] = formal
    plan["bridge_runtime_by_stage"] = {formal["stage"]: runtime}
    plan["restart_relationship"] = {
        "recorded_at": time.time(), "historical_archive": "docs/evidence/cc-bridge-native-2026-11-10.json",
        "historical_complete_observations_preserved": 12, "historical_user_interruptions_excluded": 2,
        "new_full_formal_attempts": 30, "new_sessions_and_roots": True, "no_scored_feedback": True,
        "same_five_tasks_and_equal_effort_limits": True}
    for effort in ("high", "max"):
        settings = json.loads((repo / f"configs/settings-kimi{'-high' if effort == 'high' else ''}.json").read_text())
        settings["env"].update({"ANTHROPIC_BASE_URL": f'http://127.0.0.1:{runtime["bridge_port"]}',
            "API_TIMEOUT_MS": "29400000", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000",
            "CLAUDE_CODE_EXTRA_BODY": json.dumps({"max_tokens": 131072, "output_config": {"effort": effort}}),
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "NO_PROXY": "127.0.0.1,localhost"})
        write_json(base / f"settings-formal-{effort}.json", settings)
    write_json(path, plan)
    resources = {"recorded_at": time.time(), "cpu_affinity": sorted(os.sched_getaffinity(0)),
                 "docker_processes_started": 0}
    for name in ("cpu.max", "memory.max"):
        source = Path("/sys/fs/cgroup") / name
        if source.exists():
            resources[name] = source.read_text().strip()
    write_json(base / "host-resources.json", resources)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("downloads", "base", "wheels", "configure"))
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--proxy", default="http://127.0.0.1:31128")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--uv-wheel", type=Path)
    args = parser.parse_args()
    base = args.base.resolve()
    base.mkdir(parents=True, exist_ok=True)
    if args.action == "downloads":
        downloads(base, args.proxy)
    elif args.action == "wheels":
        wheels(base, args.proxy)
    elif args.action == "configure":
        configure(base, args.proxy)
    else:
        if not args.node or not args.uv_wheel:
            parser.error("base requires --node and --uv-wheel")
        build_base(base, args.proxy, args.node.resolve(), args.uv_wheel.resolve())


if __name__ == "__main__":
    main()

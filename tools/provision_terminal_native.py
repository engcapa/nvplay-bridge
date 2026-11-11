"""Rebuild the pinned, non-Docker CC benchmark base; private inputs stay in logs/."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
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
# The security index briefly advertised .7 before both dependency archives were
# available. Keep the .6 family used by the archived benchmark together.
APT_FIXED = [f"{name}=5.38.2-3.2ubuntu0.6" for name in
             ("perl", "perl-base", "perl-modules-5.38", "libperl5.38t64")]
APT_ARCHIVE_BASE_URLS = (
    "https://archive.ubuntu.com/ubuntu/",
    "https://snapshot.ubuntu.com/ubuntu/20261110T000000Z/",
)


def fixed_apt_archive(package: str, description: str, archived: dict) -> dict:
    """An old package can disappear from current indexes without changing our pin."""
    name, version = package.split("=", 1)
    for stanza in description.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.splitlines() if ": " in line)
        if (fields.get("Package") == name and fields.get("Version") == version
                and fields.get("Filename") and fields.get("SHA256")):
            return {"filename": fields["Filename"], "sha256": fields["SHA256"],
                    "verification_basis": "current_authenticated_apt_index"}
    previous = archived.get(package)
    if not previous or not previous.get("filename") or not previous.get("sha256"):
        raise ValueError(f"pinned APT package missing from both current index and archived checksums: {package}")
    return {"filename": "pool/main/p/perl/" + Path(previous["filename"]).name,
            "sha256": previous["sha256"], "verification_basis": "archived_authenticated_apt_index_sha256"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked(command, log: Path, *, env=None, cwd=None):
    print(json.dumps({"running": shlex.join(map(str, command)), "log": str(log)}), flush=True)
    with log.open("ab") as output:
        subprocess.run(list(map(str, command)), check=True, stdout=output,
                       stderr=subprocess.STDOUT, env=env, cwd=cwd)


def download_fixed_apt_archive(fields: dict, target: Path, proxy: str, log: Path) -> str:
    temporary = target.with_suffix(".deb.part")
    for index, origin in enumerate(APT_ARCHIVE_BASE_URLS):
        url = origin + fields["filename"]
        try:
            checked(["curl", "--proxy", proxy, "--connect-timeout", "30", "--max-time", "600", "--retry", "4",
                     "--fail", "--location", "--silent", "--show-error", "--output", temporary, url], log)
        except subprocess.CalledProcessError as error:
            # Superseded security packages disappear from the live mirror.
            # The snapshot must still match the previously authenticated hash.
            if error.returncode != 22 or index == len(APT_ARCHIVE_BASE_URLS) - 1:
                raise
            continue
        if digest(temporary) != fields["sha256"]:
            raise ValueError(f"APT archive differs from authenticated index: {fields['filename']}")
        temporary.rename(target)
        return url
    raise RuntimeError("no pinned APT archive source succeeded")


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
    fixed_archives = {}
    archive_manifest = json.loads((Path(__file__).resolve().parents[1]
        / "docs/evidence/cc-bridge-resume-2026-11-10.json").read_text())["native_environment"]["fixed_apt_archives"]
    for package in APT_FIXED:
        description = subprocess.check_output(["chroot", root, "apt-cache", "show", package], text=True)
        fields = fixed_apt_archive(package, description, archive_manifest)
        target = root / "var/cache/apt/archives" / Path(fields["filename"]).name
        download_url = None
        if not target.exists() or digest(target) != fields["sha256"]:
            download_url = download_fixed_apt_archive(fields, target, proxy, log)
        actual = subprocess.check_output(["chroot", root, "dpkg-deb", "--show",
            "--showformat=${Package}=${Version}", "/var/cache/apt/archives/" + target.name], text=True)
        if actual != package:
            raise ValueError(f"downloaded APT package has unexpected identity: {package}")
        fixed_archives[package] = {"filename": target.name, "sha256": fields["sha256"],
                                   "verification_basis": fields["verification_basis"]}
        if download_url:
            fixed_archives[package]["download_url"] = download_url
    checked(apt + ["install", "-y", "--no-install-recommends", "build-essential", "make", "patch",
                   "git", "curl", "ca-certificates", "tmux", "file", "util-linux", "procps",
                   *("/var/cache/apt/archives/" + item["filename"] for item in fixed_archives.values())],
            log, env=apt_env)
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
                "fixed_apt_archives": fixed_archives,
                "proxy": proxy, "docker_processes_started": 0}
    write_json(base / "native-base-manifest.json", manifest)
    checked(["chroot", root, "apt-get", "clean"], log)
    shutil.rmtree(root / "var/lib/apt/lists")
    (root / "var/lib/apt/lists/partial").mkdir(parents=True)
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


def record_host_resources(base: Path):
    resources = {"recorded_at": time.time(), "cpu_affinity": sorted(os.sched_getaffinity(0)),
                 "docker_processes_started": 0}
    paths = {"cpu.max": "/sys/fs/cgroup/cpu.max", "memory.max": "/sys/fs/cgroup/memory.max",
             "host_cpu_quota": "/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
             "host_cpu_period": "/sys/fs/cgroup/cpu/cpu.cfs_period_us",
             "host_memory_limit_bytes": "/sys/fs/cgroup/memory/memory.limit_in_bytes"}
    for name, source in paths.items():
        source = Path(source)
        if source.exists():
            resources[name] = source.read_text().strip()
    write_json(base / "host-resources.json", resources)


def continue_schedule(formal: dict, checkpoint: Path) -> dict:
    previous = json.loads(checkpoint.read_text())
    protocol = previous["protocol"]
    from eval_terminal_native import DEFAULT_PILOT

    prior_stage = protocol.get("formal_native_subset", {}).get("stage")
    prior_runtime = protocol.get("cc_runtime_by_stage", {}).get(prior_stage, {})
    known_cli = protocol["cc_version"] == "2.1.110" or (
        prior_runtime.get("kind") in ("path-native-proot", "path-native-host")
        and prior_runtime.get("version") == protocol["cc_version"]
        and re.fullmatch(r"[0-9a-f]{64}", prior_runtime.get("sha256", "")) is not None)
    if (protocol["source_commit"] != "452bf305c6daa62fc59061d22133a7cbc7c1572e"
            or not known_cli or protocol["pilot_tasks"] != DEFAULT_PILOT):
        raise ValueError("continuation checkpoint uses a different dataset, CLI or task set")
    inherited = protocol.get("continuation", {})
    inherited_positions = inherited.get("preserved_positions", [])
    local_attempts = previous["results"]["attempts"]
    key = lambda row: (row["task"], row["repeat"], row["effort"])
    completed = {key(row) for row in (*inherited_positions, *local_attempts)}
    all_slots = [{"task": task, "repeat": repeat, "effort": effort}
                 for repeat in range(1, formal["repeats_per_effort"] + 1)
                 for task in formal["tasks"] for effort in ("high", "max")]
    if (inherited.get("preserved_completed_attempts", 0) != len(inherited_positions)
            or inherited.get("total_plan_positions", len(all_slots)) != len(all_slots)
            or previous.get("combined_plan_positions_completed", len(completed)) != len(completed)
            or len(completed) != len(inherited_positions) + len(local_attempts)
            or not completed.issubset({key(row) for row in all_slots})):
        raise ValueError("invalid or duplicate completed positions in continuation checkpoint")
    formal["schedule"] = [slot for slot in all_slots if key(slot) not in completed]
    if not formal["schedule"]:
        raise ValueError("there are no missing formal positions to continue")
    return {"source_checkpoint": str(checkpoint), "source_checkpoint_sha256": digest(checkpoint),
            "source_cc_version": protocol["cc_version"],
            "preserved_completed_attempts": len(completed), "total_plan_positions": len(all_slots),
            "preserved_positions": [slot for slot in all_slots if key(slot) in completed],
            "context_policy_changed": any(formal.get(k) != protocol.get("formal_native_subset", {}).get(k)
                                          for k in ("cc_model", "cc_auto_compact_window_tokens",
                                                    "automatic_compaction_enabled")),
            "prior_results_must_remain_in_original_stage": True, "old_raw_evidence_available": False,
            "unresolved_prior_pending": previous["results"].get("pending", []),
            "unresolved_pending_score": None, "unresolved_pending_excluded_from_model_grades": True}


def configure(base: Path, proxy: str, *, cc_model: str | None = None,
              compact_window: int | None = None, continuation_checkpoint: Path | None = None,
              no_agent_timeout: bool = False, stage: str | None = None):
    from copy import deepcopy
    from eval_terminal_native import DEFAULT_PILOT, NODE_NETWORK_DIRECTORY, initialize_plan, network_tree_identity

    path = base / "evaluation-plan.json"
    if path.exists():
        raise FileExistsError("the plan is already frozen; do not replace it")
    repo = Path(__file__).resolve().parents[1]
    historical = json.loads((repo / "docs/evidence/cc-bridge-native-2026-11-10.json").read_text())
    formal = deepcopy(historical["protocol"]["formal_fixed"])
    formal["frozen_at"] = time.time()
    if no_agent_timeout:
        formal["agent_wall_clock_timeout_seconds"] = None
        formal["budget_seconds_is_reference_only"] = True
    if bool(cc_model) != bool(compact_window):
        raise ValueError("a custom context model and its compaction window must be configured together")
    if cc_model:
        if cc_model != "kimi-k3.1-pro[1m]" or not 0 < compact_window <= 1000000:
            raise ValueError("this continuation supports the requested Kimi 1M context configuration")
        formal.update(stage="formal-1m-720k", cc_model=cc_model,
                      cc_model_context_window_tokens=1000000,
                      cc_auto_compact_window_tokens=compact_window, automatic_compaction_enabled=True)
    if stage is not None:
        if not re.fullmatch(r"formal-[A-Za-z0-9_-]+", stage):
            raise ValueError("a custom stage must start with formal- and contain only letters, digits, _ or -")
        formal["stage"] = stage
    continuation = continue_schedule(formal, continuation_checkpoint) if continuation_checkpoint else None
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
    if continuation:
        plan["continuation"] = continuation
    plan["bridge_runtime_by_stage"] = {formal["stage"]: runtime}
    plan["restart_relationship"] = {
        "recorded_at": time.time(), "historical_archive": "docs/evidence/cc-bridge-native-2026-11-10.json",
        "historical_complete_observations_preserved": 12, "historical_user_interruptions_excluded": 2,
        "new_full_formal_attempts": 30, "new_sessions_and_roots": True, "no_scored_feedback": True,
        "same_five_tasks_and_equal_effort_limits": True}
    if continuation:
        plan["restart_relationship"].update(
            new_full_formal_attempts=len(formal["schedule"]),
            original_plan_positions=continuation["total_plan_positions"],
            preserved_previous_completed=continuation["preserved_completed_attempts"],
            context_stage_changed=continuation["context_policy_changed"])
    for effort in ("high", "max"):
        settings = json.loads((repo / f"configs/settings-kimi{'-high' if effort == 'high' else ''}.json").read_text())
        settings["env"].update({"ANTHROPIC_BASE_URL": f'http://127.0.0.1:{runtime["bridge_port"]}',
            "API_TIMEOUT_MS": "29400000", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000",
            "CLAUDE_CODE_EXTRA_BODY": json.dumps({"max_tokens": 131072, "output_config": {"effort": effort}}),
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "NO_PROXY": "127.0.0.1,localhost"})
        if cc_model:
            settings["model"] = cc_model
            settings["env"].update({key: cc_model for key in (
                "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL")})
            settings["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(compact_window)
            for key in ("DISABLE_AUTO_COMPACT", "DISABLE_COMPACT", "CLAUDE_CODE_DISABLE_1M_CONTEXT"):
                settings["env"].pop(key, None)
        write_json(base / f"settings-formal-{effort}.json", settings)
    write_json(path, plan)
    record_host_resources(base)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("downloads", "base", "wheels", "configure", "resources"))
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--proxy", default="http://127.0.0.1:31128")
    parser.add_argument("--node", type=Path)
    parser.add_argument("--uv-wheel", type=Path)
    parser.add_argument("--cc-model")
    parser.add_argument("--auto-compact-window", type=int)
    parser.add_argument("--continue-checkpoint", type=Path)
    parser.add_argument("--stage", help="use a distinct formal stage name for the restored deployment")
    parser.add_argument("--no-agent-timeout", action="store_true",
                        help="wait for CC to exit naturally; retain the old budget only as a protocol reference")
    args = parser.parse_args()
    base = args.base.resolve()
    base.mkdir(parents=True, exist_ok=True)
    if args.action == "downloads":
        downloads(base, args.proxy)
    elif args.action == "wheels":
        wheels(base, args.proxy)
    elif args.action == "configure":
        configure(base, args.proxy, cc_model=args.cc_model, compact_window=args.auto_compact_window,
                  continuation_checkpoint=args.continue_checkpoint, no_agent_timeout=args.no_agent_timeout,
                  stage=args.stage)
    elif args.action == "resources":
        record_host_resources(base)
    else:
        if not args.node or not args.uv_wheel:
            parser.error("base requires --node and --uv-wheel")
        build_base(base, args.proxy, args.node.resolve(), args.uv_wheel.resolve())


if __name__ == "__main__":
    main()

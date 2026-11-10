"""Run explicitly adapted Terminal-Bench tasks with CC and no Docker.

This is a local, supervised evaluation driver, not the official Harbor harness.
Provision native-base separately, then run prepare, controls, and run as root.
Benchmark prompts, solutions, fixtures and generated artifacts stay in logs/.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import time
import tomllib

from native_eval_support import (
    allocate_uid, bridge_activity, cc_result, clone_root, copy_artifact, copy_tree,
    file_hashes, reward_result, run_logged, select_python, shell_command, write_json,
)


NODE_NETWORK_DIRECTORY = Path(__file__).with_name("cc_node_runtime").resolve()
TOOLING_SOURCES = (Path(__file__).resolve(), Path(__file__).with_name("native_eval_support.py").resolve(),
                   *(NODE_NETWORK_DIRECTORY / name for name in ("timeout.cjs", "package.json", "package-lock.json")))


def network_tree_identity(directory: Path) -> dict:
    files = file_hashes(directory)
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {"tree_sha256": hashlib.sha256(encoded).hexdigest(), "files": len(files)}


def process_identity(pid: int) -> dict | None:
    """Distinguish a live owner from a recycled PID, including after a reboot."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "start_ticks": fields[19]}
    except (OSError, IndexError):
        return None


RECIPES = {
    "session-window-debug": {"python": "3.12", "copies": {"app": "app/app"}},
    "wal-recovery-ordering": {"python": "3.13", "copies": {"app": "app"},
                              "verifier_packages": ["hypothesis==6.122.3"]},
    "batched-eval-parity": {"python": "3.11", "copies": {
        "evalbench": "app/evalbench", "model": "app/model", "data": "app/data"},
        "agent_packages": ["numpy==1.26.4"], "verifier_packages": ["numpy==1.26.4"],
        "environment": {"PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}},
    "mvcc-lsm-compaction": {"python": "3.12", "copies": {"app": "app"}},
    "ks-solver-cpp": {"python": "3.12", "copies": {"oracle.hpp": "app/oracle.hpp"},
                      "verifier_packages": []},
    "embedding-drift-monitor": {"python": "3.12", "copies": {
        "drift_monitor": "app/drift_monitor", "data": "app/data"},
        "agent_packages": ["numpy==1.26.4", "scipy==1.11.4"],
        "verifier_packages": ["numpy==2.1.3", "scipy==1.14.1"]},
}
COMMON_VERIFIER = ["pytest==9.1.1", "pytest-json-ctrf==0.5.2"]
DEFAULT_PILOT = ["session-window-debug", "embedding-drift-monitor", "batched-eval-parity",
                 "mvcc-lsm-compaction", "ks-solver-cpp"]


def source_task_hashes(archive: Path, dataset: Path, names: list[str]) -> dict:
    expected = {name: {} for name in names}
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            parts = member.name.split("/", 3)
            if len(parts) != 4 or parts[1] != "tasks" or parts[2] not in expected or member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"unsupported benchmark archive entry: {member.name}")
            with bundle.extractfile(member) as stream:
                expected[parts[2]][parts[3]] = hashlib.file_digest(stream, "sha256").hexdigest()
    for name, hashes in expected.items():
        if not hashes or file_hashes(dataset / name) != hashes:
            raise ValueError(f"extracted task differs from pinned archive: {name}")
    return expected


def initialize_plan(base: Path, names: list[str]) -> None:
    """Freeze inputs before exposing any selected task to the measured agent."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / "evaluation-plan.json"
    if path.exists():
        raise FileExistsError(path)
    archive = base / "terminal-bench.tar.gz"
    expected = "61530c06fc0069abc5917e386e888672e1a10950d40a061fa9c1daf52eb1935f"
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected:
        raise ValueError("Terminal-Bench 4.0.0 source archive checksum mismatch")
    write_json(path, {"created_at": time.time(), "dataset": "terminal-bench", "version": "4.0.0",
                      "source_commit": "452bf305c6daa62fc59061d22133a7cbc7c1572e",
                      "source_archive_sha256": digest, "docker_allowed": False,
                      "executor": "native chroot, shared host kernel/network, no /proc",
                      "cc_version": "2.1.110", "model": "moonshotai/kimi-k3.1-pro",
                      "efforts": ["high", "max"], "pilot_tasks": names,
                      "pilot_repeats": 1, "pilot_task_budget_seconds": 1800,
                      "formal_repeats": 3, "formal_task_budget_seconds": 28800,
                      "bridge_source_head": subprocess.check_output(
                          ["git", "rev-parse", "HEAD"], text=True).strip(),
                      "task_identities": source_task_hashes(archive, base / "terminal-bench/tasks", names)})


def attempt_batches(plan: dict, stage: str, repeats: int, names: list[str] | None = None) -> list:
    """Use frozen continuation slots without silently repeating earlier outcomes."""
    formal = plan.get("formal_native_subset", {})
    names = names or plan["pilot_tasks"]
    slots = formal.get("schedule") if stage == formal.get("stage") else None
    if slots is None:
        slots = [{"task": name, "repeat": repeat, "effort": effort}
                 for repeat in range(1, repeats + 1) for name in names for effort in plan["efforts"]]
    seen, batches = set(), []
    for slot in slots:
        name, repeat, effort = (slot[key] for key in ("task", "repeat", "effort"))
        identity = (name, repeat, effort)
        if (name not in plan["pilot_tasks"] or effort not in plan["efforts"]
                or type(repeat) is not int or not 1 <= repeat <= repeats or identity in seen):
            raise ValueError("invalid or duplicate frozen continuation slot")
        seen.add(identity)
        if name not in names:
            continue
        if not batches or batches[-1][:2] != (name, repeat):
            batches.append((name, repeat, []))
        batches[-1][2].append(effort)
    return batches


class Evaluation:
    def __init__(self, base: Path):
        self.base = base.resolve()
        self.plan = json.loads((self.base / "evaluation-plan.json").read_text())
        self.dataset = self.base / "terminal-bench/tasks"
        self._source_identity = {
            "identity_version": 2,
            "bridge_head": self.plan["bridge_source_head"],
            "tooling_repository_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True).strip(),
            "tooling_worktree_status": subprocess.check_output(
                ["git", "status", "--porcelain=v1", "--untracked-files=normal"], text=True).splitlines(),
            "runner_sha256": self._tooling_hashes(),
            "cc_version": self.plan["cc_version"],
            "dataset_commit": self.plan["source_commit"],
        }

    def _tooling_hashes(self) -> dict:
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in TOOLING_SOURCES}

    def identity(self, stage: str | None = None) -> dict:
        """Keep the process's initial source identity; refuse later source changes."""
        if self._tooling_hashes() != self._source_identity["runner_sha256"]:
            raise RuntimeError("runner source changed after initialization; start a new process before another attempt")
        identity = deepcopy(self._source_identity)
        runtime = self.plan.get("bridge_runtime_by_stage", {}).get(stage)
        if runtime:
            identity["bridge_head"] = runtime["source_head"]
            identity["bridge_runtime"] = deepcopy(runtime)
        return identity

    def task(self, name: str) -> dict:
        return tomllib.loads((self.dataset / name / "task.toml").read_text())

    def request_settings(self, effort: str, stage: str) -> dict:
        frozen = self.plan.get("formal_native_subset", {})
        formal = frozen if stage in ("formal", frozen.get("stage")) else {}
        explicit_output = formal.get("max_output_tokens")
        prefix = "settings-formal" if explicit_output else "settings"
        settings = json.loads((self.base / f"{prefix}-{effort}.json").read_text())
        if explicit_output:
            env = settings.get("env", {})
            extra = json.loads(env.get("CLAUDE_CODE_EXTRA_BODY", "{}"))
            if (extra.get("max_tokens") != explicit_output
                    or extra.get("output_config", {}).get("effort") != effort
                    or env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") != str(formal["cc_output_environment_tokens"])
                    or env.get("API_TIMEOUT_MS") != str(formal["cc_api_timeout_ms"])):
                raise ValueError("formal request settings do not match the frozen output/effort/timeout protocol")
            fixed = formal.get("bridge_fixed_max_output_tokens")
            runtime = self.plan.get("bridge_runtime_by_stage", {}).get(stage, {})
            if fixed and runtime.get("fixed_max_output_tokens") != fixed:
                raise ValueError("formal bridge runtime does not match the frozen fixed output protocol")
        if formal.get("cc_model"):
            env = settings.get("env", {})
            enabled = formal.get("automatic_compaction_enabled", False)
            if (settings.get("model") != formal["cc_model"]
                    or env.get("ANTHROPIC_MODEL") != formal["cc_model"]
                    or (enabled and (env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
                                     != str(formal["cc_auto_compact_window_tokens"])
                        or any(str(env.get(key, "")).lower() in ("1", "true") for key in (
                            "DISABLE_AUTO_COMPACT", "DISABLE_COMPACT", "CLAUDE_CODE_DISABLE_1M_CONTEXT"))))):
                raise ValueError("formal settings do not match the frozen model/context/compaction protocol")
        return settings

    def cc_launch_configuration(self, settings: dict, stage: str) -> dict:
        formal = self.plan.get("formal_native_subset", {})
        formal = formal if stage == formal.get("stage") else {}
        enabled = formal.get("automatic_compaction_enabled", False)
        settings["env"].update(DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
        if not enabled:
            settings["env"]["DISABLE_AUTO_COMPACT"] = "1"
        return {"model": formal.get("cc_model", "kimi-k3.1-pro"),
                "automatic_compaction_enabled": enabled,
                "auto_compact_window_tokens": formal.get("cc_auto_compact_window_tokens")}

    def verify_source(self, name: str) -> None:
        if file_hashes(self.dataset / name) != self.plan["task_identities"][name]:
            raise ValueError(f"benchmark source changed: {name}")

    def network_adapter(self, stage: str) -> dict | None:
        formal = self.plan.get("formal_native_subset", {})
        spec = formal.get("node_network_adapter") if stage == formal.get("stage") else None
        if not spec:
            return None
        actual = network_tree_identity(NODE_NETWORK_DIRECTORY)
        if actual["tree_sha256"] != spec["tree_sha256"]:
            raise RuntimeError("CC network adapter differs from the frozen installed tree")
        return {**actual, "require": "/opt/cc-node-runtime/timeout.cjs",
                "cli": "/opt/claude-code-2.1.110/package/cli.js"}

    def resume_attempt(self, name: str, effort: str, stage: str, repeat: int,
                       budget: int, recover_interrupted: bool = False) -> bool:
        """Keep all completed outcomes; explicitly archive dead, incomplete attempts."""
        output = self.base / "runs" / stage / f"{name}--{effort}--r{repeat}"
        if not output.exists():
            return False
        self.verify_source(name)
        result_path = output / "result.json"
        if result_path.exists():
            result = json.loads(result_path.read_text())
            expected = {"task": name, "effort": effort, "stage": stage,
                        "repeat": repeat, "budget_seconds": budget}
            if any(result.get(key) != value for key, value in expected.items()):
                raise RuntimeError(f"completed attempt does not match resumed protocol: {output}")
            if not isinstance(result.get("agent"), dict) or not isinstance(result.get("grade"), dict):
                raise RuntimeError(f"completed attempt has no process/grade evidence: {output}")
            print(json.dumps({"attempt_preserved": name, "effort": effort, "repeat": repeat,
                              "score": result["grade"].get("score")}), flush=True)
            return True
        if not recover_interrupted:
            raise RuntimeError(f"incomplete attempt requires --recover-interrupted: {output}")
        owner_path = output / "owner.json"
        if owner_path.exists():
            owner = json.loads(owner_path.read_text())
            if process_identity(owner["pid"]) == owner:
                raise RuntimeError(f"incomplete attempt still has a live controller: {output}")
        state_path = output / "agent-state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            for argument in state.get("command", []):
                if argument.startswith("--userspec="):
                    uid = int(argument.split("=", 1)[1].split(":", 1)[0])
                    # Never archive a still-running orphan, and never kill it here.
                    for proc in Path("/proc").iterdir():
                        if not proc.name.isdigit():
                            continue
                        try:
                            if any(line.startswith("Uid:") and int(line.split()[1]) == uid
                                   for line in (proc / "status").read_text().splitlines()):
                                raise RuntimeError(f"incomplete attempt still has a live agent UID {uid}: {output}")
                        except OSError:
                            continue
        archive = self.base / "runs-interrupted" / stage / f"{output.name}--{time.time_ns()}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        output.rename(archive)
        write_json(archive / "interruption.json", {
            "status": "administratively_interrupted", "score": None,
            "excluded_from_model_grades": True, "recorded_at": time.time(),
            "reason": "explicit recovery of an incomplete attempt after its controller and agent stopped",
            "task": name, "effort": effort, "stage": stage, "repeat": repeat,
            "original_attempt": str(output.relative_to(self.base)),
            "archive": str(archive.relative_to(self.base))})
        print(json.dumps({"interrupted_attempt_archived": str(archive)}), flush=True)
        return False

    def install(self, root: Path, packages: list[str], log: Path) -> None:
        if not packages:
            return
        # These managed Python copies belong exclusively to the task rootfs.
        # The host Python and original downloaded distributions stay untouched.
        command = "python3 -m pip install --break-system-packages --no-cache-dir --disable-pip-version-check "
        cache = self.base / "wheel-cache"
        if cache.exists():
            copy_tree(cache, root / "opt/native-wheels")
            command += "--find-links /opt/native-wheels "
            if ((cache / "offline-3.12.json").exists()
                    and (root / "usr/local/bin/python3").readlink() == Path("/opt/python/3.12/bin/python3.12")):
                command += "--no-index "
        if log.exists():
            log.rename(log.with_name(log.name + f".previous-{time.time_ns()}"))
        result = run_logged(shell_command(root, command + shlex.join(packages)), log, 600)
        if result["exit_code"] != 0:
            raise RuntimeError(f"dependency installation failed; see {log}")

    def prepare(self, name: str) -> None:
        self.verify_source(name)
        recipe = RECIPES[name]
        task = self.dataset / name
        for role in ("agent", "verifier"):
            target = self.base / "templates" / name / role
            ready = target.parent / f"{role}-ready.json"
            if ready.exists():
                if json.loads(ready.read_text())["recipe"] != recipe:
                    raise RuntimeError(f"template recipe changed; use a new evaluation base: {ready}")
                continue
            if target.exists():
                shutil.rmtree(target)
            clone_root(self.base / "native-base", target)
            select_python(target, recipe["python"])
            (target / "app").mkdir(exist_ok=True)
            (target / "logs/verifier").mkdir(parents=True, exist_ok=True)
            os.chmod(target / "logs/verifier", 0o700)
            if role == "agent":
                for source, destination in recipe["copies"].items():
                    src, dst = task / "environment" / source, target / destination
                    if src.is_dir():
                        copy_tree(src, dst)
                    else:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                packages = recipe.get("agent_packages", [])
            else:
                copy_tree(task / "tests", target / "tests")
                packages = [*COMMON_VERIFIER, *recipe.get("verifier_packages", [])]
                if name == "batched-eval-parity":
                    copy_tree(task / "tests/model", target / "app/model")
                if name == "session-window-debug":
                    (target / "app/app").mkdir(parents=True, exist_ok=True)
                if name == "ks-solver-cpp":
                    shutil.copy2(task / "tests/oracle.hpp", target / "app/oracle.hpp")
            self.install(target, packages, target.parent / f"{role}-packages.log")
            if role == "verifier":
                wrapper = target / "usr/local/bin/pytest"
                wrapper.write_text('#!/bin/sh\nexec /usr/local/bin/python3 -m pytest "$@"\n')
                wrapper.chmod(0o755)
            version = subprocess.check_output(
                shell_command(target, "python3 --version; python3 -m pip freeze"), text=True)
            write_json(ready, {"task": name, "role": role, "python_and_packages": version,
                               "recipe": recipe, "prepared_at": time.time()})
            print(json.dumps({"prepared": name, "role": role}), flush=True)

    def stage_artifacts(self, name: str, agent: Path, verifier: Path) -> dict:
        hashes, errors = {}, []
        for artifact in self.task(name)["artifacts"]:
            if not isinstance(artifact, str):
                raise ValueError("native pilot only supports single-service artifacts")
            try:
                hashes.update(copy_artifact(agent, verifier, artifact))
            except (ValueError, FileNotFoundError) as error:
                errors.append(str(error))
        return {"hashes": hashes, "errors": errors}

    def grade(self, name: str, agent: Path, output: Path, cpu_offset: int = 0) -> dict:
        verifier = output / "verifier-root"
        clone_root(self.base / "templates" / name / "verifier", verifier)
        artifacts = self.stage_artifacts(name, agent, verifier)
        write_json(output / "artifacts.json", artifacts)
        if artifacts["errors"]:
            result = {"score": 0, "infrastructure_error": None,
                      "artifact_errors": artifacts["errors"], "process": None}
            write_json(output / "grade.json", result)
            return result
        task = self.task(name)
        env = RECIPES[name].get("environment", {})
        process = run_logged(
            shell_command(verifier, "cd /app && bash /tests/test.sh", environment=env,
                          cpus=task.get("verifier", {}).get("environment", {}).get("cpus",
                                                                                  task["environment"]["cpus"]),
                          cpu_offset=cpu_offset),
            output / "verifier.log", task["verifier"]["timeout_sec"], output / "verifier-state.json")
        result = reward_result(verifier, process)
        logs = verifier / "logs/verifier"
        if logs.exists():
            copy_tree(logs, output / "verifier-results")
        result.update(process=process)
        write_json(output / "grade.json", result)
        return result

    def control(self, name: str, retry: bool = False) -> dict:
        self.verify_source(name)
        path = self.base / "controls" / name
        if (path / "result.json").exists():
            previous = json.loads((path / "result.json").read_text())
            if previous["valid"]:
                return previous
            if not retry:
                raise RuntimeError(f"cached controls are invalid: {path}; fix environment before --retry-controls")
        if path.exists():
            if not retry:
                raise RuntimeError(f"incomplete controls: {path}; inspect evidence before --retry-controls")
            archived = self.base / "controls-rejected" / f"{name}--{time.time_ns()}"
            archived.parent.mkdir(exist_ok=True)
            path.rename(archived)
            print(json.dumps({"archived_control": str(archived)}), flush=True)
        path.mkdir(parents=True, exist_ok=True)
        results = {}
        for mode in ("baseline", "oracle"):
            output = path / mode
            output.mkdir(exist_ok=True)
            agent = output / "agent-root"
            clone_root(self.base / "templates" / name / "agent", agent)
            if mode == "oracle":
                copy_tree(self.dataset / name / "solution", agent / "solution")
                process = run_logged(shell_command(agent, "cd /app && bash /solution/solve.sh",
                                                   environment=RECIPES[name].get("environment", {})),
                                     output / "oracle.log", 1800)
                if process["exit_code"]:
                    raise RuntimeError(f"oracle setup failed: {output / 'oracle.log'}")
            results[mode] = self.grade(name, agent, output)
            if results[mode]["infrastructure_error"]:
                raise RuntimeError(f"control verifier failed: {output}")
        results["valid"] = results["baseline"]["score"] == 0 and results["oracle"]["score"] == 1
        write_json(path / "result.json", results)
        if not results["valid"]:
            raise RuntimeError(f"baseline/oracle control mismatch: {path}")
        print(json.dumps({"controls_passed": name}), flush=True)
        return results

    def attempt(self, name: str, effort: str, stage: str, repeat: int,
                budget: int, uid: int) -> dict:
        identity = self.identity(stage)
        settings = self.request_settings(effort, stage)
        adapter = self.network_adapter(stage)
        if adapter:
            identity["cc_network_adapter"] = adapter
        self.verify_source(name)
        controls = json.loads((self.base / "controls" / name / "result.json").read_text())
        if not controls["valid"]:
            raise RuntimeError(f"unvalidated environment: {name}")
        output = self.base / "runs" / stage / f"{name}--{effort}--r{repeat}"
        if output.exists():
            # Never silently rerun failed/incomplete attempts or overwrite evidence.
            raise FileExistsError(output)
        output.mkdir(parents=True)
        write_json(output / "owner.json", process_identity(os.getpid()))
        write_json(output / "identity.json", identity)
        root = output / "agent-root"
        clone_root(self.base / "templates" / name / "agent", root)
        home = root / "home/agent"
        for directory in (root / "app", home):
            subprocess.run(["chown", "-R", f"{uid}:{uid}", str(directory)], check=True)
        passwd = root / "etc/passwd"
        passwd.write_text(passwd.read_text().replace("agent:x:1000:1000:", f"agent:x:{uid}:{uid}:"))
        group = root / "etc/group"
        group.write_text(group.read_text().replace("agent:x:1000:", f"agent:x:{uid}:"))
        config = root / "eval"
        config.mkdir()
        launch = self.cc_launch_configuration(settings, stage)
        write_json(config / "settings.json", settings)
        bootstrap = ["claude"]
        bootstrap_env = {}
        if adapter:
            target = root / "opt/cc-node-runtime"
            copy_tree(NODE_NETWORK_DIRECTORY, target)
            if network_tree_identity(target)["tree_sha256"] != adapter["tree_sha256"]:
                raise RuntimeError("CC network adapter changed while copying the installed tree")
            bootstrap = ["node", "--require", adapter["require"], adapter["cli"]]
            bootstrap_env = {key: settings["env"][key] for key in ("API_TIMEOUT_MS", "ANTHROPIC_BASE_URL")}
        write_json(output / "request-protocol.json", {
            "settings_sha256": hashlib.sha256((config / "settings.json").read_bytes()).hexdigest(),
            "cc_output_environment_tokens": settings["env"].get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
            "extra_body": json.loads(settings["env"].get("CLAUDE_CODE_EXTRA_BODY", "{}")),
            "cc_api_timeout_ms": settings["env"].get("API_TIMEOUT_MS"),
            "cc_launch_configuration": launch,
            "agent_budget_seconds": budget, "cc_network_adapter": adapter,
            "cc_bootstrap": bootstrap, "bootstrap_environment": bootstrap_env})
        write_json(config / "mcp.json", {"mcpServers": {}})
        (config / "instruction.md").write_bytes((self.dataset / name / "instruction.md").read_bytes())
        args = bootstrap + ["-p", "--output-format", "stream-json", "--verbose",
                "--settings", "/eval/settings.json", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", "/eval/mcp.json",
                "--permission-mode", "bypassPermissions", "--disable-slash-commands",
                "--tools", "Bash,Read,Write,Edit,Glob,Grep", "--model", launch["model"],
                "--effort", effort, "--max-turns", "500"]
        # Only runner budget information is appended; no task-specific hints.
        prefix = f"The runner enforces a {budget}-second wall-clock limit for this {stage} attempt. "
        prefix += "Work independently. Do not retrieve benchmark solutions, tests or task-specific online hints.\n\n"
        (config / "prompt.txt").write_text(prefix + (config / "instruction.md").read_text())
        command = "cd /app && " + shlex.join(args) + " < /eval/prompt.txt"
        env = {**RECIPES[name].get("environment", {}), **bootstrap_env}
        actual_version = subprocess.check_output(
            shell_command(root, shlex.join(bootstrap + ["--version"]), uid=uid, environment=env), text=True).strip()
        if actual_version.split()[0] != self.plan["cc_version"]:
            raise RuntimeError(f"CC version mismatch: {actual_version}")
        started = time.time()
        write_json(output / "status.json", {"status": "agent_running", "task": name,
                                            "effort": effort, "started_at": started})
        print(json.dumps({"attempt_started": name, "effort": effort, "repeat": repeat,
                          "budget": budget, "uid": uid}), flush=True)
        cpus = self.task(name)["environment"]["cpus"]
        process = run_logged(shell_command(root, command, uid=uid, environment=env,
                                           cpus=cpus, cpu_offset=cpus if effort == "max" else 0),
                             output / "cc.jsonl", budget, output / "agent-state.json", uid)
        client = cc_result(output / "cc.jsonl")
        write_json(output / "status.json", {"status": "grading", "task": name, "effort": effort})
        # The verifier replaces this attempt's agent on the same disjoint CPU
        # allocation; a completed max attempt must not contend with high.
        grade = self.grade(name, root, output, cpu_offset=cpus if effort == "max" else 0)
        telemetry = bridge_activity(self.base / "bridge-debug.jsonl", started,
                                    process["finished_at"], effort)
        infra = grade["infrastructure_error"]
        if not telemetry["logical_requests"]:
            infra = "no_model_request_dispatched"
        result = {"task": name, "effort": effort, "stage": stage, "repeat": repeat,
                  "budget_seconds": budget, "agent": process, "cc": client, "grade": grade,
                  "bridge": telemetry, "source_head": identity["bridge_head"],
                  "infrastructure_error": infra,
                  "end_to_end_success": grade["score"] == 1 and process["exit_code"] == 0
                  and bool(client) and not client["is_error"] and not process["timed_out"] and not infra}
        write_json(output / "result.json", result)
        write_json(output / "status.json", {"status": "completed", "task": name,
                                            "effort": effort, "score": grade["score"]})
        print(json.dumps({"attempt_finished": name, "effort": effort,
                          "score": grade["score"], "end_to_end_success": result["end_to_end_success"],
                          "agent_seconds": process["duration_seconds"]}), flush=True)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "prepare", "controls", "run"))
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--tasks", nargs="+", choices=tuple(RECIPES))
    parser.add_argument("--stage", default="pilot")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--budget", type=int, default=1800)
    parser.add_argument("--retry-controls", action="store_true",
                        help="preserve rejected/incomplete controls and repeat after an environment fix")
    parser.add_argument("--resume", action="store_true",
                        help="preserve every completed outcome and continue only missing attempts")
    parser.add_argument("--recover-interrupted", action="store_true",
                        help="with --resume, archive dead incomplete attempts as ungraded administrative interruptions")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.stage):
        parser.error("stage must be a simple directory name")
    if args.repeats < 1 or args.budget < 1:
        parser.error("repeat count and time budget must be positive")
    if args.retry_controls and args.action != "controls":
        parser.error("--retry-controls applies only to controls")
    if (args.resume or args.recover_interrupted) and args.action != "run":
        parser.error("resume options apply only to run")
    if args.recover_interrupted and not args.resume:
        parser.error("--recover-interrupted requires --resume")
    if args.action == "init":
        initialize_plan(args.base.resolve(), args.tasks or DEFAULT_PILOT)
        return
    if os.geteuid() != 0:
        parser.error("native filesystem setup requires sudo; CC itself runs as an isolated UID")
    evaluation = Evaluation(args.base)
    args.tasks = args.tasks or evaluation.plan["pilot_tasks"]
    if args.action == "prepare":
        for name in args.tasks:
            evaluation.prepare(name)
    elif args.action == "controls":
        for name in args.tasks:
            evaluation.control(name, retry=args.retry_controls)
    else:
        # Pair efforts on each task. At most one attempt for each effort runs at
        # once, so bridge requests can be attributed by effort and time window.
        for name, repeat, efforts in attempt_batches(evaluation.plan, args.stage, args.repeats, args.tasks):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = []
                for effort in efforts:
                    if args.resume and evaluation.resume_attempt(name, effort, args.stage, repeat,
                                                                 args.budget, args.recover_interrupted):
                        continue
                    uid = allocate_uid(evaluation.base)
                    futures.append(pool.submit(evaluation.attempt, name, effort,
                                               args.stage, repeat, args.budget, uid))
                for future in futures:
                    future.result()


if __name__ == "__main__":
    main()

"""Run explicitly adapted Terminal-Bench tasks with CC and no Docker.

This is a local, supervised evaluation driver, not the official Harbor harness.
Provision native-base separately, then run prepare, controls, and run as root.
Benchmark prompts, solutions, fixtures and generated artifacts stay in logs/.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
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


class Evaluation:
    def __init__(self, base: Path):
        self.base = base.resolve()
        self.plan = json.loads((self.base / "evaluation-plan.json").read_text())
        self.dataset = self.base / "terminal-bench/tasks"

    def identity(self) -> dict:
        """Record actual executable/source identities, including dirty tooling."""
        scripts = [Path(__file__).resolve(), Path(__file__).with_name("native_eval_support.py").resolve()]
        return {"bridge_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True).strip(),
                "bridge_dirty": subprocess.check_output(
                    ["git", "diff", "--name-only"], text=True).splitlines(),
                "runner_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in scripts},
                "cc_version": self.plan["cc_version"],
                "dataset_commit": self.plan["source_commit"]}

    def task(self, name: str) -> dict:
        return tomllib.loads((self.dataset / name / "task.toml").read_text())

    def verify_source(self, name: str) -> None:
        if file_hashes(self.dataset / name) != self.plan["task_identities"][name]:
            raise ValueError(f"benchmark source changed: {name}")

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
        self.verify_source(name)
        controls = json.loads((self.base / "controls" / name / "result.json").read_text())
        if not controls["valid"]:
            raise RuntimeError(f"unvalidated environment: {name}")
        output = self.base / "runs" / stage / f"{name}--{effort}--r{repeat}"
        if output.exists():
            # Never silently rerun failed/incomplete attempts or overwrite evidence.
            raise FileExistsError(output)
        output.mkdir(parents=True)
        write_json(output / "identity.json", self.identity())
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
        settings = json.loads((self.base / f"settings-{effort}.json").read_text())
        settings["env"].update(DISABLE_AUTOUPDATER="1", DISABLE_AUTO_COMPACT="1",
                               CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
        write_json(config / "settings.json", settings)
        write_json(config / "mcp.json", {"mcpServers": {}})
        (config / "instruction.md").write_bytes((self.dataset / name / "instruction.md").read_bytes())
        args = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                "--settings", "/eval/settings.json", "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", "/eval/mcp.json",
                "--permission-mode", "bypassPermissions", "--disable-slash-commands",
                "--tools", "Bash,Read,Write,Edit,Glob,Grep", "--model", "kimi-k3.1-pro",
                "--effort", effort, "--max-turns", "500"]
        # Only runner budget information is appended; no task-specific hints.
        prefix = f"The runner enforces a {budget}-second wall-clock limit for this {stage} attempt. "
        prefix += "Work independently. Do not retrieve benchmark solutions, tests or task-specific online hints.\n\n"
        (config / "prompt.txt").write_text(prefix + (config / "instruction.md").read_text())
        command = "cd /app && " + shlex.join(args) + " < /eval/prompt.txt"
        env = RECIPES[name].get("environment", {})
        actual_version = subprocess.check_output(
            shell_command(root, "claude --version", uid=uid, environment=env), text=True).strip()
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
                  "bridge": telemetry, "source_head": self.plan["bridge_source_head"],
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
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.stage):
        parser.error("stage must be a simple directory name")
    if args.repeats < 1 or args.budget < 1:
        parser.error("repeat count and time budget must be positive")
    if args.retry_controls and args.action != "controls":
        parser.error("--retry-controls applies only to controls")
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
        for repeat in range(1, args.repeats + 1):
            for name in args.tasks:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = []
                    for effort in ("high", "max"):
                        uid = allocate_uid(evaluation.base)
                        futures.append(pool.submit(evaluation.attempt, name, effort,
                                                   args.stage, repeat, args.budget, uid))
                    for future in futures:
                        future.result()


if __name__ == "__main__":
    main()

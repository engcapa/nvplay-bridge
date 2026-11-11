"""Freeze the current user's exact PATH CLI in a new native evaluation stage.

Pass --claude "$(command -v claude)" from the ordinary user's shell. Run after
native provisioning/configuration and before live smoke or campaign startup.
This never stops a running attempt or changes its recorded client identity.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from advance_terminal_native_transport import remaining_schedule
from checkpoint_terminal_native import campaign_progress
from native_eval_support import write_json
from native_path_cc import GUEST_CLI, sha256, verify_runtime
from report_terminal_native import summarize


def configure(base: Path, cli: Path, proot: Path, stage: str, *, proot_source_commit: str | None = None,
              disable_native_idle_timeout: bool = False) -> dict:
    base = base.resolve(strict=True)
    if not re.fullmatch(r"formal-[A-Za-z0-9_-]+", stage):
        raise ValueError("use a distinct formal stage name")
    with (base / "cc-runtime-configure.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = summarize(base)
        if campaign_progress(base)["controller_alive"] or report["pending"]:
            raise RuntimeError("stop scheduling and preserve incomplete attempts before changing CC runtime")
        if any((base / "smoke").glob("*/agent-state.json")) and not (base / "smoke-result.json").is_file():
            raise RuntimeError("preserve the incomplete smoke before changing CC runtime")
        path = base / "evaluation-plan.json"
        plan = json.loads(path.read_text())
        formal = plan["formal_native_subset"]
        old_stage = formal["stage"]
        if stage == old_stage or stage in plan.get("cc_runtime_by_stage", {}):
            raise ValueError("the requested stage is already frozen")
        source, translator = cli.resolve(strict=True), proot.resolve(strict=True)
        version = subprocess.check_output([str(source), "--version"], text=True).strip().split()[0]
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise RuntimeError("unrecognized PATH Claude version")
        with source.open("rb") as executable:
            native_executable = executable.read(4) == b"\x7fELF"
        if not native_executable:
            raise ValueError("this runtime requires the user's native Claude executable")
        proot_output = subprocess.check_output([str(translator), "--version"], text=True)
        match = re.search(r"\bv?(\d+\.\d+\.\d+)\b", proot_output)
        if not match or tuple(map(int, match[1].split("."))) < (5, 5, 0):
            raise ValueError("use PRoot 5.5.0 or later for native file-query compatibility")
        snapshot = base / "path-cc-runtime" / stage / "claude"
        snapshot.parent.mkdir(parents=True, exist_ok=False)
        digest = sha256(source)
        shutil.copy2(source, snapshot)
        snapshot.chmod(0o755)
        spec = {"kind": "path-native-proot", "source_path": str(cli.absolute()),
                "resolved_source": str(source), "version": version, "sha256": digest,
                "size": source.stat().st_size, "snapshot_path": str(snapshot), "guest_cli": GUEST_CLI,
                "proot_path": str(translator), "proot_sha256": sha256(translator), "proot_version": match[1],
                "real_proc_binding": True, "real_dedicated_uid": True, "no_new_privileges": True,
                "host_cwd_inside_agent_root": True,
                "isolation_boundary": "Real Unix UID and private source directory permissions; PRoot supplies path translation only"}
        if proot_source_commit:
            spec["proot_source_commit"] = proot_source_commit
        verify_runtime(spec)
        if sha256(source) != digest:
            raise RuntimeError("PATH executable changed during snapshot creation")
        remaining, preserved = remaining_schedule(formal, report)
        if not remaining:
            raise ValueError("no unfinished scheduled positions")
        settings_updates = {}
        if disable_native_idle_timeout:
            for effort in ("high", "max"):
                settings_path = base / f"settings-formal-{effort}.json"
                settings = json.loads(settings_path.read_text())
                settings["env"]["API_FORCE_IDLE_TIMEOUT"] = "0"
                settings_updates[settings_path] = settings
        write_json(snapshot.parent / "runtime.json", spec)
        write_json(base / f"evaluation-plan-before-cc-{time.time_ns()}.json", plan)
        plan.setdefault("protocol_history", []).append({"changed_at": time.time(),
            "reason": ("disable native fetch idle timeout after controlled delayed-header reproduction"
                       if disable_native_idle_timeout else "user requested current PATH Claude Code"),
            "cc_version": plan["cc_version"],
            "formal_native_subset": deepcopy(formal), "executor": plan["executor"]})
        versions = plan.setdefault("cc_versions_by_stage", {})
        versions[old_stage], versions[stage] = plan["cc_version"], version
        formal.update(stage=stage, schedule=remaining, preserved_local_schedule=preserved,
                      frozen_at=time.time(), cc_runtime_kind=spec["kind"])
        formal.pop("node_network_adapter", None)
        if formal.get("automatic_compaction_enabled"):
            formal["cc_autocompact_cli_tokens"] = formal["cc_auto_compact_window_tokens"]
        if disable_native_idle_timeout:
            formal["cc_native_idle_timeout_disabled"] = True
        plan.update(cc_version=version, executor="PRoot path translation, real dedicated ordinary UID, protected sources, real /proc; shared host kernel/network")
        plan.setdefault("cc_runtime_by_stage", {})[stage] = spec
        plan["bridge_runtime_by_stage"][stage] = deepcopy(plan["bridge_runtime_by_stage"][old_stage])
        smoke_paths = [base / name for name in ("smoke", "smoke-result.json") if (base / name).exists()]
        if smoke_paths:
            history = base / "smoke-history" / f"{old_stage}--{time.time_ns()}"
            history.mkdir(parents=True)
            for smoke in smoke_paths:
                smoke.rename(history / smoke.name)
        for settings_path, settings in settings_updates.items():
            write_json(settings_path, settings)
        write_json(path, plan)
        return {"stage": stage, "runtime": spec, "remaining_positions": len(remaining),
                "preserved_local_positions": len(preserved), "fresh_live_smoke_required": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--claude", type=Path, required=True, help="command -v claude from the ordinary user's PATH")
    parser.add_argument("--proot", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--proot-source-commit")
    parser.add_argument("--disable-native-idle-timeout", action="store_true",
                        help="Freeze API_FORCE_IDLE_TIMEOUT=0 in both native launch environments")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("use sudo; measured CC runs as an ordinary dedicated UID")
    print(json.dumps(configure(args.base, args.claude, args.proot, args.stage,
                              proot_source_commit=args.proot_source_commit,
                              disable_native_idle_timeout=args.disable_native_idle_timeout), ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Exercise real high/max CC Bash/Python tool loops as isolated ordinary UIDs."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from eval_terminal_native import Evaluation
from native_eval_support import allocate_uid, bridge_activity, cc_result, run_logged, write_json
from native_path_cc import return_agent_root


def smoke(evaluation, effort):
    stage = evaluation.plan["formal_native_subset"]["stage"]
    output = evaluation.base / "smoke" / effort
    output.mkdir(parents=True)
    uid = allocate_uid(evaluation.base)
    root = evaluation.create_agent_root(evaluation.base / "native-base", output, uid, stage)
    identity = evaluation.identity(stage)
    subprocess.run(["chown", "-R", f"{uid}:{uid}", str(root / "app"), str(root / "home/agent")], check=True)
    passwd = root / "etc/passwd"
    passwd.write_text(passwd.read_text().replace("agent:x:1000:1000:", f"agent:x:{uid}:{uid}:"))
    group = root / "etc/group"
    group.write_text(group.read_text().replace("agent:x:1000:", f"agent:x:{uid}:"))
    settings = evaluation.request_settings(effort, stage)
    launch = evaluation.cc_launch_configuration(settings, stage)
    bootstrap, env, adapter = evaluation.cc_bootstrap(root, stage, settings)
    config = root / "eval"
    config.mkdir()
    write_json(config / "settings.json", settings)
    write_json(config / "mcp.json", {"mcpServers": {}})
    payload = os.urandom(4096)
    (root / "app/smoke-input.bin").write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    (config / "prompt.txt").write_text(
        "Use the Bash tool to run Python 3 to compute the SHA256 of /app/smoke-input.bin. "
        "Write the lowercase 64-character digest to /app/answer.txt, then reply with only that digest. "
        "Do not use any other agent, network, or package installation.\n")
    command = bootstrap + ["-p", "--output-format", "stream-json", "--verbose", "--settings", "/eval/settings.json",
        "--setting-sources", "", "--strict-mcp-config", "--mcp-config", "/eval/mcp.json",
        "--permission-mode", "bypassPermissions", "--disable-slash-commands",
        "--tools", "Bash,Read,Write,Edit,Glob,Grep", "--model", launch["model"], "--effort", effort]
    command += evaluation.compaction_arguments(stage, launch)
    version = subprocess.check_output(evaluation.agent_command(root, shlex.join(bootstrap + ["--version"]),
             stage=stage, uid=uid, environment=env), cwd=root if identity.get("cc_runtime") else None,
             text=True).strip()
    if version.split()[0] != identity["cc_version"]:
        raise RuntimeError(f"CC smoke version mismatch: {version}")
    started = time.time()
    process = run_logged(evaluation.agent_command(root, "cd /app && " + shlex.join(command) + " < /eval/prompt.txt",
                                       stage=stage,
                                       uid=uid, environment=env, cpus=1, cpu_offset=1 if effort == "max" else 0),
                         output / "cc.jsonl", evaluation.agent_timeout(stage, 28800), output / "agent-state.json", uid,
                         cwd=root if identity.get("cc_runtime") else None)
    root = return_agent_root(output)
    client = cc_result(output / "cc.jsonl")
    telemetry = bridge_activity(evaluation.base / "bridge-debug.jsonl", started, process["finished_at"], effort)
    calls = []
    for line in (output / "cc.jsonl").read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant":
            calls.extend(block.get("name") for block in event.get("message", {}).get("content", [])
                         if block.get("type") == "tool_use")
    answer = root / "app/answer.txt"
    windows = sorted({value.get("contextWindow") for value in (client or {}).get("modelUsage", {}).values()
                      if value.get("contextWindow")})
    expected_window = evaluation.plan["formal_native_subset"].get("cc_model_context_window_tokens")
    correct = (answer.is_file() and not answer.is_symlink() and answer.read_text().strip() == expected
               and client and client.get("result", "").strip() == expected and "Bash" in calls)
    passed = bool(correct and process["exit_code"] == 0 and not process["timed_out"] and not client["is_error"]
                  and telemetry["actual_efforts"] == [effort] and not telemetry["errors"] and not telemetry["unfinished"]
                  and (expected_window is None or windows == [expected_window]))
    result = {"effort": effort, "passed": passed, "process": process, "uid": uid,
              "tool_calls": calls, "expected_sha256": expected, "bridge": telemetry,
              "source_identity": identity, "network_adapter": adapter, "cc_version_output": version,
              "cc_launch_configuration": launch, "reported_context_windows": windows}
    write_json(output / "result.json", result)
    print(json.dumps({"smoke_effort": effort, "passed": passed, "seconds": process["duration_seconds"]}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--efforts", nargs="+", choices=("high", "max"), default=["high", "max"],
                        help="Select an isolated supplemental smoke; campaigns require both efforts")
    args = parser.parse_args()
    if len(set(args.efforts)) != len(args.efforts):
        parser.error("smoke efforts must be unique")
    if os.geteuid() != 0:
        parser.error("native root creation needs sudo; CC itself uses a dedicated ordinary UID")
    evaluation = Evaluation(args.base)
    with ThreadPoolExecutor(max_workers=len(args.efforts)) as pool:
        results = list(pool.map(lambda effort: smoke(evaluation, effort), args.efforts))
    result = {"recorded_at": time.time(), "passed": all(row["passed"] for row in results), "efforts": results,
              "stage": evaluation.plan["formal_native_subset"]["stage"]}
    write_json(evaluation.base / "smoke-result.json", result)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

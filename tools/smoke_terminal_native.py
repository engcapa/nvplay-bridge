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

from eval_terminal_native import Evaluation, NODE_NETWORK_DIRECTORY, network_tree_identity
from native_eval_support import allocate_uid, bridge_activity, cc_result, clone_root, copy_tree, run_logged, shell_command, write_json


def smoke(evaluation, effort):
    stage = evaluation.plan["formal_native_subset"]["stage"]
    output = evaluation.base / "smoke" / effort
    output.mkdir(parents=True)
    root = output / "agent-root"
    clone_root(evaluation.base / "native-base", root)
    uid = allocate_uid(evaluation.base)
    subprocess.run(["chown", "-R", f"{uid}:{uid}", str(root / "app"), str(root / "home/agent")], check=True)
    settings = evaluation.request_settings(effort, stage)
    settings["env"].update(DISABLE_AUTOUPDATER="1", DISABLE_AUTO_COMPACT="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    adapter = evaluation.network_adapter(stage)
    if not adapter:
        raise RuntimeError("the formal Node network adapter must be enabled")
    copy_tree(NODE_NETWORK_DIRECTORY, root / "opt/cc-node-runtime")
    if network_tree_identity(root / "opt/cc-node-runtime")["tree_sha256"] != adapter["tree_sha256"]:
        raise RuntimeError("copied adapter identity mismatch")
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
    bootstrap = ["node", "--require", adapter["require"], adapter["cli"]]
    command = bootstrap + ["-p", "--output-format", "stream-json", "--verbose", "--settings", "/eval/settings.json",
        "--setting-sources", "", "--strict-mcp-config", "--mcp-config", "/eval/mcp.json",
        "--permission-mode", "bypassPermissions", "--disable-slash-commands",
        "--tools", "Bash,Read,Write,Edit,Glob,Grep", "--model", "kimi-k3.1-pro", "--effort", effort]
    env = {key: settings["env"][key] for key in ("API_TIMEOUT_MS", "ANTHROPIC_BASE_URL")}
    started = time.time()
    process = run_logged(shell_command(root, "cd /app && " + shlex.join(command) + " < /eval/prompt.txt",
                                       uid=uid, environment=env, cpus=1, cpu_offset=1 if effort == "max" else 0),
                         output / "cc.jsonl", 28800, output / "agent-state.json", uid)
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
    correct = (answer.is_file() and not answer.is_symlink() and answer.read_text().strip() == expected
               and client and client.get("result", "").strip() == expected and "Bash" in calls)
    passed = bool(correct and process["exit_code"] == 0 and not process["timed_out"] and not client["is_error"]
                  and telemetry["actual_efforts"] == [effort] and not telemetry["errors"] and not telemetry["unfinished"])
    result = {"effort": effort, "passed": passed, "process": process, "uid": uid,
              "tool_calls": calls, "expected_sha256": expected, "bridge": telemetry,
              "source_identity": evaluation.identity(stage), "network_adapter": adapter}
    write_json(output / "result.json", result)
    print(json.dumps({"smoke_effort": effort, "passed": passed, "seconds": process["duration_seconds"]}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("native root creation needs sudo; CC itself uses a dedicated ordinary UID")
    evaluation = Evaluation(args.base)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda effort: smoke(evaluation, effort), ("high", "max")))
    result = {"recorded_at": time.time(), "passed": all(row["passed"] for row in results), "efforts": results}
    write_json(evaluation.base / "smoke-result.json", result)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

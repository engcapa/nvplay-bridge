"""set_global_cc.py — 备份并把 Claude Code 全局配置切到本地中转。"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil

SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
BACKUP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)


def main() -> int:
    with open(SETTINGS, encoding="utf-8") as f:
        data = json.load(f)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{SETTINGS}.bak-{stamp}"
    shutil.copy2(SETTINGS, backup)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    shutil.copy2(SETTINGS, os.path.join(BACKUP_DIR, f"settings.json.bak-{stamp}"))

    env = data.setdefault("env", {})
    env.update(
        {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8083",
            "ANTHROPIC_AUTH_TOKEN": "test",
            "ANTHROPIC_MODEL": "kimi-k3.1-pro",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "kimi-k3.1-pro",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "kimi-k3.1-pro",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "kimi-k3.1-pro",
            "CLAUDE_CODE_ALLOW_OTHER_MODELS": "1",
            "CLAUDE_CODE_EXTRA_BODY": json.dumps({"output_config": {"effort": "max"}}),
            "API_TIMEOUT_MS": "600000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    data["model"] = "kimi-k3.1-pro"
    data["effortLevel"] = "max"

    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    masked = json.loads(json.dumps(data))
    if "ANTHROPIC_AUTH_TOKEN" in masked.get("env", {}):
        masked["env"]["ANTHROPIC_AUTH_TOKEN"] = "***"
    print("backup:", backup)
    print("updated:", SETTINGS)
    print(json.dumps(
        {
            "base": masked["env"].get("ANTHROPIC_BASE_URL"),
            "model": masked.get("model"),
            "effortLevel": masked.get("effortLevel"),
            "envModel": masked["env"].get("ANTHROPIC_MODEL"),
            "allowOtherModels": masked["env"].get("CLAUDE_CODE_ALLOW_OTHER_MODELS"),
            "extraBody": masked["env"].get("CLAUDE_CODE_EXTRA_BODY"),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

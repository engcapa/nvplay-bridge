"""Save changed evaluation progress and keep the checkpoint at most five minutes old."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from checkpoint_terminal_native import save


PROGRESS_POLL_SECONDS = 10
HEARTBEAT_SECONDS = 5 * 60


def next_heartbeat(output: Path) -> float:
    try:
        return json.loads(output.read_text())["checkpoint_at"] + HEARTBEAT_SECONDS
    except FileNotFoundError:
        return 0.0


def tick(base: Path, output: Path, health_url: str, commit: bool, push: bool) -> float:
    # Read the shared checkpoint so an immediate save by the campaign resets
    # the idle timer too. save() serializes all writes with the campaign.
    save(base, output, health_url, commit, push,
         changes_only=time.time() < next_heartbeat(output))
    return max(0, min(PROGRESS_POLL_SECONDS, next_heartbeat(output) - time.time()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--health-url", default="http://127.0.0.1:8083/health")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    if args.push and not args.commit:
        parser.error("--push requires --commit")
    base, output = args.base.resolve(), args.output.resolve()
    while True:
        time.sleep(tick(base, output, args.health_url, args.commit, args.push))


if __name__ == "__main__":
    main()

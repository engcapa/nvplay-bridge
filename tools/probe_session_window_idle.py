"""Independently check forward progress on an already audited session artifact.

Run as an ordinary user. The directory must contain the reviewed app package;
do not point this host-side probe at unaudited executable code.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-parent", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("run the audited-code probe as an ordinary user")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.app_parent.resolve(strict=True)))
    from app.events import EventSource
    from app.types import Clock, Event

    source = EventSource(Clock())
    source.register_source("first")
    source.register_source("second")
    source.ingest(Event(b"probe", 41, 3, "first"))
    source.ingest(Event(b"probe", 59, -2, "second"))
    before = source.watermark
    source.advance_time(250)
    after_first_idle = source.watermark
    source.advance_time(250)
    after_second_idle = source.watermark
    progressed = after_second_idle > before
    print(json.dumps({"watermark_before": before, "watermark_after_first_idle": after_first_idle,
                      "watermark_after_second_idle": after_second_idle, "forward_progress": progressed}))
    raise SystemExit(0 if progressed else 1)


if __name__ == "__main__":
    main()

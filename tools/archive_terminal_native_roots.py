"""Compress completed rootfs evidence, verify it, then reclaim the expanded copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from native_eval_support import write_json


def archive_root(root: Path) -> dict:
    root = root.absolute()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / "root").resolve() == root:
                raise RuntimeError(f"refusing to archive a live process root: {root}")
        except OSError:
            continue
    archive = root.with_suffix(".tar.gz")
    metadata = root.with_name(root.name + "-archive.json")
    if archive.exists():
        raise FileExistsError(f"archive already exists; inspect before resuming: {archive}")
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    subprocess.run(["tar", "--numeric-owner", "-I", "gzip -1", "-cf", str(temporary),
                    "-C", str(root.parent), root.name], check=True)
    # Compare the compressed evidence against the expanded source before deletion.
    subprocess.run(["tar", "-dzf", str(temporary), "-C", str(root.parent)], check=True)
    with temporary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        os.fsync(stream.fileno())
    temporary.rename(archive)
    result = {"archived_at": time.time(), "root": root.name, "archive": archive.name,
              "sha256": digest, "bytes": archive.stat().st_size,
              "tar_compare_passed_before_reclaim": True,
              "restore": f"sudo tar -xpf {archive.name} -C ."}
    write_json(metadata, result)
    shutil.rmtree(root)
    print(json.dumps({"archived_root": str(root), "compressed_bytes": result["bytes"]}), flush=True)
    return result


def completed_roots(base: Path):
    for result_path in sorted((base / "runs").glob("*/*/result.json")):
        data = json.loads(result_path.read_text())
        if isinstance(data.get("agent"), dict) and isinstance(data.get("grade"), dict):
            for name in ("agent-root", "verifier-root"):
                root = result_path.parent / name
                if root.exists():
                    yield root
    for result_path in sorted((base / "controls").glob("*/result.json")):
        if json.loads(result_path.read_text()).get("valid"):
            for mode in ("baseline", "oracle"):
                for name in ("agent-root", "verifier-root"):
                    root = result_path.parent / mode / name
                    if root.exists():
                        yield root
    for result_path in sorted((base / "smoke").glob("*/result.json")):
        root = result_path.parent / "agent-root"
        if root.exists():
            yield root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("native root archives include root-owned files and devices; use sudo")
    for root in completed_roots(args.base.resolve()):
        archive_root(root)


if __name__ == "__main__":
    main()

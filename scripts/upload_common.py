"""Upload a local folder as a read-only "common" snapshot.

Usage:
    python scripts/upload_common.py <folder> <collection> [--url http://localhost:8000] [--exclude NAME ...] [--only-ext .md .txt]

Reads ADMIN_TOKEN from the environment. Only reads files: never runs git (no pull, commit or push).
.git, virtualenvs, binaries and secret-like files are left out here and again on the server.
Standard library only, so it runs on any machine with Python 3.10+.
"""

import argparse
import io
import json
import os
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", ".pytest_cache"}


def build_archive(folder: Path, exclude: set[str], only_ext: tuple[str, ...]) -> bytes:
    """tar.gz of the folder's files, paths relative to the folder (excluded names skipped at any depth)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if only_ext and not path.name.lower().endswith(only_ext):
                continue
            if path.is_file() and not path.is_symlink() and not (SKIP_DIRS | exclude).intersection(relative.parts):
                archive.add(path, arcname=relative.as_posix(), recursive=False)
    return buffer.getvalue()


def request(url: str, token: str, data: bytes | None = None) -> dict:
    """POST (with data) or GET a JSON endpoint."""
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/gzip")
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.loads(response.read())


def main() -> None:
    """Pack, upload, wait for the server to finish, print the result."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("collection")
    parser.add_argument("--url", default=os.environ.get("MEMORY_URL", "http://localhost:8000"))
    parser.add_argument("--exclude", nargs="*", default=[], help="file or folder names to leave out")
    parser.add_argument("--only-ext", nargs="*", default=[], help="keep only these extensions, e.g. .md .txt")
    args = parser.parse_args()

    token = os.environ.get("ADMIN_TOKEN")
    if not token:
        sys.exit("set ADMIN_TOKEN in the environment")
    if not args.folder.is_dir():
        sys.exit(f"not a folder: {args.folder}")

    # Pack and send
    data = build_archive(args.folder, set(args.exclude), tuple(e.lower() for e in args.only_ext))
    print(f"archive: {len(data) / 1024 / 1024:.1f} MB")
    job = request(f"{args.url}/api/admin/common/{args.collection}", token, data)

    # The server indexes in the background; poll until it is done
    while True:
        status = request(f"{args.url}/api/admin/jobs/{job['job_id']}", token)
        if status["status"] != "running":
            print(json.dumps(status, indent=2))
            sys.exit(0 if status["status"] == "done" else 1)
        time.sleep(2)


if __name__ == "__main__":
    main()

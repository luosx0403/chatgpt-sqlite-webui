from __future__ import annotations

"""Validate the scoped Web wheel hash lock and optionally installed versions."""

import argparse
import importlib.metadata
import platform
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-web-py312-macos-arm64.lock"
LINE_RE = re.compile(
    r"^([a-z0-9][a-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+-]*) "
    r"--hash=sha256:([0-9a-f]{64})$"
)
EXPECTED = {
    "annotated-doc",
    "annotated-types",
    "anyio",
    "certifi",
    "click",
    "fastapi",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "pydantic",
    "pydantic-core",
    "python-multipart",
    "starlette",
    "typing-extensions",
    "typing-inspection",
    "uvicorn",
}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-installed", action="store_true")
    args = parser.parse_args()
    rows: dict[str, tuple[str, str]] = {}
    for number, raw in enumerate(LOCK.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE_RE.fullmatch(line)
        if match is None:
            raise SystemExit(f"invalid_hash_lock_line {number}")
        name, version, digest = match.groups()
        key = canonical(name)
        if key in rows:
            raise SystemExit(f"duplicate_hash_lock_package {key}")
        rows[key] = (version, digest)
    if set(rows) != EXPECTED:
        raise SystemExit("hash_lock_package_set_mismatch")
    if args.check_installed:
        if not (
            sys.version_info[:2] == (3, 12)
            and sys.platform == "darwin"
            and platform.machine() == "arm64"
        ):
            raise SystemExit("hash_lock_platform_mismatch")
        for name, (expected_version, _digest) in rows.items():
            try:
                actual = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                raise SystemExit(f"hash_lock_package_missing {name}") from None
            if actual != expected_version:
                raise SystemExit(f"hash_lock_version_mismatch {name}")
    print(f"hash_lock_valid true packages {len(rows)}")
    print("hash_lock_target cpython-3.12-macos-arm64")
    print(f"installed_versions_checked {str(args.check_installed).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

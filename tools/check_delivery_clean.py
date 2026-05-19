from __future__ import annotations

import argparse
import os
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


ALWAYS_FORBIDDEN_NAMES = {
    ".DS_Store",
    ".cache",
    ".git",
    ".gitignore.md",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".turbo",
    ".vite",
    "__MACOSX",
    "__pycache__",
    "acceptance_logs",
    "build",
    "coverage",
    "Desktop.ini",
    "Thumbs.db",
    "htmlcov",
    "logs",
    "node_modules",
    "playwright-report",
    "tsconfig.tsbuildinfo",
    "test-results",
}
ALWAYS_FORBIDDEN_LOWER_NAMES = {name.lower() for name in ALWAYS_FORBIDDEN_NAMES}
ALWAYS_FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".jsonl",
    ".log",
    ".ndjson",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".zip",
}
ALWAYS_FORBIDDEN_PATTERNS = (
    re.compile(r"conversations.*\.json$", re.IGNORECASE),
)
FORBIDDEN_DIRECTORY_PARTS = {"archive", "exports", "logs", "acceptance_logs"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a source or runnable delivery tree/zip for generated files.")
    parser.add_argument("target", nargs="?", default=".", help="Directory or zip file to inspect.")
    parser.add_argument("--mode", choices=["source", "runnable"], default="runnable")
    args = parser.parse_args()
    target = Path(args.target).resolve()
    if target.is_file() and target.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(target) as zf:
                raw_names = [name for name in zf.namelist() if name and not name.endswith("/")]
                dangerous_members = [name.replace("\\", "/") for name in raw_names if _is_dangerous_zip_member(name)]
                names = _strip_single_zip_root(raw_names)
                bad_members = [name for name in names if is_forbidden_member(name, args.mode)]
            found = dangerous_members + bad_members
            if found:
                return _report(sorted(set(found)))
            print("delivery_clean true")
            return 0
    if not target.exists() or not target.is_dir():
        print("ERROR: target_not_directory_or_zip")
        return 2
    found: list[str] = []
    for rel, is_dir in _iter_directory_paths(target):
        if rel.parts == (".git",):
            continue
        if _is_forbidden_parts(rel.parts, rel.suffix, args.mode):
            found.append(rel.as_posix())
    return _report(sorted(set(found))) if found else _ok()


def is_forbidden_member(name: str, mode: str) -> bool:
    if _is_dangerous_zip_member(name):
        return True
    normalized = name.replace("\\", "/")
    rel = PurePosixPath(normalized)
    return _is_forbidden_parts(rel.parts, rel.suffix, mode)


def _is_dangerous_zip_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return True
    return any(part == ".." for part in PurePosixPath(normalized).parts)


def _strip_single_zip_root(names: list[str]) -> list[str]:
    normalized = [name.replace("\\", "/").lstrip("/") for name in names]
    roots = {PurePosixPath(name).parts[0] for name in normalized if PurePosixPath(name).parts}
    if len(roots) != 1:
        return normalized
    root = next(iter(roots))
    stripped = []
    for name in normalized:
        parts = PurePosixPath(name).parts
        stripped.append(PurePosixPath(*parts[1:]).as_posix() if len(parts) > 1 else "")
    return [name for name in stripped if name]


def _iter_directory_paths(target: Path):
    for root, dirs, files in os.walk(target):
        root_path = Path(root)
        rel_root = root_path.relative_to(target)
        kept_dirs = []
        for dirname in dirs:
            rel = rel_root / dirname if rel_root.parts else Path(dirname)
            yield rel, True
            if rel.parts == (".git",) or _is_forbidden_parts(rel.parts, "", "runnable"):
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs
        for filename in files:
            rel = rel_root / filename if rel_root.parts else Path(filename)
            yield rel, False


def _is_forbidden_parts(parts: tuple[str, ...], suffix: str, mode: str) -> bool:
    if not parts:
        return False
    lower_parts = tuple(part.lower() for part in parts)
    if any(part in ALWAYS_FORBIDDEN_LOWER_NAMES for part in lower_parts):
        return True
    if "dist" in lower_parts and not (mode == "runnable" and lower_parts[:2] == ("webui", "dist")):
        return True
    if any(part in FORBIDDEN_DIRECTORY_PARTS for part in lower_parts):
        return True
    if any(re.fullmatch(r"v\d+", part) for part in parts):
        return True
    name = parts[-1]
    lower_name = lower_parts[-1]
    if lower_name == ".coverage" or lower_name.startswith(".coverage."):
        return True
    if suffix.lower() in ALWAYS_FORBIDDEN_SUFFIXES:
        return True
    if any(pattern.fullmatch(name) for pattern in ALWAYS_FORBIDDEN_PATTERNS):
        return True
    return False


def _report(found: list[str]) -> int:
    print("forbidden_delivery_paths")
    for rel in found:
        print(rel)
    return 1


def _ok() -> int:
    print("delivery_clean true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

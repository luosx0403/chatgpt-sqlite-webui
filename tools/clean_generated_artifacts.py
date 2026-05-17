from __future__ import annotations

import argparse
import os
import stat
import shutil
from pathlib import Path


GENERATED_DIR_NAMES = {
    ".eggs",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__MACOSX",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "playwright-report",
    "test-results",
}
GENERATED_FILE_NAMES = {".DS_Store", "Desktop.ini", "Thumbs.db", "tsconfig.tsbuildinfo"}
GENERATED_FILE_SUFFIXES = {".pyc", ".pyo"}
GENERATED_FILE_PREFIXES = (".coverage.",)
WEBUI_GENERATED_PATHS = {
    Path("webui/.cache"),
    Path("webui/.turbo"),
    Path("webui/.vite"),
    Path("webui/coverage"),
    Path("webui/node_modules"),
    Path("webui/tsconfig.tsbuildinfo"),
}
PRESERVED_GENERATED_PATHS = {Path("webui/dist")}
SENSITIVE_DIR_NAMES = {"acceptance_logs", "archive", "exports", "logs"}
SENSITIVE_FILE_SUFFIXES = {
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".jsonl",
    ".log",
    ".ndjson",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remove generated local acceptance artifacts without touching user data.")
    parser.add_argument("--dry-run", action="store_true", help="List what would be removed without deleting it.")
    parser.add_argument("--fail-on-blocked", action="store_true", help="Return a failure code when sensitive paths need manual handling.")
    parser.add_argument("--root", default=".", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    generated = list_generated_artifacts(root)
    blocked = list_blocked_sensitive_paths(root)
    for rel, path in generated:
        print(rel.as_posix())
        if not args.dry_run:
            _remove_path(path)
    if blocked:
        print("blocked_sensitive_paths")
        for rel, _path in blocked:
            print(rel.as_posix())
    print(f"generated_artifacts_found {len(generated)}")
    print(f"blocked_sensitive_paths_found {len(blocked)}")
    print(f"dry_run {str(args.dry_run).lower()}")
    return 1 if args.fail_on_blocked and blocked else 0


def list_generated_artifacts(root: Path) -> list[tuple[Path, Path]]:
    found: list[tuple[Path, Path]] = []
    for current, dirs, files in _walk_without_git(root):
        rel_current = current.relative_to(root)
        kept_dirs = []
        for dirname in dirs:
            path = current / dirname
            rel = _join_rel(rel_current, dirname)
            if _is_preserved_generated_path(rel):
                kept_dirs.append(dirname)
                continue
            if rel in WEBUI_GENERATED_PATHS or _is_generated_dir_name(dirname):
                found.append((rel, path))
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs
        for filename in files:
            path = current / filename
            rel = _join_rel(rel_current, filename)
            if _is_generated_file(rel, filename):
                found.append((rel, path))
    found.sort(key=lambda item: item[0].as_posix())
    return _dedupe(found)


def list_blocked_sensitive_paths(root: Path) -> list[tuple[Path, Path]]:
    found: list[tuple[Path, Path]] = []
    for current, dirs, files in _walk_without_git(root):
        rel_current = current.relative_to(root)
        kept_dirs = []
        for dirname in dirs:
            path = current / dirname
            rel = _join_rel(rel_current, dirname)
            if dirname.lower() in SENSITIVE_DIR_NAMES:
                found.append((rel, path))
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs
        for filename in files:
            path = current / filename
            rel = _join_rel(rel_current, filename)
            if _is_sensitive_file(filename):
                found.append((rel, path))
    found.sort(key=lambda item: item[0].as_posix())
    return _dedupe(found)


def _remove_path(path: Path) -> None:
    if path.is_symlink():
        _unlink_symlink(path)
    elif path.is_dir():
        _rmtree(path)
    else:
        _unlink(path)


def _unlink_symlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except TypeError:  # pragma: no cover - Python < 3.8 compatibility
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _unlink(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass
    try:
        path.unlink(missing_ok=True)
    except TypeError:  # pragma: no cover - Python < 3.8 compatibility
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _rmtree(path: Path) -> None:
    kwargs = {}
    if "onexc" in shutil.rmtree.__code__.co_varnames:
        kwargs["onexc"] = _rmtree_onexc
    else:  # pragma: no cover - exercised on older Python only
        kwargs["onerror"] = _rmtree_onerror
    shutil.rmtree(path, **kwargs)


def _rmtree_onexc(function, path, excinfo) -> None:
    _make_writable(Path(path))
    function(path)


def _rmtree_onerror(function, path, excinfo) -> None:  # pragma: no cover - older Python only
    _make_writable(Path(path))
    function(path)


def _make_writable(path: Path) -> None:
    if path.is_symlink():
        return
    try:
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass


def _walk_without_git(root: Path):
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = [dirname for dirname in dirs if dirname != ".git"]
        yield Path(current), dirs, files


def _join_rel(base: Path, name: str) -> Path:
    return base / name if base.parts else Path(name)


def _is_preserved_generated_path(rel: Path) -> bool:
    return rel in PRESERVED_GENERATED_PATHS or rel.parts[:2] == ("webui", "dist")


def _is_generated_dir_name(name: str) -> bool:
    lower = name.lower()
    return lower in {item.lower() for item in GENERATED_DIR_NAMES} or lower.endswith(".egg-info")


def _is_generated_file(rel: Path, name: str) -> bool:
    lower = name.lower()
    if rel in WEBUI_GENERATED_PATHS:
        return True
    if lower in {item.lower() for item in GENERATED_FILE_NAMES}:
        return True
    if lower == ".coverage" or any(lower.startswith(prefix) for prefix in GENERATED_FILE_PREFIXES):
        return True
    return Path(name).suffix.lower() in GENERATED_FILE_SUFFIXES


def _is_sensitive_file(name: str) -> bool:
    lower = name.lower()
    return Path(lower).suffix in SENSITIVE_FILE_SUFFIXES


def _dedupe(items: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    seen: set[str] = set()
    deduped: list[tuple[Path, Path]] = []
    for rel, path in items:
        key = rel.as_posix()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((rel, path))
    return deduped


if __name__ == "__main__":
    raise SystemExit(main())

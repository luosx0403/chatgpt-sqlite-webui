from __future__ import annotations

"""Generate a clean runnable release ZIP from the project root."""

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

INCLUDE_PATHS = [
    "chatgpt_archive.py",
    "chatgpt_export_archiver/",
    "tests/",
    "tools/",
    "webui/dist/",
    "webui/src/",
    "webui/tests/",
    "webui/index.html",
    "webui/tsconfig.json",
    "webui/vite.config.ts",
    "webui/package.json",
    "webui/package-lock.json",
    "README.md",
    "README.zh-CN.md",
    "README.zh-TW.md",
    "README.ja-JP.md",
    "README.es-ES.md",
    "LICENSE",
    "AGENTS.md",
    ".gitignore",
    "requirements-web.txt",
]

EXCLUDE_GLOBS = [
    ".git",
    ".git/**",
    "node_modules",
    "**/node_modules/**",
    "__pycache__",
    "**/__pycache__/**",
    "*.pyc",
    "**/*.pyc",
    "*.pyo",
    "**/*.pyo",
    ".DS_Store",
    "**/.DS_Store",
    "**/._*",
    "__MACOSX",
    "__MACOSX/**",
    "**/Thumbs.db",
    "**/Desktop.ini",
    "webui/tsconfig.tsbuildinfo",
    "webui/.vite",
    "webui/.vite/**",
    "archive",
    "archive/**",
    "exports",
    "exports/**",
    "*.db",
    "*.db-journal",
    "*.db-shm",
    "*.db-wal",
    "*.sqlite",
    "*.sqlite-journal",
    "*.sqlite-shm",
    "*.sqlite-wal",
    "*.sqlite3",
    "*.sqlite3-journal",
    "*.sqlite3-shm",
    "*.sqlite3-wal",
    "*.zip",
    "conversations*.json",
    "*.jsonl",
    "*.ndjson",
    "logs",
    "logs/**",
    "*.log",
    ".coverage",
    ".coverage.*",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
    ".tox/**",
    ".nox/**",
    "htmlcov/**",
    "build/**",
    "dist/**",
    ".eggs/**",
    "*.egg-info/**",
    "playwright-report/**",
    "test-results/**",
    "acceptance_logs/**",
    "*.gitignore.md",
    # Exclude generated build output at root, but NOT webui/dist
    "dist/",
    "dist/**",
    "build/**",
]


def _matches_any(path: str, globs: list[str]) -> bool:
    from fnmatch import fnmatch

    for g in globs:
        if fnmatch(path, g):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a clean runnable release ZIP.")
    parser.add_argument("--output", "-o", default="release.zip", help="Output ZIP path")
    parser.add_argument("--check", action="store_true", default=True,
                        help="Run delivery check on generated ZIP")
    parser.add_argument("--no-check", action="store_false", dest="check",
                        help="Skip delivery check")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()

    exclude_set = set(
        p.lower().replace("\\", "/") for p in EXCLUDE_GLOBS
    )

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for inc in INCLUDE_PATHS:
            src = root / inc
            if not src.exists():
                print(f"WARNING: include path does not exist: {inc}")
                continue
            if src.is_file():
                arcname = inc
                if inc == ".gitignore":
                    continue
                if not _matches_any(arcname, EXCLUDE_GLOBS):
                    zf.write(src, arcname)
            else:
                for fpath in sorted(src.rglob("*")):
                    if not fpath.is_file():
                        continue
                    arcname = fpath.relative_to(root).as_posix()
                    if _matches_any(arcname, EXCLUDE_GLOBS):
                        continue
                    zf.write(fpath, arcname)

        zf.writestr(".gitignore", (root / ".gitignore").read_text(encoding="utf-8"))

    print(f"Release ZIP written to: {output}")
    print(f"ZIP size: {output.stat().st_size / 1024 / 1024:.1f} MB")

    if args.check:
        check_tool = root / "tools" / "check_delivery_clean.py"
        result = subprocess.run(
            [sys.executable, str(check_tool), "--mode", "runnable", str(output)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("delivery_clean: PASS")
        else:
            print(f"delivery_clean: FAIL\n{result.stdout}")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

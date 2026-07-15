from __future__ import annotations

"""Generate and atomically publish a verified runnable release ZIP."""

import argparse
import hashlib
import json
import os
from html.parser import HTMLParser
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

INCLUDE_PATHS = [
    "chatgpt_archive.py", "chatgpt_export_archiver/", "tests/", "tools/",
    "webui/dist/", "webui/src/", "webui/tests/", "webui/index.html",
    "webui/tsconfig.json", "webui/vite.config.ts", "webui/package.json",
    "webui/package-lock.json", "README.md", "README.zh-CN.md",
    "README.zh-TW.md", "README.ja-JP.md", "README.es-ES.md", "LICENSE",
    "AGENTS.md", ".gitignore", "requirements-web.txt", "constraints-web-py312.txt",
]

AUTHORITATIVE_REQUIRED_FILES = (
    "chatgpt_archive.py",
    "chatgpt_export_archiver/__init__.py",
    "chatgpt_export_archiver/current_path.py",
    "chatgpt_export_archiver/search.py",
    "chatgpt_export_archiver/exporter.py",
    "chatgpt_export_archiver/logging_utils.py",
    "chatgpt_export_archiver/cli.py",
    "chatgpt_export_archiver/db.py",
    "chatgpt_export_archiver/parser.py",
    "chatgpt_export_archiver/scanner.py",
    "chatgpt_export_archiver/schema_contract.py",
    "chatgpt_export_archiver/sqlite_errors.py",
    "chatgpt_export_archiver/utils.py",
    "chatgpt_export_archiver/web_api.py",
    "chatgpt_export_archiver/web_app.py",
    "chatgpt_export_archiver/web_db.py",
    "chatgpt_export_archiver/web_jobs.py",
    "tools/benchmark_effective_current.py",
    "tools/check_delivery_clean.py",
    "tools/clean_generated_artifacts.py",
    "tools/make_release_zip.py",
    "webui/index.html",
    "webui/tsconfig.json",
    "webui/vite.config.ts",
    "webui/src/App.tsx",
    "webui/src/api/client.ts",
    "webui/src/components/ConversationPane.tsx",
    "webui/src/components/MessageBlock.tsx",
    "webui/src/components/SearchHelp.tsx",
    "webui/src/components/SettingsPanel.tsx",
    "webui/src/components/Sidebar.tsx",
    "webui/src/hooks/useModalFocus.ts",
    "webui/src/i18n.ts",
    "webui/src/main.tsx",
    "webui/src/settings.ts",
    "webui/src/styles.css",
    "webui/src/types.ts",
    "webui/src/utils/format.ts",
    "webui/src/utils/interaction.ts",
    "webui/src/utils/querySyntax.ts",
    "tests/test_archiver.py",
    "tests/test_web_api.py",
    "tests/fixtures/legacy-fa37b3d.sql",
    "tests/fixtures/legacy-fa37b3d.json",
    "webui/tests/dom-smoke.mjs",
    "webui/package.json",
    "webui/package-lock.json",
    "requirements-web.txt",
    "constraints-web-py312.txt",
    "README.md", "README.zh-CN.md", "README.zh-TW.md", "README.ja-JP.md", "README.es-ES.md",
    "AGENTS.md",
    "LICENSE",
    ".gitignore",
    "webui/dist/index.html",
)

# Backward-compatible name for tests and local tooling that imported it.
REQUIRED_LEAF_PATHS = AUTHORITATIVE_REQUIRED_FILES

MANIFEST_NAME = "RELEASE-MANIFEST.json"

EXCLUDE_GLOBS = [
    ".git", ".git/**", "node_modules", "**/node_modules/**", "__pycache__",
    "**/__pycache__/**", "*.pyc", "**/*.pyc", "*.pyo", "**/*.pyo",
    ".DS_Store", "**/.DS_Store", "**/._*", "__MACOSX", "__MACOSX/**",
    "**/Thumbs.db", "**/Desktop.ini", "webui/tsconfig.tsbuildinfo",
    "webui/.vite", "webui/.vite/**", "archive", "archive/**", "exports",
    "exports/**", "*.db", "*.db-journal", "*.db-shm", "*.db-wal",
    "*.sqlite", "*.sqlite-journal", "*.sqlite-shm", "*.sqlite-wal",
    "*.sqlite3", "*.sqlite3-journal", "*.sqlite3-shm", "*.sqlite3-wal",
    "*.zip", "conversations*.json", "*.jsonl", "*.ndjson", "logs", "logs/**",
    "*.log", ".coverage", ".coverage.*", ".pytest_cache/**", ".mypy_cache/**",
    ".ruff_cache/**", ".tox/**", ".nox/**", "htmlcov/**", "build/**",
    "dist/**", ".eggs/**", "*.egg-info/**", "playwright-report/**",
    "test-results/**", "acceptance_logs/**", "*.gitignore.md", "dist/", "dist/**",
]


def _matches_any(path: str) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(path, pattern) for pattern in EXCLUDE_GLOBS)


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        value = values.get("src") if tag == "script" else values.get("href") if tag == "link" else None
        if not value:
            return
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or value.startswith("//"):
            raise ValueError(f"dist_external_asset_not_allowed {value}")
        asset = parsed.path.lstrip("/")
        if asset:
            self.assets.add(asset)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_required(root: Path) -> None:
    missing = [path for path in AUTHORITATIVE_REQUIRED_FILES if not (root / path).is_file()]
    missing.extend(path.rstrip("/") for path in INCLUDE_PATHS if path.endswith("/") and not (root / path).is_dir())
    if missing:
        raise ValueError("required_release_paths_missing " + " ".join(sorted(set(missing))))


def _dist_assets(root: Path) -> set[str]:
    parser = _AssetParser()
    parser.feed((root / "webui/dist/index.html").read_text(encoding="utf-8"))
    missing = [asset for asset in parser.assets if not (root / "webui/dist" / asset).is_file()]
    if missing:
        raise ValueError("dist_missing_assets " + " ".join(sorted(missing)))
    return parser.assets


def _collect_payload(root: Path) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    for include in INCLUDE_PATHS:
        source = root / include
        paths = [source] if source.is_file() else sorted(source.rglob("*"))
        for path in paths:
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if _matches_any(relative):
                continue
            payload[relative] = path.read_bytes()
    return dict(sorted(payload.items()))


def _manifest(payload: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {"path": path, "size": len(data), "sha256": _sha256(data)}
        for path, data in payload.items()
    ]


def _manifest_bytes(payload: dict[str, bytes]) -> bytes:
    return (json.dumps(_manifest(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_archive(path: Path, payload: dict[str, bytes]) -> None:
    def write_member(archive: zipfile.ZipFile, relative: str, data: bytes) -> None:
        info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (0o100644 & 0xFFFF) << 16
        info.internal_attr = 0
        info.extra = b""
        info.comment = b""
        archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.comment = b""
        for relative, data in payload.items():
            write_member(archive, relative, data)
        write_member(archive, MANIFEST_NAME, _manifest_bytes(payload))
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _verify_archive(path: Path, payload: dict[str, bytes]) -> list[dict[str, object]]:
    expected_manifest = _manifest(payload)
    expected_names = set(payload) | {MANIFEST_NAME}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("release_duplicate_members")
        if set(names) != expected_names:
            missing = sorted(expected_names - set(names))
            extra = sorted(set(names) - expected_names)
            raise ValueError(f"release_member_set_mismatch missing={missing} extra={extra}")
        stored_manifest = json.loads(archive.read(MANIFEST_NAME))
        if stored_manifest != expected_manifest:
            raise ValueError("release_manifest_mismatch")
        for item in expected_manifest:
            data = archive.read(str(item["path"]))
            if len(data) != item["size"] or _sha256(data) != item["sha256"]:
                raise ValueError(f"release_hash_mismatch {item['path']}")
    return expected_manifest


def _delivery_check(root: Path, archive: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(root / "tools/check_delivery_clean.py"), "--mode", "runnable", str(archive)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError("delivery_check_failed " + (result.stdout + result.stderr).strip())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def build_release(root: Path, output: Path, *, check: bool = True) -> tuple[list[dict[str, object]], int]:
    _validate_required(root)
    assets = _dist_assets(root)
    payload = _collect_payload(root)
    authoritative_payload = set(AUTHORITATIVE_REQUIRED_FILES) | {
        f"webui/dist/{asset}" for asset in assets
    }
    missing_payload = sorted(authoritative_payload - set(payload))
    if missing_payload:
        raise ValueError("required_release_payload_missing " + " ".join(missing_payload))
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp.zip", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _write_archive(temporary, payload)
        manifest = _verify_archive(temporary, payload)
        if check:
            _delivery_check(root, temporary)
        os.replace(temporary, output)
        _fsync_directory(output.parent)
        return manifest, len(assets)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a clean runnable release ZIP.")
    parser.add_argument("--output", "-o", default="release.zip", help="Output ZIP path")
    parser.add_argument("--check", "--verify", action="store_true", default=True, help="Run delivery check")
    parser.add_argument("--no-check", action="store_false", dest="check", help="Skip delivery check")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    try:
        manifest, asset_count = build_release(root, output, check=args.check)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}")
        return 2
    manifest_data = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    print("release_zip_written true")
    print(f"zip_size_bytes {output.stat().st_size}")
    print(f"dist_assets_verified {asset_count}")
    print(f"manifest_files {len(manifest)}")
    print(f"manifest_sha256 {_sha256(manifest_data)}")
    print("delivery_clean: PASS" if args.check else "delivery_clean: SKIPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

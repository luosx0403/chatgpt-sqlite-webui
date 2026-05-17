# ChatGPT Export Archiver

Language: English | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

ChatGPT Export Archiver is a local, privacy-oriented toolkit for turning OpenAI / ChatGPT export ZIP files into a searchable SQLite archive. It keeps the raw export out of the browser, supports repeatable incremental imports, offers CLI export and search tools, and includes a local React Web UI for browsing and importing ZIP files.

## What this project does

- Imports `conversations.json` from an OpenAI / ChatGPT export ZIP or extracted export directory into SQLite.
- Preserves conversation metadata, mapping nodes, message roles, text content, timestamps, parent links, and import warnings.
- Supports incremental imports. Re-importing a newer export updates changed conversations without intentionally duplicating unchanged data.
- Builds an optional FTS5 message index for CLI search.
- Builds optional Web substring indexes for faster browser search.
- Exports conversations as Markdown, TXT, or both.
- Provides `verify`, `stats`, and privacy-preserving `inspect` commands that avoid printing message text.
- Provides a local Web UI that can start without an existing database and can import ZIP files from the browser.
- Keeps logging separate from structured command output and avoids logging titles, snippets, raw JSON, or message bodies.

## Privacy

Everything runs locally. The database, generated exports, temporary upload copies, Web UI, and logs stay on your machine unless you move or publish them yourself. The CLI deliberately prints IDs, counts, timestamps, and status lines rather than message snippets. CLI summaries and logs do not print chat message bodies, titles, snippets, raw JSON, full input/output paths, or real ZIP file names; import summaries report the input kind such as `source zip`. The Web UI is intended for local use and binds to `127.0.0.1` by default.

In import summaries, `valid_conversations` counts parsed input conversation elements before duplicate-id coalescing. When duplicate ids are merged, it can be larger than the final `inserted_conversations`, `updated_conversations`, or `unchanged_conversations` database-change counts.

`inspect` and scanner errors avoid printing real ZIP names or full paths by default. Existing-database CLI commands such as `verify`, `stats`, `search`, and `export` report `database_not_found` when the database path is wrong and do not create an empty SQLite file. Web search uses optional trigram indexes as a candidate layer when available, then still applies the normalized substring filters so short queries, symbols, and unsupported trigram cases fall back safely.

`--delete-input-on-success` only runs after the main import transaction succeeds. When the explicit input path is a symlink, it removes the symlink itself after a successful import, not the file it points to.

The database and exported Markdown or TXT files may still contain private conversation content. Treat `archive/*.db`, exported files, and your original ChatGPT export ZIPs as sensitive data.

## Requirements

- Python 3.10 or newer.
- SQLite with JSON1 and FTS5 enabled. Most current Python builds on macOS, Windows, and Linux include both.
- Node.js and npm only if you want to rebuild the React Web UI or run frontend checks. The runnable delivery includes `webui/dist`, so normal local Web UI use does not require rebuilding the frontend.
- For Web ZIP upload support, install the Web requirements from `requirements-web.txt`.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

On Windows PowerShell:

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

On Windows cmd.exe:

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

## Quick start

Put your ChatGPT export ZIP somewhere outside the repository, then run the fastest safe import command. This skips input hashing and rebuilds FTS once at the end, which is much faster for large archives than maintaining FTS row by row.

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows PowerShell equivalent:

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows cmd.exe equivalent:

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Start the local Web UI:

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

If no database exists yet, the Web UI still starts and shows an empty state with an import panel. You can choose a ChatGPT export ZIP in the browser; the backend writes a temporary local copy, imports it, then automatically runs `verify`, `stats`, and `web-index`.

```bash
python chatgpt_archive.py web --port 8787
```

## Quick CLI workflow

Inspect an export without printing chat content:

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

Create an empty database explicitly:

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

Import an export with the large-archive path:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

Verify structural consistency:

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

Show structured counts and time bounds:

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Search message text through the CLI search path. This prints conversation IDs, node IDs, and roles, not snippets:

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

Export the conversation as Markdown, TXT, or both formats in the same run. `--format md` writes Markdown body files and updates the manifest, `--format txt` writes plain text body files and updates the manifest, and `--format all` writes both body formats and updates the manifest:

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

Export a date range and rewrite existing files if needed. Date boundaries for `--from` and `--to` accept only `YYYY-MM-DD`:

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

The export summary reports body-file counts. `written` counts Markdown/TXT body files whose final bytes changed, and `skipped_unchanged` counts unchanged Markdown/TXT body files. Manifest files are updated as needed but are not included in those two counts.

Rebuild optional Web search indexes:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

Start the Web UI:

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## Import modes

The recommended large-archive command is:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

If you want SQLite to spend extra time tidying planner statistics and the FTS index after import, use:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` is intentionally off by default. Only use it when you already have another backup of the ZIP. Deletion happens only after the main import transaction succeeds. If deletion succeeds, the CLI prints `deleted_input True` without a path. If deletion fails, the import still succeeds, the run remains `finished`, a structured `delete_input_failed` warning is stored, and the CLI prints only `delete_input_failed True` plus the exception type.

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

Incremental imports are normal. Importing a newer export into the same database updates conversations that changed and preserves the rest of the archive.

## Web UI workflow

The Web UI is a local React app served by FastAPI. The preferred path is to serve the prebuilt `webui/dist` files included in the runnable tree.

```bash
python chatgpt_archive.py web --port 8787
```

The Web UI can be used in two ways. If the database already exists, pass it explicitly or let the default path be used. If the database does not exist, start the Web UI anyway, then use the import panel to upload a ChatGPT export ZIP. Upload imports are serialized so that only one SQLite writer runs in the process at a time.

After a successful Web upload import, the backend runs the same core import pipeline as the CLI, then runs `verify`, `stats`, and `web-index`. The uploaded ZIP is a temporary server-side copy and is cleaned up independently from the original file on your disk.


## Web UI acceptance checklist

Use this checklist when changing the Web path or preparing a runnable delivery:

- Start the Web UI with no database and confirm that it serves the empty-state contract.
- Import a small ChatGPT export ZIP from the browser and confirm that the job finishes.
- Confirm that the backend runs `verify`, `stats`, and `web-index` after the upload import.
- Refresh the page and confirm that conversations can be listed and opened.
- Re-import a newer ZIP and confirm that the incremental path still works.

The Web path should not require `webui/node_modules` in a runnable delivery because the built React assets are served from `webui/dist`.

## Search syntax

CLI search uses the project's safe query syntax, not raw SQLite query text. Use plain keywords, quoted phrases, `-term` exclusions, `OR`, and filters such as `role:user`, `source:zip`, `path:current`, `path:all`, `scope:title`, and `scope:message`. It prints conversation IDs, node IDs, and roles, not snippets.

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web search uses optional normalized trigram indexes built by `web-index`. This is designed for practical substring search in the browser. If those optional indexes are missing or damaged, rebuild them:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## Verification and optional Web indexes

`verify` checks SQLite integrity and project-level consistency, including missing current nodes, broken parent links, empty conversations, and parent cycles.

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

If `PRAGMA integrity_check` reports a malformed FTS5 inverted index for `web_message_trigram` or `web_title_trigram`, the core conversation data may still be structurally valid while the optional Web search index is damaged. In that case `verify` reports `optional_web_index_error true` and prints a recovery hint. Rebuild the optional Web indexes with:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

The diagnostic is conservative. It is marked as an optional Web index issue only when all integrity-check errors can be attributed to those optional Web index tables or their FTS5 shadow tables.

## Logging

The log levels are `debug`, `info`, `warning`, `error`, and `none`. The default is `warning`. More detailed levels include the quieter levels after them. Logs do not include titles, snippets, raw JSON, or message bodies.

Logging flags can be placed before or after the subcommand:

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

Keep JSON logs in ignored locations such as `logs/`. Files ending in `*.jsonl` are local log artifacts and are rejected by delivery clean.

Import timing fields include `source_scan_seconds`, `parse_and_upsert_seconds`, `fts_rebuild_seconds`, `finalize_commit_seconds`, `close_seconds`, `legacy_pre_commit_seconds`, `wall_total_seconds`, and `total_import_seconds`. `total_import_seconds` is the end-to-end wall time, including final commit and close.

After the import transaction has successfully finished, later summary updates are best-effort. `summary_update_after_commit_failed`, `import_connection_close_failed`, and `summary_update_after_close_failed` are warnings, not reasons to mark a successful import as failed.

## Development and acceptance checks

Run the Python checks and clean safe generated artifacts before the first delivery check:

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

Build and smoke-test the Web UI:

```bash
cd webui
npm ci
npm run typecheck
npm run build
npm run test:python-resolution
npm run test:dom
cd ..
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

`clean_generated_artifacts.py` is cross-platform and keeps `webui/dist`. It removes safe regenerated files only. It will not delete databases, ZIP files, SQLite sidecars, `archive/`, `exports/`, or `logs/`; if delivery clean still reports those sensitive paths, move them outside the project root or remove them manually. The acceptance commands use `--fail-on-blocked` so sensitive leftovers stop the delivery flow immediately.

On Windows PowerShell or cmd, use double quotes for search queries that contain spaces, for example `"python sqlite"` or `"role:user path:current python -pandas"`. The Python, Web, Web index, typecheck, build, cleanup, and delivery-check commands above work on macOS, Windows, and Linux when Python and Node are on `PATH`. If your Windows install uses the Python launcher, run the cleanup helper as `py -3 tools/clean_generated_artifacts.py --fail-on-blocked`.

When checking a ZIP delivery:

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## Delivery notes

A runnable delivery should include the Python sources, tests, docs, `requirements-web.txt`, and `webui/dist`. It should not include `webui/node_modules`, `webui/tsconfig.tsbuildinfo`, Python cache directories or bytecode, coverage/typecheck caches, `.DS_Store`, `__MACOSX`, `Thumbs.db`, `Desktop.ini`, `.gitignore.md`, temporary logs, local acceptance logs, `*.log`, `*.ndjson`, `*.jsonl`, `archive/`, `exports/`, any `*.zip`, `conversations*.json`, real databases such as `*.db`, `*.sqlite`, and `*.sqlite3`, or SQLite sidecars such as `*.db-journal`, `*.sqlite-wal`, `*.sqlite-shm`, `*.sqlite-journal`, `*.sqlite3-wal`, `*.sqlite3-shm`, and `*.sqlite3-journal`. Directory checks allow the target root's own `.git` directory so a normal Git clone can be checked, but nested `.git` directories are forbidden; ZIP delivery checks forbid any `.git` entry.

A source-only delivery may omit `webui/dist`, but then the frontend must be rebuilt before serving the full React UI.

## Source tree guide

```text
chatgpt_archive.py                 CLI entry point
chatgpt_export_archiver/cli.py     CLI commands and reusable import pipeline
chatgpt_export_archiver/db.py      SQLite schema, import helpers, verify, stats, FTS helpers
chatgpt_export_archiver/web_app.py FastAPI app factory and static UI serving
chatgpt_export_archiver/web_api.py Web API routes
chatgpt_export_archiver/web_db.py  Web query helpers and optional trigram index builder
chatgpt_export_archiver/web_jobs.py Web ZIP import job manager
webui/                             React frontend source and built dist files
tests/                             Python unit and integration tests
tools/                             Delivery and support scripts
```

## Database overview

The main database stores conversations, mapping nodes, import runs, and warnings. The CLI FTS table is `message_fts`. Optional Web search helper tables include `web_message_norm`, `web_title_norm`, `web_message_trigram`, and `web_title_trigram` plus SQLite FTS5 shadow tables.

The project intentionally does not change the database schema during small robustness fixes unless a migration is explicitly planned and documented.

## Known limits

- This is a local archive tool, not a cloud sync service.
- The Web UI is intended for local use. Do not expose it to an untrusted network without adding your own access controls.
- Export parsing follows the observed OpenAI / ChatGPT export format. If the upstream export shape changes, `inspect` and tests should be updated before trusting a new import path.
- Very large archives can take time to import, rebuild FTS, and build Web trigram indexes. Prefer the `--rebuild-fts` path for large imports.

# ChatGPT Export Archiver

Language: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

Turn your official ChatGPT export ZIP into a private, searchable SQLite knowledge archive.

`Local-first` · `SQLite` · `Privacy-first` · `Fast import` · `Fast Web index` · `Chat-style Web UI` · `Markdown/TXT export`

ChatGPT Export Archiver imports OpenAI / ChatGPT export ZIP files directly into SQLite, verifies the archive, builds searchable indexes, opens a local Web UI, and exports conversations to Markdown or TXT. It is built for long-term personal archives, offline search, knowledge-base migration, and the workflows the official history UI does not expose as local files and indexes.

## Why Use It

- **Local-first and private.** Your ZIP, database, exports, temporary upload copies, Web UI, and logs stay on your machine unless you move them yourself.
- **Direct ZIP import.** Import the official ChatGPT export ZIP without manually unpacking or merging shards.
- **Large-archive friendly.** The recommended import path supports large ZIP files, incremental re-imports, deferred FTS rebuilds, and optimized optional Web search indexes.
- **Chat-style reader.** The Web UI now defaults to a ChatGPT-like layout: user messages on the right, assistant messages on the left, and system/internal messages collapsed but expandable.
- **Classic technical view remains.** Use Settings or `?layout=classic` / `?messageLayout=classic` to switch back to the older row-by-row layout.
- **Archive-grade search.** For archive workflows, local SQLite search gives you more control than the official history UI: role/title/source/scope/exclude filters, phrases, OR, pagination, verification, rebuildable indexes, and export.
- **Portable exports.** Markdown and TXT exports are deterministic and suitable for backups, local knowledge bases, offline grep, or migration.

## Screenshot

Safe screenshot coming soon. Screenshots should use synthetic conversations only, not real chat titles, snippets, raw JSON, emails, or local paths.

## Local Smoke Observations

These are example observations from one local machine, not a universal guarantee:

- A roughly 2.25 GB real export ZIP imported in about 98 seconds with the large-archive path.
- `verify` on that archive completed in about 4 seconds.
- An optional Web index rebuild after a larger incremental archive completed in about 106 seconds.
- A high-hit Web message search over the local Uvicorn app returned in about 0.3 seconds.

## What This Project Does

- Imports `conversations.json` and sharded `conversations-*.json` files from an OpenAI / ChatGPT export ZIP, a standalone `conversations.json` file, or an extracted export directory into SQLite.
- Preserves conversation metadata, mapping nodes, message roles, text content, timestamps, parent links, source tracking, and import warnings.
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

- Python 3.10 or newer. Python 3.12 is the reproducible Web dependency test target.
- SQLite with JSON support. FTS5 is optional: when unavailable or when `message_fts` is missing, safe scan search remains available.
- Node.js and npm only if you want to rebuild the React Web UI or run frontend checks. The runnable delivery includes `webui/dist`, so normal local Web UI use does not require rebuilding the frontend.
- Core CLI commands use only Python's standard library and work without Web packages. To run the Web UI, including ZIP upload, install `requirements-web.txt`; the `web` command fails fast with an install hint when that profile is absent.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

On Windows PowerShell:

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

On Windows cmd.exe:

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

The Python 3.12 constraints file pins every resolved Web dependency version, but it is not a cross-platform hash lock. A future release workflow should generate and verify platform-specific hashes for the supported Python/OS matrix; until then, install only from a trusted package index and retain the npm lockfile and audit checks as separate frontend controls.

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

`--input` may point at the official export ZIP, a standalone `conversations.json`, or an extracted export directory. Extracted directories may contain either `conversations.json` or sharded `conversations-*.json` files; do not merge shards manually.

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
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

CLI and Web exports default to the effective current path and visible messages only. Use `--path all` and/or `--include-internal` explicitly when the export should include branches or internal messages; the manifest records both choices. CLI export reads conversation nodes in bounded batches, while Web download and `Copy current path conversation` use dedicated bounded server-side text streams. Complete canonical text and eligible legacy/raw recovered text are therefore exported without depending on the reader response budget.

Rebuild optional Web search indexes:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

`web-index` scans and normalizes messages, normalizes titles, builds message/title trigram indexes when supported, writes generation metadata, and commits in explicit observable stages. Every data stage uses bounded keyset batches plus byte budgets and resolves each message once. The rebuild stays in one atomic SQLite transaction: readers keep seeing the previous current optional index until commit, while generation/metadata failure, SQLite interruption, disk error, or an internal/CLI cancellation callback rolls back all replacement objects. This holds `BEGIN IMMEDIATE` and one writer slot for the complete build and may use temporary disk. Web import-job JSON exposes stage and processed/total, but this release has no independent Web index-build job or Web cancel endpoint. Oversized rows are recorded and canonical-verifier scanned, preserving recall.

Start the Web UI:

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## Import modes

The recommended large-archive command is:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

The input may be a ZIP, a single `conversations.json`, or an extracted directory containing `conversations.json` or sharded `conversations-*.json` files. Scanner discovery ignores macOS metadata paths such as `__MACOSX`, AppleDouble `._*` files, and `.DS_Store`, so those local artifacts do not become conversation sources.

For directory input, POSIX platforms use component-by-component `dir_fd` plus `O_NOFOLLOW` where available. The portable fallback rejects symlink/reparse components and verifies containment immediately before path-based open, but the Python standard library cannot eliminate every local replacement race on every platform. Do not import an extracted directory that an untrusted local user or concurrent process can modify; use the original read-only ZIP for that threat model.

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

The default reader layout is `chat`: user messages align right, assistant messages align left, and system/internal messages appear as collapsed disclosure notes. To use the older row-by-row technical layout, open Settings and choose `Classic`, or add `?layout=classic` or `?messageLayout=classic` to the Web UI URL.

`path=current` uses one shared effective-current rule everywhere: a valid conversation-owned `current_node` and its parent chain win even when all raw flags are zero; otherwise a deterministic usable `is_on_current_path=1` leaf chain is selected; only when neither exists does that conversation fall back to all nodes. Responses keep the raw flag unchanged and expose `current_node_exists`, `current_collection_source`, `current_path_fallback_to_all`, `effective_path`, and per-node effective visibility. Broken parents and cycles terminate with deterministic diagnostics rather than recursive queries hanging.

Global current-path search first derives a path-independent conversation candidate set from normalized text/title indexes and safe source/date/role predicates, then builds effective-current membership only for those candidates. Exclusion-only queries that cannot be narrowed use an explicit full-database fallback. Reader hit navigation fetches one compact page initially and appends more hits only as navigation approaches the loaded boundary. Search and Web-index SQL use a portable non-flattening query shape instead of requiring SQLite's `AS MATERIALIZED` syntax, while resolving each legacy raw candidate at most once per logical stage.

Reader copy and export actions follow the visible reader contract. `Copy current path conversation` uses the dedicated complete-text stream for the current reader path and respects the Show internal messages toggle, while ignoring the current search filters. It does not accumulate reader pages in the browser. `Copy visible` copies only the already loaded visible messages. Download links use the same current path and Show internal setting. Raw message access is a bounded larger raw preview through the per-message endpoint; truncated responses must render `raw_text` as plain preview text and the UI only shows that capped preview.

Reader jump-to-hit requests with `around_node_id` use the same pagination collection as the reader: visible-only rows when Show internal is off, the full node collection when Show internal is on, and the effective all-node collection for damaged conversations with no current-path nodes.

The Web UI can be used in two ways. If the database already exists, pass it explicitly or let the default path be used. If the database does not exist, start the Web UI anyway, then use the import panel to upload a ChatGPT export ZIP. Upload imports are serialized so that only one SQLite writer runs in the process at a time.

After a successful Web upload import, the backend runs the same core import pipeline as the CLI, then runs `verify`, `stats`, and `web-index`. The uploaded ZIP is a temporary server-side copy and is cleaned up independently from the original file on your disk.

Preflight failures and terminal import jobs may return multiple `cleanup_warnings`. The React UI renders every safe warning code and `path_kind` with localized user-facing text, while retaining the deprecated single `cleanup_warning` fallback. It never displays temporary paths, filenames, OS messages, or error-class details from these warnings, and repeated polling replaces the same job snapshot instead of appending duplicate announcements.

If the prebuilt React application cannot be served, the server's fallback HTML is an emergency, deliberately limited interface, not a substitute for the full reader. It has reduced search/reader controls and downloads exclude internal nodes unless explicitly requested. Rebuild `webui/dist` for the complete UI.

## Web upload security limits

Web uploads enforce application-level safety limits before the import job starts. These are controlled by environment variables and are independent of CLI `import`, which does not use these limits.

The Web upload pending slot is reserved before reading the file so a large upload cannot race with another writer. Any error after that reservation, including temporary upload path creation failure, must release the slot and clean the server-side temporary directory; a successful import job takes ownership of the slot and temporary copy.

A process kill, OOM, or host crash can bypass normal cleanup and leave an old `chatgpt-archive-upload-*` directory under the operating-system temporary directory. This release does not delete such directories automatically because an ownership/age mistake could remove unrelated data. With the server stopped, an administrator may remove an individual old directory only after verifying the exact prefix, current-account ownership, age, absence of symlinks/reparse points, and that no job uses it; never delete a user export ZIP or use an unchecked wildcard.

When the Web UI is bound to a loopback address (`127.0.0.1`, `localhost`, `::1`), the defaults allow large trusted archives:

| Environment variable | Local default | Controls |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | Total compressed ZIP upload size |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | Max uncompressed size of a single JSON member |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | Max number of conversation JSON members |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | Max total uncompressed JSON data |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | Max compression ratio for large JSON members |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | Max total ZIP members |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | unset | Set to `local` only on trusted non-loopback networks to use local defaults for unset upload limits |

**Remote binding policy.** A non-loopback bind (for example `0.0.0.0`, `::`, or a LAN IP) is rejected unless `CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS=true`, `CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true`, or `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local` explicitly opts in. The server then warns that the archive browser is exposed and applies conservative remote-safe defaults: 128 MiB compressed ZIP, 256 MiB per JSON member, 512 MiB total uncompressed, 200.0 compression ratio, 200 JSON members, and 10,000 total ZIP members. Trusted loopback/local defaults use a 1000.0 compression ratio. `CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true` permits only explicitly configured per-limit overrides; unset limits stay remote-safe. `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local` restores every unset limit to its local large default and is appropriate only on a trusted LAN. There is no authentication layer: the trust boundary is the network and host firewall. Upload writes enforce Origin/Sec-Fetch checks, but those checks do not make an untrusted network safe.

`/api/schema` reports the current effective upload policy for the running host, including its multipart-body limit (ZIP byte limit plus bounded overhead), whether remote-safe, explicit remote override, or local-profile limits are active. The writer slot and receive-level body cap apply before multipart parsing. Multipart parsers may spool to disk and the import pipeline then owns a server-side temporary ZIP, so allow temporary disk headroom approaching two compressed copies plus database growth. ZIP checks run before import, but decoded JSON parsing, SQLite writes, and `web-index` rebuild still consume memory, disk, and CPU proportional to decoded JSON size. Remote uploads require a valid `Content-Length`; loopback chunked uploads are still bounded while streaming. For very large archives, prefer a trusted loopback environment, CLI import, and ample disk and memory.

To raise a local limit for a legitimate large archive, set the corresponding variable before starting the Web UI:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=64424509440  # 60 GiB
python chatgpt_archive.py web --port 8787
```

```powershell
# Windows PowerShell
$env:CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES = 64424509440  # 60 GiB
python chatgpt_archive.py web --port 8787
```

```batch
:: Windows cmd.exe
set CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=64424509440
python chatgpt_archive.py web --port 8787
```

To allow an explicit large compressed ZIP limit on a trusted internal network while keeping other unset limits remote-safe:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true
export CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=10737418240  # 10 GiB
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

```powershell
# Windows PowerShell
$env:CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS = "true"
$env:CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES = 10737418240  # 10 GiB
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

```batch
:: Windows cmd.exe
set CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true
set CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=10737418240
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

To use the full local upload profile on a trusted internal network:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

Set higher limits only for local files you trust. Larger values raise ZIP bomb, disk-pressure, and CPU/memory risk.


## Web UI acceptance checklist

Use this checklist when changing the Web path or preparing a runnable delivery:

- Start the Web UI with no database and confirm that it serves the empty-state contract.
- Import a small ChatGPT export ZIP from the browser and confirm that the job finishes.
- Confirm that the backend runs `verify`, `stats`, and `web-index` after the upload import.
- Refresh the page and confirm that conversations can be listed and opened.
- Re-import a newer ZIP and confirm that the incremental path still works.

The Web path should not require `webui/node_modules` in a runnable delivery because the built React assets are served from `webui/dist`.

## Search syntax

CLI search uses the project's safe query syntax, not raw SQLite query text. Plain terms use normalized substring `contains` matching and are ANDed; uppercase `OR` creates alternatives. Quoted phrases stay intact and `-term`/`-"quoted phrase"` exclude. `word` mode applies ASCII letter/digit/underscore boundaries; CJK text conservatively remains normalized contains matching. Filters include `role:user`, `source:zip`, `path:current`, `path:all`, `scope:title`, and `scope:message`; raw `path:` and `scope:` modifiers override matching UI selectors. It prints conversation IDs, node IDs, and roles, not snippets.

Exclusions are conversation-level for conversation results: if any title or message in the selected search scope and path matches an excluded fragment, that conversation is not returned. `/api/search/messages` still returns only message hits that do not contain the excluded fragment. `path:current` follows the reader path per conversation; if a damaged archive has no current-path nodes at all, current-path search falls back to the same all-node view that the reader displays.

Date filters such as `after:2026-05-01`, `before:2026-05-13`, `--from`, and `--to` use UTC calendar days, not your local time zone's calendar day. Start dates are inclusive at `00:00:00Z`; end dates include the full UTC day by using the next day's `00:00:00Z` as an exclusive upper bound, so fractional timestamps like `23:59:59.5Z` are included. CLI export timestamps and deterministic filename dates use UTC; the browser displays timestamps in the browser's local time zone. The Web search box is limited to 500 characters; use advanced filters for longer structured queries. Browser Cmd/Ctrl+F sees only currently rendered virtual-list rows, so use archive search or Copy conversation for the whole conversation.

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web search uses optional normalized trigram indexes built by `web-index`. This is designed for practical substring search in the browser. If those optional indexes are missing or damaged, rebuild them:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

Search diagnostics are best-effort performance hints. They must report only normalized-safe candidate layers or scan fallbacks such as normalized trigram, normalized scan, normalized title scan, or full scan. Legacy raw FTS presence may be reported separately, but it must not be presented as the actual candidate backend because it can miss normalized-equivalent text.

If you manually run `VACUUM`, `VACUUM INTO`, or rewrite the SQLite database with an external compaction or backup tool, run `python chatgpt_archive.py web-index --db <archive.db>` again before relying on Web UI search. The optional Web index is rebuilt from the canonical conversation tables and is safe to regenerate.

## Verification and optional Web indexes

`verify` checks SQLite integrity and project-level consistency, including missing current nodes, broken parent links, empty conversations, and parent cycles.

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

`message_fts_rebuildable` is a runtime capability result, not a constant: missing FTS with available FTS5 reports `missing` and rebuildable true; an unavailable FTS5 module reports `capability_unavailable` and rebuildable false; a damaged table reports `damaged`, with rebuildability determined by the same bounded runtime probe. Other SQLite errors propagate through the structured database error classifier.

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

A runnable delivery should include the Python sources, tests, docs, `requirements-web.txt`, `constraints-web-py312.txt`, frontend source and tests under `webui/src` and `webui/tests`, frontend config/package files, and built assets under `webui/dist`. It should not include `webui/node_modules`, `webui/tsconfig.tsbuildinfo`, Python cache directories or bytecode, coverage/typecheck caches, `.DS_Store`, AppleDouble `._*` files, `__MACOSX`, `Thumbs.db`, `Desktop.ini`, `.gitignore.md`, temporary logs, local acceptance logs, `*.log`, `*.ndjson`, `*.jsonl`, `archive/`, `exports/`, any `*.zip`, `conversations*.json`, real databases such as `*.db`, `*.sqlite`, and `*.sqlite3`, or SQLite sidecars such as `*.db-journal`, `*.sqlite-wal`, `*.sqlite-shm`, `*.sqlite-journal`, `*.sqlite3-wal`, `*.sqlite3-shm`, and `*.sqlite3-journal`. Directory checks allow the target root's own `.git` directory so a normal Git clone can be checked, but nested `.git` directories are forbidden; ZIP delivery checks forbid any `.git` entry.

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

The main database stores conversations, mapping nodes, import runs, and warnings. Complete raw JSON is retained for message objects only; conversation and mapping-node objects are normalized rather than preserved byte-for-byte. Input ZIP SHA-256 is optional, while per-entry SHA columns in `source_files`/`file_index` are reserved and currently unset. The CLI FTS table is `message_fts`. Optional Web search helper tables include `web_message_norm`, `web_title_norm`, `web_message_trigram`, and `web_title_trigram` plus SQLite FTS5 shadow tables.

The canonical database schema is versioned with `PRAGMA user_version` (current version 3). Version 3 makes canonical TEXT identities explicitly `NOT NULL`; migration rebuilds compatible legacy tables in one locked transaction and refuses NULL identities without guessing. Read-only commands and Web requests never run migration DDL; an older compatible database is gated with `database_migration_required`. Before upgrading, create and verify an external backup, then run `python chatgpt_archive.py migrate --db archive/chatgpt_archive.db`.

Health and `verify` distinguish a missing optional `message_fts` from a damaged one. A damaged table reports `optional_message_fts_error` with a `--rebuild-fts` recovery hint; generic malformed, locked, readonly, I/O, and SQL runtime failures are not silently treated as a missing capability and use the stable codes `database_malformed`, `database_locked`, `database_readonly`, `database_io_error`, or `database_runtime_failure`.

## Known limits

- This is a local archive tool, not a cloud sync service.
- The Web UI is intended for local use. Do not expose it to an untrusted network without adding your own access controls.
- Export parsing follows the observed OpenAI / ChatGPT export format. If the upstream export shape changes, `inspect` and tests should be updated before trusting a new import path.
- Export file name parts are sanitized for Windows as well as Unix-like systems, including reserved device names such as `CON`, `AUX`, `COM1`, `LPT9`, `COM¹`, and `LPT²`, plus trailing dots and spaces.
- Very large archives can take time to import, rebuild FTS, and build Web trigram indexes. Prefer the `--rebuild-fts` path for large imports.

## Security and response contracts

Upload ingress accepts at most one `Origin`, `Content-Length`, and `Sec-Fetch-Site` header. Origin must be exactly one HTTP(S) origin without userinfo, path, query, fragment, controls, or comma chains. Content length must be a canonical nonnegative ASCII decimal integer; duplicate or malformed security headers are rejected before multipart parsing. Invalid or non-finite compression-ratio configuration falls back to the finite safe profile default.

Loopback Web access accepts only `localhost`, `127.0.0.1`, `::1`, the explicit loopback bind host, and explicitly configured hosts. Non-loopback binds additionally require `CHATGPT_ARCHIVE_ALLOWED_HOSTS` (or `--allowed-hosts`) with the actual browser hostname or LAN IP; `*` is rejected. `CHATGPT_ARCHIVE_TRUSTED_PROXIES` (or `--trusted-proxies`) uses a strict single-edge model: forwarded headers are ignored from untrusted peers, while a trusted direct edge must overwrite client values. Repeated Host/Forwarded headers, comma-separated proxy chains, malformed syntax, and conflicts between `Forwarded` and `X-Forwarded-Host/Proto` are rejected. Every request, including static UI and GET APIs, must have an allowed Host. Remote writes require a same-origin `Origin`; missing Origin is compatible only in the trusted loopback profile. Upload writes always reject `Sec-Fetch-Site: cross-site`.

Import failures use stable preflight, source-scan, source-read, JSON-decode, top-level-contract, and transaction stages. Stable codes include `upload_preflight_failed`, `input_source_open_failed`, `input_source_not_regular_file`, `source_read_failed`, `source_changed_during_read`, `invalid_conversation_encoding`, and `json_integer_too_large`. Rollback summaries separate `attempted_*` work from zero `committed_*` counts. Failed-run persistence uses a fresh connection and reports any secondary persistence failure. Cleanup diagnostics use a structured `cleanup_warnings` array; the deprecated `cleanup_warning` scalar is its first item. No temporary path is returned. A successful canonical commit is not described as unimported if later verify, stats, optional Web-index, or cleanup work fails.

Standalone JSON, directory members, and ZIP members use the same single-pass top-level-array framer and one import transaction. Each element is scanned once, decoded once, and capped at 128 Mi characters. Exactly one file-leading UTF-8 BOM is removed; U+FEFF inside a JSON string is preserved. Canonical IDs are limited to 512 characters and never truncated. Query-based `/api/by-id/*` endpoints are the primary Web addressing contract and accept bounded legacy IDs up to 16 Ki characters, so slash and URL punctuation remain unambiguous.

Non-standard JSON `NaN`/`Infinity`, including overflowing standard numbers such as `1e9999`, are rejected. Invalid timestamps become `NULL` with a content-free warning. Ordinary CLI/Web reads and default `/api/health` use a bounded schema gate and do not run `foreign_key_check`; `verify` and `/api/health?deep=true` stream the exact database-wide check with bounded samples and explicit freshness fields. Parent/effective-current counters retain their documented units.

Long display text uses a revision-bound opaque cursor over SQLite incremental BLOB reads; numeric offset remains a compatibility scan capped at 1,048,576 characters, after which the cursor is required. Raw preview uses one NUL-safe bounded BLOB query and reports byte size plus exact/incomplete metadata. Visible NUL and isolated-surrogate recovery is consistently U+FFFD while raw JSON stays safely escaped. Optional Web index format 3 enforces input, normalized, and derived byte budgets; oversized rows are recorded and canonical-verifier scanned so building the index cannot reduce recall. The atomic build holds `BEGIN IMMEDIATE` for the full build, so other writers may wait; there is no Web build/cancel job, only an internal/CLI cancellation callback.

CLI and Web conversation export reject before materialization above 100,000 nodes, 32 MiB for one node's canonical/raw input, or 128 MiB total conversation input; streamed output is capped at 256 MiB. CLI writes a same-directory temporary file while hashing and comparing the old file incrementally, then atomically replaces only changed output. Effective-current materialization uses the same 100,000-node limit plus a 128 MiB graph-ID input limit per conversation. Browser copy reads a stream and aborts before clipboard write above 16 MiB UTF-8 or 8 Mi characters, directing the user to Download; no partial text reaches the clipboard.

Legacy canonical text that is empty or a legacy placeholder may recover bounded readable text from a valid raw text message. The shared resolver is capped and is used consistently by the reader, message/conversation search, highlights, copy, CLI Markdown/TXT, and Web export. Default message APIs return one complete user-visible body, `display_text`; the duplicate `content_text`/`render_text` aliases are not returned. Invalid, oversized, or genuinely non-text raw payloads remain placeholders.

“Copy URL” always serializes explicit `match_mode`, `layout`, and `show_internal` values plus one already-applied search/list/selection context; text still waiting for debounce is not mixed with an older selected conversation. Explicit URL values win over `localStorage`; omitted values may use local settings. This release uses `replaceState` and intentionally does not restore step-by-step search or selection history with browser Back/Forward. Japanese and Spanish are visibly labelled as partial translations; English, Simplified Chinese, and Traditional Chinese have compile-time key parity.

Release ZIP members use fixed metadata and deterministic ordering, so identical payloads built with the same Python/ZIP implementation are byte-for-byte reproducible. The builder still verifies the authoritative required-file set, every payload SHA-256, the internal manifest, built assets, and atomic replacement.

Release ZIP publication validates an authoritative required-source list independent of collector output, writes a temporary file in the target directory, verifies a sorted size/SHA-256 manifest for every payload file, the exact member set and built assets, runs the delivery check, and only then atomically replaces the destination. Missing required source/config/docs hard-fail, and failure leaves the previous release unchanged.

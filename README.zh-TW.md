# ChatGPT Export Archiver

語言: English | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

ChatGPT Export Archiver 是一套本機優先、重視隱私的封存工具，用來把 OpenAI / ChatGPT 匯出的 ZIP 檔整理成可搜尋的 SQLite 資料庫。它不會把原始匯出內容送進瀏覽器，支援可重複的增量匯入，提供命令列匯出與搜尋工具，也包含本機 React Web UI，方便瀏覽並從頁面選取 ZIP 匯入。

## 專案功能

- 從 OpenAI / ChatGPT 匯出的 ZIP 或解壓縮後的匯出目錄匯入 `conversations.json`。
- 保留對話中繼資料、mapping nodes、訊息角色、文字內容、時間戳記、父節點關係與匯入警告。
- 支援增量匯入。把較新的匯出檔再次匯入同一個資料庫時，會更新已變更的對話，不會刻意重複寫入未變更的資料。
- 建立可選的 FTS5 訊息索引，供命令列搜尋使用。
- 建立可選的 Web 子字串搜尋索引，提升瀏覽器搜尋體驗。
- 支援匯出 Markdown、TXT，或同時匯出兩種格式。
- 提供 `verify`、`stats` 與不會列印聊天正文的隱私友善 `inspect` 指令。
- 提供本機 Web UI。即使資料庫尚未存在，也可以先啟動頁面，再從瀏覽器選取 ZIP 匯入。
- 日誌與結構化指令輸出分離，並避免記錄標題、snippet、raw JSON 或訊息正文。

## 隱私

所有處理都在本機完成。資料庫、匯出的檔案、暫存上傳副本、Web UI 與紀錄檔都留在你的電腦上，除非你自行移動或發布它們。命令列預設列印的是 ID、計數、時間戳記與狀態列，而不是訊息片段。CLI summary 與紀錄檔不會輸出聊天正文、標題、snippet、raw JSON、完整輸入/輸出路徑或真實 ZIP 檔名；匯入 summary 只回報輸入類型，例如 `source zip`。Web UI 供本機使用，預設繫結到 `127.0.0.1`。

在匯入 summary 中，`valid_conversations` 統計的是去重合併前已解析通過的輸入 conversation 元素。發生重複 id 合併時，它可能大於最後的 `inserted_conversations`、`updated_conversations` 或 `unchanged_conversations` 資料庫變更計數。

`inspect` 與 scanner 錯誤預設不會列印真實 ZIP 檔名或完整路徑。`verify`、`stats`、`search`、`export` 等需要既有資料庫的 CLI 命令在資料庫路徑寫錯時會回報 `database_not_found`，不會建立空的 SQLite 檔案。Web 搜尋在可用時會把可選的 trigram 索引作為候選召回層，之後仍套用正規化子字串過濾，因此短查詢、符號與不支援 trigram 的情況都會安全回退。

`--delete-input-on-success` 只會在主要匯入交易成功後執行。明確輸入是 symlink 時，它會刪除命令列指定的 symlink 本身，不會刪除該 symlink 指向的真實 ZIP 檔。

資料庫與匯出的 Markdown / TXT 仍可能包含私人聊天內容。請把 `archive/*.db`、匯出檔與原始 ChatGPT 匯出 ZIP 都視為敏感資料處理。

## 系統需求

- Python 3.10 或更新版本。
- SQLite 需要啟用 JSON1 與 FTS5。現行 macOS、Windows 與 Linux 上多數 Python 建置版本都已包含二者。
- 只有在需要重新建置 React Web UI 或執行前端檢查時，才需要 Node.js 與 npm。runnable 交付包已包含 `webui/dist`，一般本機使用 Web UI 不需要重新建置前端。
- 若要使用 Web ZIP 上傳功能，請安裝 `requirements-web.txt` 中的 Web 依賴套件。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

Windows PowerShell：

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

Windows cmd.exe：

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

## 快速開始

把 ChatGPT 匯出 ZIP 放在儲存庫外部，然後執行最快的安全匯入指令。這個指令會略過輸入雜湊，並在匯入結尾一次重建 FTS；對大型封存來說，比逐筆維護 FTS 快得多。

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows PowerShell 等價寫法：

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows cmd.exe 等價寫法：

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

啟動本機 Web UI：

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

如果還沒有資料庫，Web UI 仍可啟動，並會顯示空狀態與匯入面板。你可以在瀏覽器中選取 ChatGPT 匯出 ZIP；後端會寫入一個本機暫存副本，完成匯入後自動執行 `verify`、`stats` 與 `web-index`。

```bash
python chatgpt_archive.py web --port 8787
```

## 常用 CLI 流程

檢查匯出檔，但不列印聊天內容：

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

明確建立空資料庫：

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

使用大型封存路徑匯入：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

檢查結構一致性：

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

查看結構化計數與時間範圍：

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

透過 CLI 搜尋路徑搜尋訊息文字。輸出只包含 conversation ID、node ID 與角色，不包含 snippet：

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

將對話匯出為 Markdown、TXT，或在同一次執行中同時匯出兩種格式。`--format md` 會寫入 Markdown 正文檔並更新 manifest，`--format txt` 會寫入 plain text 正文檔並更新 manifest，`--format all` 會同時寫入兩種正文檔並更新 manifest：

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

依日期範圍匯出，並在需要時重寫既有檔案。`--from` 與 `--to` 的日期邊界只接受 `YYYY-MM-DD`：

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

匯出 summary 回報的是正文檔計數。`written` 統計最終位元組有變更的 Markdown/TXT 正文檔，`skipped_unchanged` 統計未變更的 Markdown/TXT 正文檔。manifest 會視需要更新，但不列入這兩個計數。

重建可選 Web 搜尋索引：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

啟動 Web UI：

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## 匯入模式

建議的大型封存指令是：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

如果願意讓 SQLite 在匯入後額外花時間整理 planner statistics 與 FTS 索引，可以使用：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` 預設關閉。只有在你已經有另一份 ZIP 備份時才建議使用。刪除動作只會在主匯入交易成功後執行。若刪除成功，CLI 會列印 `deleted_input True`，不列印路徑。若刪除失敗，匯入仍然算成功，run 保持 `finished`，寫入結構化 `delete_input_failed` warning，CLI 只列印 `delete_input_failed True` 與例外類型。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

增量匯入是正常使用方式。把較新的匯出再次匯入同一個資料庫時，會更新已變更的對話，並保留其餘封存資料。

## Web UI 流程

Web UI 是由 FastAPI 提供服務的本機 React 應用程式。建議路徑是直接使用 runnable tree 中已建置好的 `webui/dist`。

```bash
python chatgpt_archive.py web --port 8787
```

Web UI 有兩種使用方式。如果資料庫已存在，可以明確傳入資料庫路徑，也可以使用預設路徑。如果資料庫不存在，也可以先啟動 Web UI，再用匯入面板上傳 ChatGPT 匯出 ZIP。上傳匯入會依序執行，同一進程內一次只允許一個 SQLite writer。

Web 上傳匯入成功後，後端使用與 CLI 相同的核心 import pipeline，接著執行 `verify`、`stats` 與 `web-index`。上傳 ZIP 是伺服器端暫存副本，會獨立於你磁碟上的原始檔案進行清理。


## Web UI 驗收清單

修改 Web 路徑或準備 runnable 交付包時，可以用這份清單檢查：

- 在沒有資料庫的情況下啟動 Web UI，並確認頁面能提供空狀態契約。
- 從瀏覽器匯入一個小型 ChatGPT 匯出 ZIP，並確認 job 正常完成。
- 確認上傳匯入後，後端會執行 `verify`、`stats` 與 `web-index`。
- 重新整理頁面，確認對話可以列出並開啟。
- 再匯入一個較新的 ZIP，確認增量路徑仍可使用。

runnable 交付包中的 Web 路徑不應依賴 `webui/node_modules`，因為建置好的 React assets 已由 `webui/dist` 提供。

## 搜尋語法

CLI 搜尋使用專案統一的安全查詢語法，不直接使用 SQLite 查詢文字。可以使用一般關鍵字、加引號的片語、`-term` 排除、`OR`，以及 `role:user`、`source:zip`、`path:current`、`path:all`、`scope:title`、`scope:message` 等篩選條件。輸出只包含 conversation ID、node ID 與角色，不包含 snippet。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web 搜尋使用 `web-index` 建立的可選 normalized trigram 索引，適合瀏覽器中的實用子字串搜尋。如果這些可選索引缺失或損壞，請重建：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## 驗證與可選 Web 索引

`verify` 會檢查 SQLite integrity 與專案層級一致性，包括缺失的 current node、斷裂的父節點連結、空對話與父節點迴圈。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

如果 `PRAGMA integrity_check` 報告 `web_message_trigram` 或 `web_title_trigram` 的 FTS5 inverted index 損壞，核心對話資料仍可能結構正常，只是可選 Web 搜尋索引損壞。此時 `verify` 會報告 `optional_web_index_error true` 並列印恢復提示。用下面的指令重建可選 Web 索引：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

這個診斷是保守的。只有當所有 integrity-check 錯誤都能歸因到這些可選 Web 索引表或其 FTS5 shadow tables 時，才會標記為可選 Web 索引問題。

## 日誌

日誌等級為 `debug`、`info`、`warning`、`error` 與 `none`。預設等級是 `warning`。越詳細的等級會包含其後較安靜等級的內容。日誌不會包含標題、snippet、raw JSON 或訊息正文。

日誌參數可以寫在子命令之前，也可以寫在子命令之後：

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

請把 JSON logs 放在 `logs/` 這類已忽略位置。`*.jsonl` 是本機紀錄檔產物，delivery clean 會拒絕它們。

匯入計時欄位包括 `source_scan_seconds`、`parse_and_upsert_seconds`、`fts_rebuild_seconds`、`finalize_commit_seconds`、`close_seconds`、`legacy_pre_commit_seconds`、`wall_total_seconds` 與 `total_import_seconds`。`total_import_seconds` 是端到端 wall time，包含最終 commit 與 close。

匯入交易成功完成後，後續 summary update 都是 best-effort。`summary_update_after_commit_failed`、`import_connection_close_failed` 與 `summary_update_after_close_failed` 是警告，不會把已成功的匯入標記為失敗。

## 開發與驗收檢查

執行 Python 檢查，並在第一次 delivery clean 前清理安全的產生物：

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

建置並 smoke-test Web UI：

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

`clean_generated_artifacts.py` 是跨平台工具，並會保留 `webui/dist`。它只會刪除可安全再產生的檔案，不會刪除資料庫、ZIP、SQLite sidecar、`archive/`、`exports/` 或 `logs/`；如果 delivery clean 仍回報這些敏感路徑，請把它們移出專案根目錄或手動刪除。驗收命令使用 `--fail-on-blocked`，因此敏感殘留會立即中止交付流程。

Windows PowerShell 或 cmd 使用者在 search query 包含空格時請使用雙引號，例如 `"python sqlite"` 或 `"role:user path:current python -pandas"`。上面的 Python、Web、Web index、typecheck、build、cleanup 與 delivery-check 命令在 Python 與 Node 位於 `PATH` 時可用於 macOS、Windows 與 Linux。如果 Windows 使用 Python launcher，可用 `py -3 tools/clean_generated_artifacts.py --fail-on-blocked` 執行清理工具。

檢查 ZIP 交付包：

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## 交付說明

runnable delivery 應包含 Python 原始碼、測試、文件、`requirements-web.txt` 與 `webui/dist`。不應包含 `webui/node_modules`、`webui/tsconfig.tsbuildinfo`、Python 快取目錄或 bytecode、coverage/typecheck 快取、`.DS_Store`、`__MACOSX`、`Thumbs.db`、`Desktop.ini`、`.gitignore.md`、暫存紀錄檔、本機驗收紀錄檔、`*.log`、`*.ndjson`、`*.jsonl`、`archive/`、`exports/`、任何 `*.zip`、`conversations*.json`、`*.db`、`*.sqlite`、`*.sqlite3` 等真實資料庫檔，或 `*.db-journal`、`*.sqlite-wal`、`*.sqlite-shm`、`*.sqlite-journal`、`*.sqlite3-wal`、`*.sqlite3-shm`、`*.sqlite3-journal` 等 SQLite sidecar。目錄檢查允許目標根目錄自己的 `.git`，因此一般 Git clone 可以直接檢查；巢狀 `.git` 會失敗，ZIP delivery 中任何 `.git` 都會失敗。

source-only delivery 可以省略 `webui/dist`，但之後需要先重新建置前端，才能提供完整 React UI。

## 原始碼樹說明

```text
chatgpt_archive.py                 CLI 進入點
chatgpt_export_archiver/cli.py     CLI 指令與可重用 import pipeline
chatgpt_export_archiver/db.py      SQLite schema、匯入 helper、verify、stats、FTS helper
chatgpt_export_archiver/web_app.py FastAPI app factory 與靜態 UI 服務
chatgpt_export_archiver/web_api.py Web API routes
chatgpt_export_archiver/web_db.py  Web 查詢 helper 與可選 trigram index builder
chatgpt_export_archiver/web_jobs.py Web ZIP 匯入 job manager
webui/                             React 前端原始碼與建置後的 dist 檔案
tests/                             Python 單元測試與整合測試
tools/                             交付檢查與輔助腳本
```

## 資料庫概覽

主資料庫保存 conversations、mapping nodes、import runs 與 warnings。CLI FTS 表是 `message_fts`。可選 Web 搜尋輔助表包括 `web_message_norm`、`web_title_norm`、`web_message_trigram`、`web_title_trigram`，以及 SQLite FTS5 shadow tables。

除非已明確規劃並記錄 migration，否則專案不會在小型穩健性修復中修改資料庫 schema。

## 已知限制

- 這是本機封存工具，不是雲端同步服務。
- Web UI 供本機使用。不要在沒有額外存取控制的情況下暴露到不可信網路。
- 匯出解析遵循目前觀察到的 OpenAI / ChatGPT 匯出格式。如果上游匯出結構變更，應先更新 `inspect` 與測試，再信任新的匯入路徑。
- 超大型封存在匯入、重建 FTS 與建立 Web trigram 索引時都可能需要時間。大型匯入優先使用 `--rebuild-fts` 路徑。

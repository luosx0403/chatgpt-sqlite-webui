# ChatGPT Export Archiver

語言: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

把 ChatGPT 官方匯出的 ZIP 變成私有、可搜尋的 SQLite 知識封存。

`本機優先` · `SQLite` · `隱私優先` · `快速匯入` · `快速 Web 索引` · `聊天式 Web UI` · `Markdown/TXT 匯出`

ChatGPT Export Archiver 可以直接匯入 OpenAI / ChatGPT 官方 export ZIP，寫入 SQLite，驗證封存，建立搜尋索引，開啟本機 Web UI，並把對話匯出為 Markdown 或 TXT。它適合長期個人封存、離線檢索、知識庫遷移，以及官方歷史紀錄頁面沒有提供的本機檔案與索引工作流程。

## 為什麼值得用

- **本機優先，保護隱私。** ZIP、資料庫、匯出檔、暫存上傳副本、Web UI 與紀錄檔都留在你的電腦上，除非你自行移動。
- **直接匯入 ZIP。** 可以直接讀取 ChatGPT 官方匯出 ZIP，不需要手動解壓縮或合併 shard。
- **適合大型封存。** 建議的匯入路徑支援大型 ZIP、增量匯入、延後 FTS 重建，以及經過最佳化的可選 Web 搜尋索引。
- **聊天式閱讀器。** Web UI 預設改為類似 ChatGPT 網頁的版面：user 在右側，assistant 在左側，system/internal 預設收合但可展開。
- **保留經典技術視圖。** 可以在設定中切換，或透過 `?layout=classic` / `?messageLayout=classic` 回到舊的逐列版面。
- **更適合封存的搜尋。** 在長期封存和本機檢索情境下，本機 SQLite 搜尋比官方歷史頁面更可控：支援 role/title/source/scope/exclude、片語、OR、分頁、verify、可重建索引和匯出。
- **可遷移匯出。** Markdown 和 TXT 匯出是確定性的，適合備份、本機知識庫、離線 grep 和遷移。

## 截圖

安全截圖待補。截圖應使用 synthetic 假資料，不能包含真實聊天標題、snippet、raw JSON、Email 或本機路徑。

## 本機 Smoke 觀察

以下只是某台本機機器上的範例觀察，不是通用效能承諾：

- 約 2.25 GB 的真實匯出 ZIP 使用大型封存路徑匯入約 98 秒。
- 該封存的 `verify` 約 4 秒完成。
- 較大增量封存後的可選 Web 索引重建約 106 秒完成。
- 本機 Uvicorn Web 應用程式上的高命中訊息搜尋約 0.3 秒返回。

## 專案功能

- 從 OpenAI / ChatGPT 匯出的 ZIP、單一 `conversations.json` 檔案或解壓縮後的匯出目錄匯入 `conversations.json` 和 sharded `conversations-*.json`。
- 保留對話中繼資料、mapping nodes、訊息角色、文字內容、時間戳記、父節點關係、來源追蹤與匯入警告。
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

- Python 3.10 或更新版本；Python 3.12 是 Web 依賴可重現安裝的測試目標。
- SQLite 需要 JSON 支援。FTS5 是可選能力；不可用或缺少 `message_fts` 時仍使用安全掃描搜尋。
- 只有在需要重新建置 React Web UI 或執行前端檢查時，才需要 Node.js 與 npm。runnable 交付包已包含 `webui/dist`，一般本機使用 Web UI 不需要重新建置前端。
- 核心 CLI 只使用 Python 標準函式庫，沒有 Web 套件也能執行。若要啟動 Web UI（包括 ZIP 上傳），請安裝 `requirements-web.txt`；缺少此 profile 時，`web` 指令會立即失敗並顯示安裝提示。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

Windows PowerShell：

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

Windows cmd.exe：

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

Python 3.12 constraints 檔案會固定所有已解析 Web 相依套件的版本，但它不是跨平台 hash lock。後續發布流程應為支援的 Python/OS 矩陣產生並驗證各平台 hash；在此之前，只應從可信套件索引安裝，並把 npm lockfile 與 audit 檢查作為獨立的前端控制。

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

`--input` 可以指向官方匯出 ZIP、單一 `conversations.json`，或解壓縮後的匯出目錄。解壓縮目錄可以包含 `conversations.json`，也可以包含分片形式的 `conversations-*.json`；不要手動合併分片。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
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

CLI 與 Web 匯出預設使用有效目前路徑，且只包含可見訊息。需要包含分支或內部訊息時，請明確使用 `--path all` 和/或 `--include-internal`；manifest 會記錄這兩個選擇。CLI 匯出會以有界批次讀取對話節點；Web 下載與`複製目前路徑整段對話`使用專用的有界伺服器端文字串流。因此，完整標準文字及符合條件的 legacy/raw 復原文字不會受 reader 回應預算截斷。

重建可選 Web 搜尋索引：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

`web-index` 會依明確階段掃描並正規化訊息與標題，並在支援時建立 trigram 索引。每次建置使用不可預測的獨立 staging 名稱與持久 owner-token lease；第二個建置會以 `web_index_build_in_progress` 拒絕，逾期清理也必須驗證精確所有權。所有階段皆使用有界 keyset，以及各自的 input、normalized、derived 與 FTS-bind 位元組預算並回報實測峰值。批次間會釋放 writer lock；最後以短暫的 `BEGIN IMMEDIATE` transaction 複核 canonical generation、物件所有權與 metadata 後原子發布。提交前 reader 始終看到舊 index；generation 變動、SQLite 中斷、磁碟錯誤或取消會保留舊 index，且只清理該 lease 擁有的物件。`POST /api/import/jobs/{job_id}/web-index/cancel` 只適用於匯入工作的 index 階段。超出預算的資料列會記錄並對 canonical text 精確驗證，建置 index 不會降低召回率。

啟動 Web UI：

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## 匯入模式

建議的大型封存指令是：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

輸入可以是 ZIP、單一 `conversations.json`，或包含 `conversations.json` 或分片 `conversations-*.json` 檔案的解壓縮目錄。scanner discovery 會忽略 `__MACOSX`、AppleDouble `._*` 檔案和 `.DS_Store` 等 macOS metadata paths，因此這些本機 artifact 不會成為 conversation source。

對於目錄輸入，POSIX 平台會在可用時逐 component 使用 `dir_fd` 與 `O_NOFOLLOW`。可攜 fallback 會拒絕 symlink/reparse component，並在依路徑開啟前立即驗證 containment，但 Python 標準函式庫無法在所有平台消除所有本機 replacement race。不要匯入不受信任的本機使用者或並行程序可修改的解壓縮目錄；此威脅模型應使用原始唯讀 ZIP。

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

預設閱讀版面是 `chat`：user 訊息靠右，assistant 訊息靠左，system/internal 訊息以收合提示顯示。若要使用舊的逐列技術版面，可以在設定中選擇「經典逐列」，或在 Web UI URL 後加上 `?layout=classic` 或 `?messageLayout=classic`。

所有入口共用同一套 `path=current` effective-current 規則：有效且屬於該對話的 `current_node` 與其父鏈優先，即使所有 raw flag 都為 0；否則選擇確定且可用的 `is_on_current_path=1` 葉鏈；兩者皆不存在時，該對話才 fallback 到 all。回應保留 raw flag 原義，並提供 `current_node_exists`、`current_collection_source`、`current_path_fallback_to_all`、`effective_path` 與逐節點 effective visibility。斷裂父鏈和 cycle 會有限且確定地診斷，不會讓遞迴查詢掛起。

全域 current-path 搜尋會先透過正規化內文/標題索引及安全的 source/date/role 條件取得不依賴路徑的對話候選，再只為這些候選建立 effective-current membership；只有無法縮小的僅排除查詢才明確回退到全資料庫。Reader 命中導覽初始只取一個精簡頁面，接近已載入邊界時才繼續追加。搜尋與 Web 索引 SQL 使用可攜式的防扁平化查詢結構，不要求 SQLite 支援 `AS MATERIALIZED`，並保證每個 legacy raw 候選在每個邏輯階段最多解析一次。

閱讀器複製與匯出動作遵守可見閱讀器契約。`複製目前路徑整段對話` 使用專用的完整文字串流處理目前 reader 路徑，遵守「顯示內部訊息」開關並忽略目前搜尋篩選，不會在瀏覽器中累積 reader 分頁。`複製目前可見` 只複製已載入的可見訊息。下載連結使用同樣的目前路徑與「顯示內部訊息」設定。Raw 訊息存取只透過單則訊息 endpoint 提供有上限的較大 raw 預覽；截斷回應必須把 `raw_text` 當作純文字預覽渲染，UI 只顯示這個 capped preview。

reader 使用 `around_node_id` 跳轉到命中時，會使用與 reader 相同的分頁集合：Show internal 關閉時使用 visible-only rows，Show internal 開啟時使用完整 node collection；對沒有 current-path node 的損壞 conversation，使用 effective all-node collection。

Web UI 有兩種使用方式。如果資料庫已存在，可以明確傳入資料庫路徑，也可以使用預設路徑。如果資料庫不存在，也可以先啟動 Web UI，再用匯入面板上傳 ChatGPT 匯出 ZIP。上傳匯入會依序執行，同一進程內一次只允許一個 SQLite writer。

Web 上傳匯入成功後，後端使用與 CLI 相同的核心 import pipeline，接著執行 `verify`、`stats` 與 `web-index`。上傳 ZIP 是伺服器端暫存副本，會獨立於你磁碟上的原始檔案進行清理。

預檢失敗與終態匯入工作都可能回傳多個 `cleanup_warnings`。React UI 會把每個安全 warning code 與 `path_kind` 顯示為本地化使用者文案，同時相容已棄用的單值 `cleanup_warning`。這些提示不會顯示暫存路徑、檔名、OS message 或 error class；重複輪詢會取代同一工作快照，不會追加重複提示。

若無法提供預先建置的 React 應用，伺服器 fallback HTML 只是功能受限的緊急介面，不能替代完整 reader。它的搜尋與閱讀控制較少，下載預設排除 internal node；完整 UI 需重新建置 `webui/dist`。

## Web 上傳安全限制

Web 上傳在匯入 job 啟動前執行應用層安全限制。這些限制由環境變數控制，與 CLI `import` 無關（CLI 不使用這些限制）。

Web 上傳會在讀取檔案前先保留 pending slot，因此大型上傳不能與另一個 writer 競爭。從保留 slot 之後發生的任何錯誤，包括暫存上傳路徑建立失敗，都必須釋放 slot 並清理伺服器端暫存目錄；成功啟動的 import job 會接管 slot 與暫存副本。

程序被 kill、OOM 或主機崩潰可能繞過正常清理，在作業系統暫存目錄留下舊的 `chatgpt-archive-upload-*` 目錄。本版本不會自動刪除它們，因為 ownership/age 判斷錯誤可能刪除無關資料。停止伺服器後，管理員只能在確認精確前綴、目前帳號 ownership、目錄年齡、沒有 symlink/reparse point 且沒有工作使用後，逐一刪除舊目錄；絕不能刪除使用者匯出 ZIP，也不要使用未經檢查的 wildcard。

當 Web UI 繫結到 loopback 位址（`127.0.0.1`、`localhost`、`::1`）時，預設允許大型可信封存：

| 環境變數 | 本機預設值 | 控制內容 |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | 壓縮 ZIP 上傳總大小 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | 單個 JSON member 最大未壓縮大小 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | conversation JSON member 最大數量 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | 未壓縮 JSON 資料總量上限 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | 大型 JSON member 最大壓縮比 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | ZIP 內總 member 數上限 |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | 未設定 | 只在可信非 loopback 網路上設為 `local`，讓未設定的上傳限制使用本機預設值 |

**遠端繫結策略。** 非 loopback 需透過 `CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS=true`、`CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true` 或 `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local` 明確 opt-in。remote-safe 預設為 128 MiB 壓縮 ZIP、每個 JSON member 256 MiB、總未壓縮 512 MiB、200.0 壓縮比、200 JSON members、10,000 ZIP members；可信 loopback/local 預設壓縮比是 1000.0。`ALLOW_REMOTE_UPLOADS` 只放寬明確設定的限制，未設定項仍 remote-safe；`REMOTE_UPLOAD_PROFILE=local` 會把所有未設定限制恢復為本機大型預設值，只適合可信 LAN。

`/api/schema` 會回報有效上傳策略，包括 multipart body 上限（ZIP byte 上限加有限 overhead）與 remote profile。writer slot 和 receive-level body cap 在 multipart 解析前生效。parser 可能寫 spool，之後 pipeline 仍擁有伺服器暫存 ZIP，因此暫存磁碟應預留接近兩份壓縮副本再加資料庫成長。ZIP 檢查先執行，但 JSON 解碼、SQLite 寫入與 `web-index` 仍依解碼資料消耗記憶體、磁碟與 CPU。遠端上傳必須提供有效 `Content-Length`；loopback chunked 上傳仍受串流上限限制。

如需為合法的超大型封存提高本機限制，在啟動 Web UI 前設定相應變數：

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

在可信內部網路只提高明確的壓縮 ZIP 上限，同時讓其他未設定限制保持 remote-safe：

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

在可信內部網路使用完整本機上傳 profile：

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

僅對你信任的本機檔案設定更高限制。更大的值會提高 ZIP bomb、磁碟壓力和 CPU/記憶體風險。


## Web UI 驗收清單

修改 Web 路徑或準備 runnable 交付包時，可以用這份清單檢查：

- 在沒有資料庫的情況下啟動 Web UI，並確認頁面能提供空狀態契約。
- 從瀏覽器匯入一個小型 ChatGPT 匯出 ZIP，並確認 job 正常完成。
- 確認上傳匯入後，後端會執行 `verify`、`stats` 與 `web-index`。
- 重新整理頁面，確認對話可以列出並開啟。
- 再匯入一個較新的 ZIP，確認增量路徑仍可使用。

runnable 交付包中的 Web 路徑不應依賴 `webui/node_modules`，因為建置好的 React assets 已由 `webui/dist` 提供。

## 搜尋語法

CLI 搜尋使用安全查詢語法，不直接使用 SQLite 查詢文字。一般詞使用 normalized substring `contains` 並預設 AND；大寫 `OR` 建立替代分支。引號保留片語，`-term`/`-"quoted phrase"` 表示排除。`word` 僅對 ASCII 字母、數字與底線套用邊界；CJK 無斷詞時保守維持 normalized contains。查詢中的 raw `path:`/`scope:` 會覆寫 UI 選擇器。

排除詞對對話結果採用 conversation-level 語意：只要所選搜尋 scope 和 path 內任一標題或訊息命中排除片段，該 conversation 就不會返回。`/api/search/messages` 仍只返回本身不包含排除片段的訊息命中。`path:current` 會依每個 conversation 遵守 reader 路徑；如果損壞封存完全沒有 current-path node，current-path 搜尋會 fallback 到 reader 顯示的同一個 all-node 視圖。

日期篩選使用 UTC 日曆日；起始包含 `00:00:00Z`，結束以隔天 `00:00:00Z` 為排他上界。CLI 匯出時間與確定性檔名日期使用 UTC；瀏覽器依瀏覽器本機時區顯示。Web 搜尋框最多 500 個字元。虛擬清單只渲染部分列，因此瀏覽器 Cmd/Ctrl+F 看不到未渲染訊息；整段搜尋請用封存搜尋或複製對話。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web 搜尋使用 `web-index` 建立的可選 normalized trigram 索引，適合瀏覽器中的實用子字串搜尋。如果這些可選索引缺失或損壞，請重建：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

搜尋 diagnostics 是 best-effort 效能提示。它們只能回報 normalized-safe 候選層或掃描 fallback，例如 normalized trigram、normalized scan、normalized title scan 或 full scan。可以單獨回報 legacy raw FTS 是否存在，但不能把它宣稱為實際 candidate backend，因為它可能漏掉正規化等價文字。

如果你手動執行 `VACUUM`、`VACUUM INTO`，或使用外部工具重寫/壓縮 SQLite 資料庫，請在繼續依賴 Web UI 搜尋前重新執行 `python chatgpt_archive.py web-index --db <archive.db>`。Web UI 的可選索引可以從主資料表安全重建，不會改變原始對話資料。

## 驗證與可選 Web 索引

`verify` 會檢查 SQLite integrity 與專案層級一致性，包括缺失的 current node、斷裂的父節點連結、空對話與父節點迴圈。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

`message_fts_rebuildable` 來自真實 runtime capability，而不是常數：FTS 缺失但 FTS5 可用時回報 `missing` 且可重建；FTS5 module 不可用時回報 `capability_unavailable` 且不可重建；資料表損壞時回報 `damaged`，能否重建仍由同一有界 runtime probe 決定。其他 SQLite 錯誤會交給結構化資料庫錯誤分類器。

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

runnable delivery 應包含 Python 原始碼、測試、文件、`requirements-web.txt`、`constraints-web-py312.txt`、`webui/src` 與 `webui/tests` 下的前端原始碼與測試、前端設定/package 檔，以及 `webui/dist` 下的建置產物。不應包含 `webui/node_modules`、`webui/tsconfig.tsbuildinfo`、Python 快取目錄或 bytecode、coverage/typecheck 快取、`.DS_Store`、AppleDouble `._*` 檔、`__MACOSX`、`Thumbs.db`、`Desktop.ini`、`.gitignore.md`、暫存紀錄檔、本機驗收紀錄檔、`*.log`、`*.ndjson`、`*.jsonl`、`archive/`、`exports/`、任何 `*.zip`、`conversations*.json`、`*.db`、`*.sqlite`、`*.sqlite3` 等真實資料庫檔，或 `*.db-journal`、`*.sqlite-wal`、`*.sqlite-shm`、`*.sqlite-journal`、`*.sqlite3-wal`、`*.sqlite3-shm`、`*.sqlite3-journal` 等 SQLite sidecar。目錄檢查允許目標根目錄自己的 `.git`，因此一般 Git clone 可以直接檢查；巢狀 `.git` 會失敗，ZIP delivery 中任何 `.git` 都會失敗。

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

主資料庫保存 conversations、mapping nodes、import runs 與 warnings。message object 的 raw JSON 欄位按完整物件保留；conversation 與 mapping-node object 會正規化，不做逐位元組保存。輸入 ZIP SHA-256 可選，`source_files`/`file_index` 的逐 entry SHA 欄位目前保留但不填入。CLI FTS 表是 `message_fts`。可選 Web 搜尋輔助表包括 `web_message_norm`、`web_title_norm`、`web_message_trigram`、`web_title_trigram`，以及 SQLite FTS5 shadow tables。

canonical 資料庫以 `PRAGMA user_version` 版本化（目前版本 4）。版本 3 為 canonical TEXT identity 加上 `NOT NULL`；版本 4 新增依欄位劃分的持久 address/graph revision，使 fresh reader 不會重用過期的 compatibility 或 effective-current 判斷。Migration 在同一 write-lock transaction 安裝 revision row 與 managed trigger，並視需要使 optional index 失效。唯讀路徑不執行 migration DDL；舊相容資料庫回傳 `database_migration_required`。升級前先建立並驗證外部備份。

Health 與 `verify` 會區分可選 `message_fts` 缺失與損壞。損壞時回報 `optional_message_fts_error` 和 `--rebuild-fts` 復原提示；一般 malformed、locked、readonly、I/O 與 SQL 執行期錯誤不會被當成能力缺失，並使用 `database_malformed`、`database_locked`、`database_readonly`、`database_io_error` 或 `database_runtime_failure`。

## Round 9 資源、所有權與復原契約

managed FTS、可選 Web 索引、staging、metadata、generation 與 shadow 物件只有在精確核對類型、目標 table、SQL 與 fingerprint 所有權後才能執行破壞性 DDL。名稱衝突分別以 `core_fts_name_collision`、`optional_index_name_collision` 或 `staging_name_collision` 拒絕，絕不因名稱相似就刪除使用者物件。可選 Web 索引格式為 4：每次建置使用不可預測的獨立 staging 名稱與持久 owner-token lease；並行建置回傳 `web_index_build_in_progress`，逾期復原也必須核對 owner、資料庫身分、schema、generation、format 與全部物件名。輸入、正規化、派生及 FTS bind 分別按批預算，並回報實際 current/peak 位元組；完整串流 placeholder 分類不會被超過 256 字元的前綴繞過。

長正文 cursor 會綁定保存在 `archive_generations` 內部 `display:<rowid>` key 的持久逐列 revision。受管 insert/update trigger 會為每次影響顯示文字的寫入遞增 revision，即使直接外部 SQLite writer 未更新 `content_hash`；無關資料列不會使 cursor 失效。缺少這些 trigger 的既有 version-4 資料庫會進入 migration-required gate，由明確 writer migration 回填 revision。刪除資料列的 revision tombstone 會保留，避免 rowid 重用讓舊 cursor 復活。

精確訊息搜尋以 64 KiB 重疊分塊增量讀取 canonical BLOB。每列通常限制 32 MiB 解碼字元與 32 MiB UTF-8，可信本機可明確提高到 100 MiB 字元；raw-only fallback 另限 1 MiB/800,000 字元，單次請求另有 128 MiB 位元組/字元總預算。超限回傳完整 HTTP 413，本版本不回傳 partial page，也不承諾 continuation token。第一個 hit 請求取得精確總數，後續頁可分別顯示已載入數；晚位置命中攜帶綁定列 revision 的字元 anchor，reader 直接 seek，不重播數 MB 頁面。

單一新匯入對話元素獨立限制為 32 MiB UTF-8、32 MiB 解碼字元、250,000 scalar 與 5,000 mapping node；超出 node 限制以 `conversation_node_limit_exceeded` 略過且不保存內容。reader/effective-current/export 的 100,000-node 上限只為相容 legacy 或外部寫入資料庫，不代表允許匯入 100,000 node。多個 ZIP shard 共用一個讀取 session；目錄探索採增量預算。空 `parent` 視為 legacy root/missing-parent 相容。legacy ID readiness 檢查全部位址/圖欄位長度與不安全 Unicode，並以持久欄位 revision 使快取失效，一般讀取不輪詢 `PRAGMA data_version`。

專案批次匯入在同一寫入鎖 transaction 內暫時替換精確的專案自有 generation trigger，每個 dirty 欄位 domain 只推進一次，再復原並驗證 trigger；rollback 或 crash 會復原原 DDL/資料，外部 writer 仍使用一般逐 statement trigger。有限 effective-current scope 透過有界 SQLite TEMP 批次精確比較。全庫匯出把 plan 與 node spool 到暫存 SQLite，並以 keyset 串流讀取，不在 Python 保存全 archive node graph。

使用 `--delete-input-on-success` 時，canonical commit 成功前使用者原路徑始終存在。commit 後先持久寫入並 fsync 綁定身分的復原 journal，再 rename；中斷會留下 token，可用 `python chatgpt_archive.py recover-delete-input --directory <目錄> --token <token>` 明確復原，且絕不覆蓋替換檔案。Windows 或缺少 descriptor-relative no-follow 身分能力的平台會拒絕安全刪除。Web Python constraints 只固定 resolved version，仍不是跨平台 hash lock；請使用可信套件索引。

## 已知限制

- 這是本機封存工具，不是雲端同步服務。
- Web UI 供本機使用。不要在沒有額外存取控制的情況下暴露到不可信網路。
- 匯出解析遵循目前觀察到的 OpenAI / ChatGPT 匯出格式。如果上游匯出結構變更，應先更新 `inspect` 與測試，再信任新的匯入路徑。
- 匯出檔名片段會同時依 Windows 和類 Unix 系統清理，包括 `CON`、`AUX`、`COM1`、`LPT9`、`COM¹`、`LPT²` 等保留裝置名稱，以及尾隨點和空格。
- 超大型封存在匯入、重建 FTS 與建立 Web trigram 索引時都可能需要時間。大型匯入優先使用 `--rebuild-fts` 路徑。

## 安全與回應契約

上傳入口對 `Origin`、`Content-Length` 與 `Sec-Fetch-Site` 各只接受一個值。Origin 必須是沒有使用者資訊、路徑、查詢、片段、控制字元或逗號鏈的單一 HTTP(S) origin；Content-Length 必須是規範的非負 ASCII 十進位整數。重複或格式錯誤的安全標頭會在 multipart 解析前拒絕；無效或非有限的壓縮比設定會回退到有限的安全 profile 預設值。

Loopback Web 只接受 `localhost`、`127.0.0.1`、`::1`、明確的 loopback bind host 與明確設定的 host。非 loopback bind 還必須透過 `CHATGPT_ARCHIVE_ALLOWED_HOSTS`（或 `--allowed-hosts`）指定實際瀏覽器 hostname/LAN IP，禁止 `*`。`CHATGPT_ARCHIVE_TRUSTED_PROXIES`（或 `--trusted-proxies`）採嚴格單 edge 模型：未受信直連的 forwarded header 會被忽略，受信直連 proxy 必須覆寫 client 值；重複 Host/Forwarded、逗號 proxy chain、非法語法及 `Forwarded` 與 `X-Forwarded-Host/Proto` 衝突會被拒絕。靜態 UI、GET API 與全部請求都驗證 Host。遠端寫入必須有同源 `Origin`；只有可信 loopback profile 相容無 Origin 用戶端。上傳永遠拒絕 `Sec-Fetch-Site: cross-site`。

匯入失敗使用穩定的 preflight、source scan、source read、JSON decode、top-level 與 transaction 階段。code 包括 `upload_preflight_failed`、`input_source_open_failed`、`input_source_not_regular_file`、`source_read_failed`、`source_changed_during_read`、`invalid_conversation_encoding`、`json_integer_too_large`。清理使用結構化 `cleanup_warnings`；舊 `cleanup_warning` 只代表第一項。

獨立 JSON、目錄成員與 ZIP 成員使用同一個單遍、逐頂層陣列元素的解碼器，並位於同一匯入交易；每個元素只掃描和解碼一次，UTF-8 輸入與解碼後字元數各限制為 32 MiB，巢狀最多 256 層、scalar 最多 250,000 個。legacy raw 使用迭代 sanitizer，遍歷最多 100,000 個 node、raw preview 最多 80,000 bytes、完整 sanitized API payload 最多 4 MiB。ZIP 中央目錄全部 entry 與目錄全部 entry 都計入 100,000 member 上限。只移除檔案開頭的一個 UTF-8 BOM；JSON 字串內的 U+FEFF 會保留，重複開頭 BOM、字串外的中間 BOM、UTF-16/32、混合編碼與無效 UTF-8 都會拒絕。新 canonical ID 上限為 512 字元且不截斷；主要 query-based `/api/by-id/*` 最多接受 16 Ki 字元 legacy ID，更長舊 ID 會使 readiness 回報 `database_data_incompatible`。ZIP source-read 會區分加密、缺失、讀取期間變更、CRC 失敗及其他讀取失敗。

檔案身分透過 descriptor-bound stat/hash/read 驗證；`--delete-input-on-success` 使用原子 staging rename 與最終身分屏障，無法復原的佔名競態會產生 `delete_input_recovery_required`。Migration 僅接受定義完全相符的已知 predecessor；任何使用錯誤型別、目標或定義佔用 managed trigger/index 名稱的物件，都會在 DDL 前以 `database_managed_object_name_collision` 拒絕。

非標準 JSON `NaN` / `Infinity`（包括 `1e9999` 這類溢出的標準數值）會被拒絕；無效時間寫成 `NULL` 並記錄不含內容的 warning。預設 message API 只回傳一份受 reader 預算限制的 `display_text`，並以 truncation/total-exactness metadata 表示能否完整復原，不複製 `content_text`/`render_text`。普通 CLI/Web 讀取和預設 `/api/health` 使用有界 schema gate，不執行 `foreign_key_check`；`verify` 與 `/api/health?deep=true` 執行完整精確檢查並提供 freshness 欄位。每個多語句 CLI/Web 邏輯讀取都在 schema/capability probe 前建立一個 SQLite read snapshot，串流回應正常結束或失敗時都會釋放。Effective-current、分頁與 around-node 語義維持不變。

「複製 URL」永遠明確寫入 `match_mode`、`layout`、`show_internal` 與可分享的搜尋/reader 狀態。URL 明確值優先於 `localStorage`，缺少值才可回退本機設定。本版本使用 `replaceState`，瀏覽器上一頁/下一頁不會還原逐步搜尋或選擇歷程。

Release ZIP 使用固定 member metadata 產生可重現 bytes，並先寫入目標目錄暫存檔，核對每個 payload 的 size/SHA-256 manifest、精確 member 集合與 dist asset，再原子替換；任何失敗都保留舊 release。

rollback summary 以 `attempted_*` 與歸零的 `committed_*` 區分已嘗試和已提交工作；失敗 run 使用新連線持久化並明確報告次要持久化失敗。pre-job 暫存清理失敗保留主要 HTTP 錯誤，另回傳安全的 `cleanup_warning`/`cleanup_error_type`。job 查詢只接受 32 位小寫十六進位 ID。

JSON 會拒絕 `NaN`/`Infinity` 與 `1e9999` 等溢位數；無效 timestamp 寫入 `NULL` 並產生只含欄位和值類型的 warning。一般 CLI/Web 讀取與預設 `/api/health` 僅執行有界 schema gate，不執行 `foreign_key_check`；`verify` 與 `/api/health?deep=true` 才會串流執行完整全庫檢查。總數精確，記憶體只保留有界 sample，完整性模式、完成時間、generation 與 stale 欄位會說明結果；CPU 與 SQLite VM 工作量仍隨資料庫大小成長。parent-cycle node 與 component 數維持分開。effective-current verify counter 以對話為單位，獨立回報 selected-chain 與 raw-flag topology 的 cycle/missing/cross/partial，aggregate counter 不再誤標為 selected-chain 問題。

長訊息正文使用綁定內容 revision 的 opaque cursor 與 SQLite 增量 BLOB 讀取分頁；數字 offset 相容掃描最多 1,048,576 字元，之後必須使用 cursor。raw preview 使用一次可處理 NUL 的有界 BLOB 查詢，並回報位元組大小與精確狀態。可見文字中的 NUL 與孤立 surrogate 一致替換為 U+FFFD，raw JSON 維持安全跳脫。搜尋結果標量有明確顯示預算及截斷/原長度 metadata，ID 不會截斷。

CLI 與 Web 對話匯出在物化前採用固定總邊界：每個對話最多 100,000 個 node、單 node canonical/raw 輸入最多 32 MiB、對話輸入合計最多 128 MiB，串流輸出最多 256 MiB；effective-current 線上物化同樣限制每對話 100,000 node 與 128 MiB 圖 ID 輸入。CLI 在目標目錄分塊寫入暫存檔，同時計算 hash 並串流比較舊檔，只原子替換變更內容。瀏覽器複製以 `ReadableStream` 讀取，超過 16 MiB UTF-8 或 8 Mi 字元會在寫入剪貼簿前取消並提示改用下載，不會寫入 partial text。

全封存匯出只掃描一次 conversation 並寫入同輸出目錄的暫存 SQLite plan，在磁碟分配 collision-safe 檔名並串流產生 hash 與 JSONL/CSV manifest；上限為 1,000,000 conversation、1 GiB plan metadata、每個 manifest 2 GiB。全域 effective-current 上限為 100,000 conversation、1,000,000 node、512 MiB 圖輸入及 1 GiB 估算暫存資料，batch 最多 20,000 row/node 與 64 MiB 輸入。

message search page 一律含 `total_exact`；空資料庫或可確定為空時是 true，`count_total=false` 的一般探測是 false；conversation page 不保證此欄位。around metadata 分開表示 found、effective-current membership、requested-path membership、visible 與 applied。空 canonical 或 legacy placeholder 可從有界且有效的 raw text 恢復，reader、兩種搜尋、highlight、copy、CLI/Web export 共用 resolver；非法、過大或真正非文字 raw 維持 placeholder。

僅篩選和僅排除可篩選 conversation；只有正向訊息正文詞會產生 message hit、reader 醒目提示與 hit navigation。「複製 URL」使用同一個已套用的 search/list/selected context，不會混入 debounce 中的新輸入。日文與西班牙文在選擇器明確標為部分翻譯。release 收集前會驗證獨立的權威必要檔案清單，缺少任何必要 source/config/doc 都會失敗且不覆蓋舊 ZIP。

request validation 回應最多包含 16 個安全項目，每項只有白名單化的 `location`、`field` 與穩定公開 `code`，絕不回顯 body、path/query 原值或框架驗證類型。Raw API 分開回報精確 UTF-8 byte 與 character 單位。message search 的 candidate 精確驗證、晚位置 snippet、enrichment 與序列化回應都有界；匯入範圍內透過增量 BLOB 精確驗證，更大的 legacy candidate 穩定回傳 413，而不是 false-exact。Web index 計量實際讀取、正規化與 FTS bind 的 byte。Release payload 採分塊串流 hash、寫入與驗證。

Python `zipfile` 與本專案匯入管線支援 ZIP64 結構，並有小型強制 ZIP64 member 回歸測試；一般驗收未建立實體大小超過 4 GiB 的 ZIP。所有 member、byte、壓縮比、磁碟與 CPU 限制仍然適用。

長時間 CLI/Web 串流匯出會刻意保持同一個 SQLite read snapshot，直到完成、失敗或用戶端中斷。在 WAL 模式下，長 reader 可能延遲 checkpoint，並在並行寫入時造成 WAL 增長；持續時間、CPU/VM 工作、WAL 與暫存磁碟仍隨所選資料規模增長，不能藉由破壞 snapshot 一致性來提早 checkpoint。

`npm run build` 使用 `webui/scripts/build.mjs`，先 typecheck，再建置到同層 staging 目錄，驗證 staged `index.html` 引用的所有資源，先發布資源，最後原子取代 `dist/index.html`。注入失敗自我測試保證失敗建置仍保留舊入口及其引用資源可用。

搜尋 candidate 會分別遵守匯入元素的 32 Mi 字元與 32 MiB UTF-8 上限，並透過增量 BLOB 讀取精確驗證；可信本機測試可用 `CHATGPT_ARCHIVE_SEARCH_EXACT_VERIFY_CHARS` opt in，最多 100 Mi 字元，該明確 opt-in 也會允許相應的合法 UTF-8 位元組容量。更大的 legacy candidate 回傳 HTTP 413 `search_candidate_exact_verify_limit`，不會偽裝成 exact 空結果。長文字 cursor 綁定目標 row revision，無關 row 更新不會使其失效。

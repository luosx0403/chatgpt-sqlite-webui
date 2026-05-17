# ChatGPT Export Archiver

语言: English | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

ChatGPT Export Archiver 是一个本地优先、重视隐私的归档工具，用来把 OpenAI / ChatGPT 导出的 ZIP 文件整理成可搜索的 SQLite 数据库。它不会把原始导出内容送进浏览器，支持可重复的增量导入，提供命令行导出与搜索工具，也包含一个本地 React Web UI，方便浏览和从页面选择 ZIP 导入。

## 项目功能

- 从 OpenAI / ChatGPT 导出的 ZIP 或解压后的导出目录中导入 `conversations.json`。
- 保存会话元数据、mapping nodes、消息角色、正文文本、时间戳、父节点关系和导入警告。
- 支持增量导入。把较新的导出文件再次导入同一个数据库时，会更新发生变化的会话，而不是有意重复写入未变化的数据。
- 建立可选的 FTS5 消息索引，供命令行搜索使用。
- 建立可选的 Web 子串搜索索引，提升浏览器搜索体验。
- 支持导出 Markdown、TXT 或两种格式同时导出。
- 提供 `verify`、`stats` 和不会打印聊天正文的隐私友好型 `inspect` 命令。
- 提供本地 Web UI。即使数据库还不存在，也可以先启动页面，再从浏览器选择 ZIP 导入。
- 日志与结构化命令输出分离，并避免记录标题、snippet、raw JSON 或消息正文。

## 隐私

所有处理都在本机完成。数据库、导出的文件、临时上传副本、Web UI 和日志都留在你的电脑上，除非你自己移动或发布它们。命令行默认打印的是 ID、计数、时间戳和状态行，而不是消息片段。CLI summary 和日志不会输出聊天正文、标题、snippet、raw JSON、完整输入/输出路径或真实 ZIP 文件名；导入 summary 只报告输入类型，例如 `source zip`。Web UI 面向本地使用，默认绑定到 `127.0.0.1`。

在导入 summary 中，`valid_conversations` 统计的是去重合并前已经解析通过的输入 conversation 元素。发生重复 id 合并时，它可能大于最终的 `inserted_conversations`、`updated_conversations` 或 `unchanged_conversations` 数据库变更计数。

`inspect` 和 scanner 错误默认不会打印真实 ZIP 文件名或完整路径。`verify`、`stats`、`search`、`export` 等需要既有数据库的 CLI 命令在数据库路径写错时会报告 `database_not_found`，不会创建空 SQLite 文件。Web 搜索在可用时把可选 trigram 索引作为候选召回层，随后仍执行规范化子串过滤，因此短查询、符号和不支持 trigram 的情况会安全回退。

`--delete-input-on-success` 只会在主导入事务成功后执行。显式输入是 symlink 时，它删除命令行指定的 symlink 本身，不删除该 symlink 指向的真实 ZIP 文件。

数据库和导出的 Markdown / TXT 仍可能包含私人聊天内容。请把 `archive/*.db`、导出文件和原始 ChatGPT 导出 ZIP 都当作敏感资料处理。

## 环境要求

- Python 3.10 或更新版本。
- SQLite 需要启用 JSON1 和 FTS5。当前 macOS、Windows 和 Linux 上的大多数 Python 构建都已经包含二者。
- 只有在你需要重新构建 React Web UI 或运行前端检查时，才需要 Node.js 和 npm。runnable 交付包已经包含 `webui/dist`，正常本地使用 Web UI 不需要重新构建前端。
- 如需使用 Web ZIP 上传功能，请安装 `requirements-web.txt` 中的 Web 依赖。

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

## 快速开始

把 ChatGPT 导出 ZIP 放在仓库外部，然后运行最快的安全导入命令。这个命令跳过输入哈希，并在导入末尾一次性重建 FTS；对大型归档来说，比逐条维护 FTS 快得多。

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows PowerShell 等价写法：

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows cmd.exe 等价写法：

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

启动本地 Web UI：

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

如果还没有数据库，Web UI 也能启动，并会显示空状态和导入面板。你可以在浏览器中选择 ChatGPT 导出 ZIP；后端会写入一个本地临时副本，完成导入后自动运行 `verify`、`stats` 和 `web-index`。

```bash
python chatgpt_archive.py web --port 8787
```

## 常用 CLI 流程

检查导出文件，但不打印聊天内容：

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

显式建立一个空数据库：

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

使用大型归档路径导入：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

检查结构一致性：

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

查看结构化计数和时间边界：

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

通过 CLI 搜索路径搜索消息正文。输出只包含 conversation ID、node ID 和角色，不包含 snippet：

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

把会话导出为 Markdown、TXT，或在同一次运行中同时导出两种格式。`--format md` 写 Markdown 正文文件并更新 manifest，`--format txt` 写 plain text 正文文件并更新 manifest，`--format all` 同时写两种正文文件并更新 manifest：

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

按日期范围导出，并在必要时重写已有文件。`--from` 和 `--to` 的日期边界只接受 `YYYY-MM-DD`：

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

导出 summary 报告的是正文文件计数。`written` 统计最终字节发生变化的 Markdown/TXT 正文文件，`skipped_unchanged` 统计未变化的 Markdown/TXT 正文文件。manifest 会按需更新，但不计入这两个计数。

重建可选 Web 搜索索引：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

启动 Web UI：

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## 导入模式

推荐的大型归档命令是：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

如果你愿意让 SQLite 在导入后额外花时间整理 planner statistics 和 FTS 索引，可以使用：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` 默认关闭。只有在你已经有另一份 ZIP 备份时才建议使用。删除动作只会在主导入事务成功后执行。若删除成功，CLI 打印 `deleted_input True`，不打印路径。若删除失败，导入仍然算成功，run 保持 `finished`，写入结构化 `delete_input_failed` warning，CLI 只打印 `delete_input_failed True` 和异常类型。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

增量导入是正常使用方式。把较新的导出再次导入同一个数据库时，会更新已变化的会话，并保留其余归档数据。

## Web UI 流程

Web UI 是由 FastAPI 提供服务的本地 React 应用。推荐路径是直接使用 runnable tree 中已经构建好的 `webui/dist`。

```bash
python chatgpt_archive.py web --port 8787
```

Web UI 有两种使用方式。如果数据库已经存在，可以显式传入数据库路径，也可以使用默认路径。如果数据库不存在，也可以先启动 Web UI，再用导入面板上传 ChatGPT 导出 ZIP。上传导入会串行执行，同一进程内一次只允许一个 SQLite writer。

Web 上传导入成功后，后端使用与 CLI 相同的核心 import pipeline，然后运行 `verify`、`stats` 和 `web-index`。上传 ZIP 是服务端临时副本，会独立于你磁盘上的原始文件进行清理。


## Web UI 验收清单

修改 Web 路径或准备 runnable 交付包时，可以用这份清单检查：

- 在没有数据库的情况下启动 Web UI，并确认页面能提供空状态契约。
- 从浏览器导入一个小型 ChatGPT 导出 ZIP，并确认 job 正常完成。
- 确认上传导入后，后端会运行 `verify`、`stats` 和 `web-index`。
- 刷新页面，确认会话可以列出并打开。
- 再导入一个更新的 ZIP，确认增量路径仍可使用。

runnable 交付包中的 Web 路径不应依赖 `webui/node_modules`，因为构建好的 React assets 已经由 `webui/dist` 提供。

## 搜索语法

CLI 搜索使用项目统一的安全查询语法，不直接使用 SQLite 查询文本。可以使用普通关键词、带引号的短语、`-term` 排除、`OR`，以及 `role:user`、`source:zip`、`path:current`、`path:all`、`scope:title`、`scope:message` 等过滤条件。输出只包含 conversation ID、node ID 和角色，不包含 snippet。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web 搜索使用 `web-index` 建立的可选 normalized trigram 索引，适合浏览器中的实用子串搜索。如果这些可选索引缺失或损坏，请重建：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## 验证与可选 Web 索引

`verify` 会检查 SQLite integrity 和项目层级的一致性，包括缺失的 current node、断裂的父节点链接、空会话和父节点环。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

如果 `PRAGMA integrity_check` 报告 `web_message_trigram` 或 `web_title_trigram` 的 FTS5 inverted index 损坏，核心会话数据仍可能是结构正常的，只是可选 Web 搜索索引损坏。此时 `verify` 会报告 `optional_web_index_error true` 并打印恢复提示。用下面的命令重建可选 Web 索引：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

这个诊断是保守的。只有当所有 integrity-check 错误都能归因到这些可选 Web 索引表或其 FTS5 shadow tables 时，才会标记为可选 Web 索引问题。

## 日志

日志等级为 `debug`、`info`、`warning`、`error` 和 `none`。默认等级是 `warning`。越详细的等级会包含其后更安静等级的内容。日志不会包含标题、snippet、raw JSON 或消息正文。

日志参数可以写在子命令之前，也可以写在子命令之后：

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

请把 JSON logs 放在 `logs/` 这类已忽略位置。`*.jsonl` 是本地日志产物，delivery clean 会拒绝它们。

导入计时字段包括 `source_scan_seconds`、`parse_and_upsert_seconds`、`fts_rebuild_seconds`、`finalize_commit_seconds`、`close_seconds`、`legacy_pre_commit_seconds`、`wall_total_seconds` 和 `total_import_seconds`。`total_import_seconds` 是端到端 wall time，包含最终 commit 和 close。

导入事务成功完成后，后续 summary update 都是 best-effort。`summary_update_after_commit_failed`、`import_connection_close_failed` 和 `summary_update_after_close_failed` 是警告，不会把已经成功的导入标记为失败。

## 开发与验收检查

运行 Python 检查，并在第一次 delivery clean 前清理安全的生成物：

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

构建并 smoke-test Web UI：

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

`clean_generated_artifacts.py` 是跨平台工具，并会保留 `webui/dist`。它只删除可安全再生成的文件，不会删除数据库、ZIP、SQLite sidecar、`archive/`、`exports/` 或 `logs/`；如果 delivery clean 仍报告这些敏感路径，请把它们移出项目根目录或手动删除。验收命令使用 `--fail-on-blocked`，因此敏感残留会立即中止交付流程。

Windows PowerShell 或 cmd 用户在 search query 包含空格时请使用双引号，例如 `"python sqlite"` 或 `"role:user path:current python -pandas"`。上面的 Python、Web、Web index、typecheck、build、cleanup 和 delivery-check 命令在 Python 与 Node 位于 `PATH` 时可用于 macOS、Windows 和 Linux。如果 Windows 使用 Python launcher，可用 `py -3 tools/clean_generated_artifacts.py --fail-on-blocked` 运行清理工具。

检查 ZIP 交付包：

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## 交付说明

runnable delivery 应包含 Python 源码、测试、文档、`requirements-web.txt` 和 `webui/dist`。不应包含 `webui/node_modules`、`webui/tsconfig.tsbuildinfo`、Python 缓存目录或字节码、coverage/typecheck 缓存、`.DS_Store`、`__MACOSX`、`Thumbs.db`、`Desktop.ini`、`.gitignore.md`、临时日志、本机验收日志、`*.log`、`*.ndjson`、`*.jsonl`、`archive/`、`exports/`、任何 `*.zip`、`conversations*.json`、`*.db`、`*.sqlite`、`*.sqlite3` 等真实数据库文件，或 `*.db-journal`、`*.sqlite-wal`、`*.sqlite-shm`、`*.sqlite-journal`、`*.sqlite3-wal`、`*.sqlite3-shm`、`*.sqlite3-journal` 等 SQLite sidecar。目录检查允许目标根目录自己的 `.git`，因此普通 Git clone 可以直接检查；嵌套 `.git` 会失败，ZIP delivery 中任何 `.git` 都会失败。

source-only delivery 可以省略 `webui/dist`，但之后需要先重新构建前端，才能提供完整 React UI。

## 源码树说明

```text
chatgpt_archive.py                 CLI 入口
chatgpt_export_archiver/cli.py     CLI 命令和可复用 import pipeline
chatgpt_export_archiver/db.py      SQLite schema、导入 helper、verify、stats、FTS helper
chatgpt_export_archiver/web_app.py FastAPI app factory 和静态 UI 服务
chatgpt_export_archiver/web_api.py Web API routes
chatgpt_export_archiver/web_db.py  Web 查询 helper 和可选 trigram index builder
chatgpt_export_archiver/web_jobs.py Web ZIP 导入 job manager
webui/                             React 前端源码和构建后的 dist 文件
tests/                             Python 单元测试和集成测试
tools/                             交付检查和辅助脚本
```

## 数据库概览

主数据库保存 conversations、mapping nodes、import runs 和 warnings。CLI FTS 表是 `message_fts`。可选 Web 搜索辅助表包括 `web_message_norm`、`web_title_norm`、`web_message_trigram`、`web_title_trigram`，以及 SQLite FTS5 shadow tables。

除非已经明确规划并记录 migration，否则项目不会在小型健壮性修复中修改数据库 schema。

## 已知限制

- 这是本地归档工具，不是云同步服务。
- Web UI 面向本地使用。不要在没有额外访问控制的情况下暴露到不可信网络。
- 导出解析遵循目前观察到的 OpenAI / ChatGPT 导出格式。如果上游导出结构变化，应先更新 `inspect` 和测试，再信任新的导入路径。
- 超大型归档在导入、重建 FTS 和构建 Web trigram 索引时都可能需要时间。大型导入优先使用 `--rebuild-fts` 路径。

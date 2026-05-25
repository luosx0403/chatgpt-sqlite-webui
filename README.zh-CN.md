# ChatGPT Export Archiver

语言: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

把 ChatGPT 官方导出的 ZIP 变成私有、可搜索的 SQLite 知识档案。

`本地优先` · `SQLite` · `隐私优先` · `快速导入` · `快速 Web 索引` · `聊天式 Web UI` · `Markdown/TXT 导出`

ChatGPT Export Archiver 可以直接导入 OpenAI / ChatGPT 官方 export ZIP，写入 SQLite，验证归档，建立搜索索引，打开本地 Web UI，并把会话导出为 Markdown 或 TXT。它适合长期个人归档、离线检索、知识库迁移，以及官方历史记录页面没有提供的本地文件和索引工作流。

## 为什么值得用

- **本地优先，保护隐私。** ZIP、数据库、导出文件、临时上传副本、Web UI 和日志都留在你的电脑上，除非你自己移动。
- **直接导入 ZIP。** 可以直接读取 ChatGPT 官方导出 ZIP，不需要手动解压或合并 shard。
- **面向大型归档。** 推荐导入路径支持大 ZIP、增量导入、延后 FTS 重建和经过优化的可选 Web 搜索索引。
- **聊天式阅读器。** Web UI 默认改为类似 ChatGPT 网页的布局：user 在右侧，assistant 在左侧，system/internal 默认折叠但可展开。
- **保留经典技术视图。** 可以在设置里切换，或通过 `?layout=classic` / `?messageLayout=classic` 回到旧的逐行布局。
- **更适合归档的搜索。** 在长期归档和本地检索场景下，本地 SQLite 搜索比官方历史页面更可控：支持 role/title/source/scope/exclude、短语、OR、分页、verify、可重建索引和导出。
- **可迁移导出。** Markdown 和 TXT 导出是确定性的，适合备份、本地知识库、离线 grep 和迁移。

## 截图

安全截图待补充。截图应使用 synthetic 假数据，不能包含真实聊天标题、snippet、raw JSON、邮箱或本机路径。

## 本地 Smoke 观察

以下只是某台本地机器上的示例观察，不是通用性能承诺：

- 约 2.25 GB 的真实导出 ZIP 使用大型归档路径导入约 98 秒。
- 该归档的 `verify` 约 4 秒完成。
- 较大增量归档后的可选 Web 索引重建约 106 秒完成。
- 本地 Uvicorn Web 应用上的高命中消息搜索约 0.3 秒返回。

## 项目功能

- 从 OpenAI / ChatGPT 导出的 ZIP、单个 `conversations.json` 文件或解压后的导出目录中导入 `conversations.json` 和 sharded `conversations-*.json`。
- 保存会话元数据、mapping nodes、消息角色、正文文本、时间戳、父节点关系、来源跟踪和导入警告。
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

`--input` 可以指向官方导出 ZIP、单个 `conversations.json`，或解压后的导出目录。解压目录可以包含 `conversations.json`，也可以包含分片形式的 `conversations-*.json`；不要手动合并分片。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
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

输入可以是 ZIP、单个 `conversations.json`，或包含 `conversations.json` 或分片 `conversations-*.json` 文件的解压目录。scanner discovery 会忽略 `__MACOSX`、AppleDouble `._*` 文件和 `.DS_Store` 等 macOS metadata paths，因此这些本地 artifact 不会成为 conversation source。

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

默认阅读布局是 `chat`：user 消息靠右，assistant 消息靠左，system/internal 消息以折叠提示显示。要使用旧的逐行技术布局，可以在设置里选择「经典逐行」，或在 Web UI URL 后添加 `?layout=classic` 或 `?messageLayout=classic`。

阅读器复制和导出动作遵守可见阅读器契约。`复制当前路径整段对话` 会按当前 reader 路径抓取全部分页，并遵守「显示内部消息」开关，同时忽略当前搜索筛选。`复制当前可见` 只复制已经加载的可见消息。下载链接使用同样的当前路径和「显示内部消息」设置。Raw 消息访问只通过单条消息 endpoint 提供有上限的较大 raw 预览；截断响应必须把 `raw_text` 当作纯文本预览渲染，UI 只显示这个 capped preview。

reader 使用 `around_node_id` 跳转到命中时，会使用与 reader 相同的分页集合：Show internal 关闭时使用 visible-only rows，Show internal 开启时使用完整 node collection；对没有 current-path node 的损坏 conversation，使用 effective all-node collection。

Web UI 有两种使用方式。如果数据库已经存在，可以显式传入数据库路径，也可以使用默认路径。如果数据库不存在，也可以先启动 Web UI，再用导入面板上传 ChatGPT 导出 ZIP。上传导入会串行执行，同一进程内一次只允许一个 SQLite writer。

Web 上传导入成功后，后端使用与 CLI 相同的核心 import pipeline，然后运行 `verify`、`stats` 和 `web-index`。上传 ZIP 是服务端临时副本，会独立于你磁盘上的原始文件进行清理。

## Web 上传安全限制

Web 上传在导入 job 启动前执行应用层安全限制。这些限制由环境变量控制，与 CLI `import` 无关（CLI 不使用这些限制）。

Web 上传会在读取文件前先保留 pending slot，因此大型上传不能与另一个 writer 竞争。从保留 slot 之后发生的任何错误，包括临时上传路径创建失败，都必须释放 slot 并清理服务端临时目录；成功启动的 import job 会接管 slot 和临时副本。

当 Web UI 绑定到 loopback 地址（`127.0.0.1`、`localhost`、`::1`）时，默认允许大型可信归档：

| 环境变量 | 本机默认值 | 控制内容 |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | 压缩 ZIP 上传总大小 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | 单个 JSON member 最大未压缩大小 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | conversation JSON member 最大数量 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | 未压缩 JSON 数据总量上限 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | 大型 JSON member 最大压缩比 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | ZIP 内总 member 数上限 |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | 未设置 | 只在可信非 loopback 网络上设为 `local`，让未设置的上传限制使用本机默认值 |

**远程绑定策略。** 如果 Web UI 绑定到非 loopback 地址（如 `0.0.0.0`、`::`、局域网 IP），服务端会使用保守的 remote-safe 默认值：128 MiB 压缩 ZIP、256 MiB 每 JSON member、512 MiB 总未压缩、200.0 压缩比、200 个 JSON member、10,000 个 ZIP 总 member。设置 `CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true` 只允许显式设置的单项限制超过这些 remote-safe 默认值；未设置的限制仍保持 remote-safe。如需在可信 LAN 上让未设置限制恢复本机大默认值，设置 `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local`。两种远程模式都会暴露本地归档浏览器和上传入口，因此只应在可信网络使用。

`/api/schema` 会报告当前运行主机的有效上传策略，包括 remote-safe、显式远程 override 或 local-profile 限制是否生效。ZIP 大小检查会在导入前执行，但直接 JSON 解析、SQLite 写入和 `web-index` 重建仍会按解码后的 conversation JSON 大小消耗内存、磁盘和 CPU。超大归档建议在可信本地环境使用 CLI import，并准备足够磁盘和内存。

如需为合法的大型归档提高本机限制，在启动 Web UI 前设置相应变量：

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

在可信内网只提高显式压缩 ZIP 上限，同时让其他未设置限制保持 remote-safe：

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

在可信内网使用完整本机上传 profile：

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

仅对你信任的本机文件设置更高限制。更大的值会提高 ZIP bomb、磁盘压力和 CPU/内存风险。


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

排除词对会话结果采用 conversation-level 语义：只要所选搜索 scope 和 path 内任意标题或消息命中排除片段，该 conversation 就不会返回。`/api/search/messages` 仍只返回自身不包含排除片段的消息命中。`path:current` 按每个 conversation 遵守 reader 路径；如果损坏归档完全没有 current-path node，current-path 搜索会 fallback 到 reader 显示的同一个 all-node 视图。

日期筛选（例如 `after:2026-05-01`、`before:2026-05-13`、`--from`、`--to`）使用 UTC 自然日，而不是你本地时区的自然日。起始日期包含当天 `00:00:00Z`；结束日期会用次日 `00:00:00Z` 作为排他上界，因此 `23:59:59.5Z` 这类小数秒时间戳仍包含在当天内。Web 搜索框最多 500 个字符；更长的结构化条件请使用高级筛选。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web 搜索使用 `web-index` 建立的可选 normalized trigram 索引，适合浏览器中的实用子串搜索。如果这些可选索引缺失或损坏，请重建：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

搜索 diagnostics 是 best-effort 性能提示。它们只能报告 normalized-safe 候选层或扫描 fallback，例如 normalized trigram、normalized scan、normalized title scan 或 full scan。可以单独报告 legacy raw FTS 是否存在，但不能把它声明为实际 candidate backend，因为它可能漏掉规范化等价文本。

如果你手动执行 `VACUUM`、`VACUUM INTO`，或使用外部工具重写/压缩 SQLite 数据库，请在继续依赖 Web UI 搜索前重新运行 `python chatgpt_archive.py web-index --db <archive.db>`。Web UI 的可选索引可以从主数据表安全重建，不会改变原始会话数据。

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

runnable delivery 应包含 Python 源码、测试、文档、`requirements-web.txt`、`webui/src` 和 `webui/tests` 下的前端源码与测试、前端配置/package 文件，以及 `webui/dist` 下的构建产物。不应包含 `webui/node_modules`、`webui/tsconfig.tsbuildinfo`、Python 缓存目录或字节码、coverage/typecheck 缓存、`.DS_Store`、AppleDouble `._*` 文件、`__MACOSX`、`Thumbs.db`、`Desktop.ini`、`.gitignore.md`、临时日志、本机验收日志、`*.log`、`*.ndjson`、`*.jsonl`、`archive/`、`exports/`、任何 `*.zip`、`conversations*.json`、`*.db`、`*.sqlite`、`*.sqlite3` 等真实数据库文件，或 `*.db-journal`、`*.sqlite-wal`、`*.sqlite-shm`、`*.sqlite-journal`、`*.sqlite3-wal`、`*.sqlite3-shm`、`*.sqlite3-journal` 等 SQLite sidecar。目录检查允许目标根目录自己的 `.git`，因此普通 Git clone 可以直接检查；嵌套 `.git` 会失败，ZIP delivery 中任何 `.git` 都会失败。

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

除非已经明确规划并记录 migration，否则项目不会在小型健壮性修复中修改数据库 schema。缺少新列的旧数据库会通过 `verify` 或 Web health 的 `missing_columns` 诊断被明确拒绝，而不会静默迁移；遇到这种情况请先备份旧库，再用原始导出重新导入到新数据库。

## 已知限制

- 这是本地归档工具，不是云同步服务。
- Web UI 面向本地使用。不要在没有额外访问控制的情况下暴露到不可信网络。
- 导出解析遵循目前观察到的 OpenAI / ChatGPT 导出格式。如果上游导出结构变化，应先更新 `inspect` 和测试，再信任新的导入路径。
- 导出文件名片段会同时按 Windows 和类 Unix 系统清理，包括 `CON`、`AUX`、`COM1`、`LPT9`、`COM¹`、`LPT²` 等保留设备名，以及尾随点和空格。
- 超大型归档在导入、重建 FTS 和构建 Web trigram 索引时都可能需要时间。大型导入优先使用 `--rebuild-fts` 路径。

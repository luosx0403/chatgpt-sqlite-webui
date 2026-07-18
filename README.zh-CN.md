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

- Python 3.10 或更新版本；Python 3.12 是 Web 依赖可复现安装的测试目标。
- SQLite 需要 JSON 支持。FTS5 是可选能力；不可用或缺少 `message_fts` 时仍使用安全扫描搜索。
- 只有在你需要重新构建 React Web UI 或运行前端检查时，才需要 Node.js 和 npm。runnable 交付包已经包含 `webui/dist`，正常本地使用 Web UI 不需要重新构建前端。
- 核心 CLI 只使用 Python 标准库，没有 Web 包也能运行。如需启动 Web UI（包括 ZIP 上传），请安装 `requirements-web.txt`；缺少该 profile 时，`web` 命令会立即失败并给出安装提示。

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

Python 3.12 constraints 文件会固定所有已解析 Web 依赖的版本，但它不是跨平台 hash lock。后续发布流程应为支持的 Python/OS 矩阵生成并校验各平台 hash；在此之前，只应从可信包索引安装，并把 npm lockfile 与 audit 检查作为独立的前端控制。

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

CLI 与 Web 导出默认使用有效当前路径，并且只包含可见消息。需要包含分支或内部消息时，请显式使用 `--path all` 和/或 `--include-internal`；manifest 会记录这两个选择。CLI 导出会分批、限量读取对话节点；Web 下载与`复制当前路径整段对话`使用专用的有界服务端文本流。因此，完整规范文本以及符合条件的 legacy/raw 恢复文本不会受 reader 响应预算截断。

重建可选 Web 搜索索引：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

`web-index` 会按明确阶段扫描并规范化消息与标题，并在支持时构建 trigram 索引。每次构建使用不可预测的独立 staging 名称与持久 owner-token lease；第二个构建会以 `web_index_build_in_progress` 拒绝，过期清理也必须验证精确所有权。所有阶段均使用有界 keyset 与输入、规范化、派生及 FTS-bind 字节预算并报告实测峰值。批次之间释放 writer lock；最终用一个短 `BEGIN IMMEDIATE` 事务复核 canonical generation、对象所有权和 metadata 后原子发布。提交前 reader 始终看到旧索引；generation 变化、SQLite 中断、磁盘错误或取消都会保留旧索引，并且只清理该 lease 拥有的对象。Web import job 会报告 stage 与 processed/total，`POST /api/import/jobs/{job_id}/web-index/cancel` 取消仅适用于导入任务的索引阶段。超预算行会记录并对 canonical text 做精确验证，构建索引不会降低召回率。

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

对于目录输入，POSIX 平台会在可用时逐 component 使用 `dir_fd` 与 `O_NOFOLLOW`。便携 fallback 会拒绝 symlink/reparse component，并在按路径打开前立即校验 containment，但 Python 标准库无法在所有平台消除所有本地 replacement race。不要导入不可信本地用户或并发进程可修改的解压目录；此威胁模型应使用原始只读 ZIP。

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

所有入口共用同一套 `path=current` effective-current 规则：有效且属于该会话的 `current_node` 及其父链优先，即使所有 raw flag 都为 0；否则选择确定的可用 `is_on_current_path=1` 叶链；只有两者都不存在时，该会话才 fallback 到 all。响应保持 raw flag 原义，并提供 `current_node_exists`、`current_collection_source`、`current_path_fallback_to_all`、`effective_path` 和逐节点 effective visibility。断裂父链和 cycle 会有限、确定地诊断，不会让递归查询挂起。

全局 current-path 搜索会先通过规范化正文/标题索引以及安全的 source/date/role 条件取得不依赖路径的会话候选，再只为这些候选建立 effective-current membership；只有无法缩小的仅排除查询才显式回退到全库。Reader 命中导航初始只取一个紧凑页面，接近已加载边界时才继续追加。搜索与 Web 索引 SQL 使用可移植的防扁平化查询结构，不要求 SQLite 支持 `AS MATERIALIZED`，并保证每个 legacy raw 候选在每个逻辑阶段最多解析一次。

阅读器复制和导出动作遵守可见阅读器契约。`复制当前路径整段对话` 使用专用的完整文本流处理当前 reader 路径，遵守「显示内部消息」开关并忽略当前搜索筛选，不会在浏览器中累积 reader 分页。`复制当前可见` 只复制已经加载的可见消息。下载链接使用同样的当前路径和「显示内部消息」设置。Raw 消息访问只通过单条消息 endpoint 提供有上限的较大 raw 预览；截断响应必须把 `raw_text` 当作纯文本预览渲染，UI 只显示这个 capped preview。

reader 使用 `around_node_id` 跳转到命中时，会使用与 reader 相同的分页集合：Show internal 关闭时使用 visible-only rows，Show internal 开启时使用完整 node collection；对没有 current-path node 的损坏 conversation，使用 effective all-node collection。

Web UI 有两种使用方式。如果数据库已经存在，可以显式传入数据库路径，也可以使用默认路径。如果数据库不存在，也可以先启动 Web UI，再用导入面板上传 ChatGPT 导出 ZIP。上传导入会串行执行，同一进程内一次只允许一个 SQLite writer。

Web 上传导入成功后，后端使用与 CLI 相同的核心 import pipeline，然后运行 `verify`、`stats` 和 `web-index`。上传 ZIP 是服务端临时副本，会独立于你磁盘上的原始文件进行清理。

预检失败与终态导入任务都可能返回多个 `cleanup_warnings`。React UI 会把每个安全 warning code 与 `path_kind` 显示为本地化用户文案，同时兼容已弃用的单值 `cleanup_warning`。这些提示不会显示临时路径、文件名、OS message 或 error class；重复轮询会替换同一任务快照，不会追加重复提示。

如果无法提供预构建 React 应用，服务端 fallback HTML 只是功能受限的紧急界面，并非完整 reader 的替代品。它的搜索和阅读控制较少，下载默认排除 internal node；要使用完整 UI，请重新构建 `webui/dist`。

## Web 上传安全限制

Web 上传在导入 job 启动前执行应用层安全限制。这些限制由环境变量控制，与 CLI `import` 无关（CLI 不使用这些限制）。

Web 上传会在读取文件前先保留 pending slot，因此大型上传不能与另一个 writer 竞争。从保留 slot 之后发生的任何错误，包括临时上传路径创建失败，都必须释放 slot 并清理服务端临时目录；成功启动的 import job 会接管 slot 和临时副本。

进程被 kill、OOM 或宿主崩溃可能绕过正常清理，在操作系统临时目录遗留旧的 `chatgpt-archive-upload-*` 目录。本版本不会自动删除它们，因为 ownership/age 判断错误可能删除无关数据。停止服务器后，管理员只能在确认精确前缀、当前账号 ownership、目录年龄、没有 symlink/reparse point 且没有任务使用后，逐个删除旧目录；绝不能删除用户导出 ZIP，也不要使用未经检查的 wildcard。

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

**远程绑定策略。** 绑定非 loopback 地址（如 `0.0.0.0`、`::` 或局域网 IP）时，必须通过 `CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS=true`、`CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true` 或 `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local` 明确 opt-in，否则启动会被拒绝。remote-safe 默认值为 128 MiB 压缩 ZIP、256 MiB 每 JSON member、512 MiB 总未压缩、200.0 压缩比、200 个 JSON member和 10,000 个 ZIP 总 member；可信 loopback/local 默认压缩比为 1000.0。`ALLOW_REMOTE_UPLOADS` 只放宽显式设置的限制，未设置项仍 remote-safe；`REMOTE_UPLOAD_PROFILE=local` 会把全部未设置限制恢复为本机大默认值，只适合可信 LAN。

`/api/schema` 会报告当前有效上传策略，包括 multipart body 上限（ZIP byte 上限加有界 overhead）及 remote profile。writer slot 和 receive-level body cap 在 multipart 解析前生效。multipart parser 可能写 spool，之后 import pipeline 还会拥有服务端临时 ZIP，因此临时磁盘应预留接近两份压缩副本再加数据库增长。ZIP 检查先执行，但 JSON 解码、SQLite 写入和 `web-index` 仍按解码后数据消耗内存、磁盘和 CPU。远程上传必须提供有效 `Content-Length`；loopback chunked 上传仍按流式上限限制。超大归档优先使用可信 loopback 和 CLI import。

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

CLI 搜索使用安全查询语法，不直接使用 SQLite 查询文本。普通词是 normalized substring `contains` 并默认 AND；大写 `OR` 建立备选分支。引号保留短语，`-term`/`-"quoted phrase"` 表示排除。`word` 只对 ASCII 字母、数字、下划线应用边界；CJK 在无分词时保守地保持 normalized contains。支持 `role:user`、`source:zip`、`path:current`、`path:all`、`scope:title`、`scope:message`；查询中的 raw `path:`/`scope:` 会覆盖 UI 选择器。输出不含 snippet。

排除词对会话结果采用 conversation-level 语义：只要所选搜索 scope 和 path 内任意标题或消息命中排除片段，该 conversation 就不会返回。`/api/search/messages` 仍只返回自身不包含排除片段的消息命中。`path:current` 按每个 conversation 遵守 reader 路径；如果损坏归档完全没有 current-path node，current-path 搜索会 fallback 到 reader 显示的同一个 all-node 视图。

日期筛选（例如 `after:2026-05-01`、`before:2026-05-13`、`--from`、`--to`）使用 UTC 自然日。起始日期包含 `00:00:00Z`，结束日期以次日 `00:00:00Z` 为排他上界。CLI 导出时间与确定性文件名日期使用 UTC；浏览器按浏览器本地时区显示。Web 搜索框最多 500 字符。虚拟列表只渲染部分行，因此浏览器 Cmd/Ctrl+F 看不到未渲染消息；整段搜索请使用归档搜索或复制会话。

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

`message_fts_rebuildable` 来自真实 runtime capability，而不是常量：FTS 缺失但 FTS5 可用时报告 `missing` 且可重建；FTS5 模块不可用时报告 `capability_unavailable` 且不可重建；表损坏时报告 `damaged`，是否可重建仍由同一有界 runtime probe 决定。其他 SQLite 错误会交给结构化数据库错误分类器。

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

runnable delivery 应包含 Python 源码、测试、文档、`requirements-web.txt`、`constraints-web-py312.txt`、`webui/src` 和 `webui/tests` 下的前端源码与测试、前端配置/package 文件，以及 `webui/dist` 下的构建产物。不应包含 `webui/node_modules`、`webui/tsconfig.tsbuildinfo`、Python 缓存目录或字节码、coverage/typecheck 缓存、`.DS_Store`、AppleDouble `._*` 文件、`__MACOSX`、`Thumbs.db`、`Desktop.ini`、`.gitignore.md`、临时日志、本机验收日志、`*.log`、`*.ndjson`、`*.jsonl`、`archive/`、`exports/`、任何 `*.zip`、`conversations*.json`、`*.db`、`*.sqlite`、`*.sqlite3` 等真实数据库文件，或 `*.db-journal`、`*.sqlite-wal`、`*.sqlite-shm`、`*.sqlite-journal`、`*.sqlite3-wal`、`*.sqlite3-shm`、`*.sqlite3-journal` 等 SQLite sidecar。目录检查允许目标根目录自己的 `.git`，因此普通 Git clone 可以直接检查；嵌套 `.git` 会失败，ZIP delivery 中任何 `.git` 都会失败。

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

主数据库保存 conversations、mapping nodes、import runs 和 warnings。message object 的 raw JSON 字段按完整对象保留；conversation 和 mapping-node object 会规范化，不做逐字节原样保存。输入 ZIP SHA-256 是可选项，`source_files`/`file_index` 的逐 entry SHA 列为保留字段，目前不填充。CLI FTS 表是 `message_fts`。可选 Web 搜索辅助表包括 `web_message_norm`、`web_title_norm`、`web_message_trigram`、`web_title_trigram`，以及 SQLite FTS5 shadow tables。

canonical 数据库使用 `PRAGMA user_version` 版本化（当前版本 4）。版本 3 为 canonical TEXT identity 增加 `NOT NULL`；版本 4 增加按字段划分的持久 address/graph revision，使 fresh reader 不会复用过期的 compatibility 或 effective-current 判断。Migration 在同一写锁事务安装 revision row 与 managed trigger，并按需失效 optional index。只读路径不执行 migration DDL；旧兼容数据库返回 `database_migration_required`。升级前先创建并验证外部备份。

Health 与 `verify` 会区分可选 `message_fts` 缺失和损坏。损坏时报告 `optional_message_fts_error` 与 `--rebuild-fts` 恢复提示；通用的 malformed、locked、readonly、I/O 和 SQL 运行时错误不会被伪装成能力缺失，并使用 `database_malformed`、`database_locked`、`database_readonly`、`database_io_error` 或 `database_runtime_failure`。

## Round 9 资源、所有权与恢复契约

managed FTS、可选 Web 索引、staging、metadata、generation 与 shadow 对象只有在精确核对类型、目标表、SQL 和 fingerprint 所有权后才能执行破坏性 DDL。名称冲突分别以 `core_fts_name_collision`、`optional_index_name_collision` 或 `staging_name_collision` 拒绝，绝不因名称相似就删除用户对象。可选 Web 索引格式为 4：每次构建使用不可预测的独立 staging 名和持久 owner-token lease；并发构建返回 `web_index_build_in_progress`，过期恢复也必须核对 owner、数据库身份、schema、generation、format 与全部对象名。输入、规范化、派生及 FTS bind 分别按批预算，并报告实际 current/peak 字节；完整流式 placeholder 分类不会被超过 256 字符的前缀绕过。

长正文 cursor 绑定 `archive_generations` 内部 `display:<rowid>` 键保存的持久逐行 revision。受管 insert/update trigger 会为每次影响显示文本的写入递增该 revision，即使直接外部 SQLite writer 没有刷新 `content_hash`；无关行不会使 cursor 失效。缺少这些 trigger 的既有 version-4 数据库会进入 migration-required 门禁，由显式 writer migration 回填 revision。删除行的 revision tombstone 会保留，避免 rowid 重用使旧 cursor 复活。

精确消息搜索以 64 KiB 重叠分块增量读取 canonical BLOB。每行通常限制 32 MiB 解码字符和 32 MiB UTF-8，可信本机可显式提高到 100 MiB 字符；raw-only fallback 另限 1 MiB/800,000 字符，单请求另有 128 MiB 字节/字符总预算。超限返回完整 HTTP 413，本版本不返回 partial page，也不承诺 continuation token。首个 hit 请求精确总数，后续页可分别显示已加载数；晚位置命中携带绑定行 revision 的字符 anchor，reader 直接 seek，不重放数 MB 页面。

单个新导入会话元素独立限制为 32 MiB UTF-8、32 MiB 解码字符、250,000 scalar 与 5,000 mapping node；超出 node 限制以 `conversation_node_limit_exceeded` 跳过且不保存内容。reader/effective-current/export 的 100,000-node 上限只为兼容 legacy 或外部写入数据库，不代表允许导入 100,000 node。多个 ZIP shard 共用一个读取 session；目录发现采用增量预算。空 `parent` 按 legacy root/missing-parent 兼容。legacy ID readiness 检查全部地址/图字段的长度与不安全 Unicode，并以持久字段 revision 失效缓存，普通读取不轮询 `PRAGMA data_version`。

项目批量导入在同一写锁事务内临时替换精确的项目自有 generation trigger，每个 dirty 字段域只推进一次，再恢复并校验 trigger；回滚或崩溃恢复原 DDL/数据，外部 writer 仍使用普通逐语句 trigger。有限 effective-current scope 通过有界 SQLite TEMP 批次精确比较。整库导出将 plan 与 node spool 到临时 SQLite，并 keyset 流式读取，不在 Python 中保存全归档 node graph。

使用 `--delete-input-on-success` 时，canonical commit 成功前用户原路径始终存在。commit 后先持久写入并 fsync 绑定身份的恢复 journal，再 rename；中断会留下 token，可用 `python chatgpt_archive.py recover-delete-input --directory <目录> --token <token>` 明确恢复，且绝不覆盖替换文件。Windows 或缺少 descriptor-relative no-follow 身份能力的平台会拒绝安全删除。Web Python constraints 只固定 resolved version，仍不是跨平台 hash lock；应使用可信包索引。

## 已知限制

- 这是本地归档工具，不是云同步服务。
- Web UI 面向本地使用。不要在没有额外访问控制的情况下暴露到不可信网络。
- 导出解析遵循目前观察到的 OpenAI / ChatGPT 导出格式。如果上游导出结构变化，应先更新 `inspect` 和测试，再信任新的导入路径。
- 导出文件名片段会同时按 Windows 和类 Unix 系统清理，包括 `CON`、`AUX`、`COM1`、`LPT9`、`COM¹`、`LPT²` 等保留设备名，以及尾随点和空格。
- 超大型归档在导入、重建 FTS 和构建 Web trigram 索引时都可能需要时间。大型导入优先使用 `--rebuild-fts` 路径。

## 安全与响应契约

上传入口对 `Origin`、`Content-Length` 和 `Sec-Fetch-Site` 各只接受一个值。Origin 必须是没有用户信息、路径、查询、片段、控制字符或逗号链的单一 HTTP(S) origin；Content-Length 必须是规范的非负 ASCII 十进制整数。重复或畸形的安全标头会在 multipart 解析前拒绝；无效或非有限的压缩比配置会回退到有限的安全 profile 默认值。

Loopback Web 只接受 `localhost`、`127.0.0.1`、`::1`、显式 loopback bind host 和明确配置的 host。非 loopback bind 还必须用 `CHATGPT_ARCHIVE_ALLOWED_HOSTS`（或 `--allowed-hosts`）指定实际浏览器 hostname/LAN IP，禁止 `*`。`CHATGPT_ARCHIVE_TRUSTED_PROXIES`（或 `--trusted-proxies`）采用严格单 edge 模型：未受信直连的 forwarded header 会被忽略，受信直连代理必须覆盖客户端值；重复 Host/Forwarded、逗号代理链、非法语法以及 `Forwarded` 与 `X-Forwarded-Host/Proto` 冲突会被拒绝。静态 UI、GET API 和全部请求都校验 Host。远程写入必须有同源 `Origin`；只有可信 loopback profile 兼容无 Origin 客户端。上传始终拒绝 `Sec-Fetch-Site: cross-site`。

导入失败使用稳定的输入预检、source scan、source read、JSON decode、顶层契约和事务阶段。新增稳定 code 包括 `upload_preflight_failed`、`input_source_open_failed`、`input_source_not_regular_file`、`source_read_failed`、`source_changed_during_read`、`invalid_conversation_encoding` 和 `json_integer_too_large`。清理诊断使用结构化 `cleanup_warnings` 数组；旧 `cleanup_warning` 标量仅代表首项。任何响应都不泄漏临时路径。

独立 JSON、目录成员和 ZIP 成员使用同一个单遍、逐顶层数组元素的解码器，并处于同一导入事务；每个元素只扫描和解码一次，UTF-8 输入与解码后字符数各限制为 32 MiB，嵌套最多 256 层、scalar 最多 250,000 个。legacy raw 使用迭代 sanitizer，遍历最多 100,000 个 node、raw preview 最多 80,000 bytes、完整 sanitized API payload 最多 4 MiB。ZIP 中央目录的全部 entry 与目录中的全部 entry 都计入 100,000 member 上限。只移除文件开头的一个 UTF-8 BOM；JSON 字符串内的 U+FEFF 会保留，重复开头 BOM、字符串外的中间 BOM、UTF-16/32、混合编码和无效 UTF-8 都会拒绝。新导入的 canonical 会话及图结构 ID 限制为 512 个字符，绝不截断。主要 Web 寻址使用 query-based `/api/by-id/*`，最多接受 16 Ki 字符的 legacy ID；更长的旧 ID 会令 readiness 返回 `database_data_incompatible`。ZIP source-read 会区分加密、缺失、读取期间变化、CRC 失败及其他读取失败。

文件通过 descriptor-bound stat/hash/read 校验身份；`--delete-input-on-success` 使用原子 staging rename 与最终身份屏障，无法恢复的占名竞态产生 `delete_input_recovery_required`。Migration 只接受定义完全匹配的已知 predecessor；任何用户对象以错误类型、目标或定义占用 managed trigger/index 名称，都会在 DDL 前以 `database_managed_object_name_collision` 拒绝。

非标准 JSON `NaN` / `Infinity`（包括 `1e9999` 这类溢出标准数值）会被拒绝；无效时间写成 `NULL` 并记录不含内容的 warning。默认 message API 只返回一份受 reader 预算限制的 `display_text`，并以 truncation/total-exactness metadata 说明能否完整恢复，不复制 `content_text`/`render_text` 别名。普通 CLI/Web 读取和默认 `/api/health` 使用有界 schema gate，不执行 `foreign_key_check`；`verify` 与 `/api/health?deep=true` 执行完整精确检查并提供 freshness 字段。每个多语句 CLI/Web 逻辑读取都在 schema/capability probe 前建立一个 SQLite read snapshot，流式响应正常结束或失败时均会释放。Effective-current、分页和 around-node 语义保持不变。

“复制 URL”始终显式写入 `match_mode`、`layout`、`show_internal` 及可共享搜索/reader 状态。URL 显式值优先于 `localStorage`，缺失值才可回退本地设置。本版本使用 `replaceState`，浏览器前进/后退不会恢复逐步搜索或选择历史。

Release ZIP 使用固定 member metadata 与确定顺序，同一 payload 可生成逐字节一致的结果；构建器仍校验 required-file 集合、每个 payload SHA-256、内部 manifest、dist asset，并在全部成功后原子替换。任何失败都保留旧 release。

回滚摘要用 `attempted_*` 与归零的 `committed_*` 区分已尝试和已提交工作；失败 run 用新连接持久化，并明确报告二次持久化失败。pre-job 临时目录清理失败保留主要 HTTP 错误，同时返回安全的 `cleanup_warning`/`cleanup_error_type`。job 查询只接受 32 位小写十六进制 ID。

JSON 会拒绝 `NaN`/`Infinity` 和 `1e9999` 等溢出数；无效 timestamp 写入 `NULL` 并产生只含字段与值类型的 warning。普通 CLI/Web 读取和默认 `/api/health` 只执行有界 schema gate，不运行 `foreign_key_check`；`verify` 与 `/api/health?deep=true` 才会流式执行完整全库检查。总数精确，内存只保留有界 sample，并用完整性模式、完成时间、generation 与 stale 字段说明结果；CPU 与 SQLite VM 工作量仍随数据库大小增长。parent-cycle 节点数与 component 数继续分开。effective-current verify counter 以会话为单位，独立报告 selected-chain 与 raw-flag topology 的 cycle/missing/cross/partial，aggregate counter 不再误标为 selected-chain 问题。

长消息正文通过绑定内容 revision 的 opaque cursor 与 SQLite 增量 BLOB 读取分页；数字 offset 兼容扫描最多 1,048,576 字符，之后必须使用 cursor。raw preview 用一次可处理 NUL 的有界 BLOB 查询，并报告字节大小及其是否精确。可见文本中的 NUL 与孤立 surrogate 一致替换为 U+FFFD，raw JSON 保持安全转义。搜索结果中的 title/source/role/author/content-type 等标量有明确显示预算及 truncation/原长度元数据，ID 不会截断。

CLI 与 Web 会话导出在物化前实施固定总边界：每个会话最多 100,000 个 node、单 node canonical/raw 输入最多 32 MiB、会话输入合计最多 128 MiB，流式输出最多 256 MiB；effective-current 在线物化同样限制为每会话 100,000 node 与 128 MiB 图 ID 输入。CLI 在目标目录分块写临时文件，同时计算哈希并流式比较旧文件，只原子替换变化内容。浏览器复制通过 `ReadableStream` 读取，超过 16 MiB UTF-8 或 8 Mi 字符就会在写剪贴板前取消并提示使用下载，绝不会复制 partial text。

全归档导出只扫描一次 conversation 并写入同输出目录的临时 SQLite plan，在磁盘上分配冲突安全文件名，并流式产生 hash 与 JSONL/CSV manifest；上限为 1,000,000 个 conversation、1 GiB plan metadata、每个 manifest 2 GiB。全局 effective-current 上限为 100,000 个 conversation、1,000,000 node、512 MiB 图输入及 1 GiB 估算临时数据，batch 最多 20,000 row/node 与 64 MiB 输入。

message search page 始终包含 `total_exact`；空库或可确定为空时为 true，`count_total=false` 的普通探测为 false；conversation page 不承诺该字段。around metadata 分开表示 found、effective-current membership、requested-path membership、visible 与 applied。空 canonical 或 legacy placeholder 可从有界、有效的 raw text 恢复，reader、两类搜索、highlight、copy、CLI/Web export 使用同一 resolver；非法、过大或真实非文本 raw 保持 placeholder。

仅筛选和仅排除可筛选 conversation；只有正向消息正文词才产生 message hit、reader 高亮和 hit navigation。“复制 URL”使用同一个已应用的搜索/list/selected context，不会混合 debounce 中的新输入。日语和西班牙语在选择器中明确标为部分翻译。release 在收集前验证独立的权威必要文件清单，任何必要源码、配置或文档缺失都会失败且不会覆盖旧 ZIP。

请求校验响应最多包含 16 个安全项，每项只有白名单化的 `location`、`field` 和稳定公开 `code`，绝不回显 body、path/query 原值或框架校验类型。Raw API 分别报告精确 UTF-8 byte 与 character 单位。message search 的 candidate 精确验证、晚位置 snippet、enrichment 与序列化响应都有界；导入范围内通过增量 BLOB 精确验证，更大的 legacy candidate 稳定返回 413，而不是 false-exact。Web index 计量实际读取、规范化与 FTS bind 的 byte。Release payload 以分块流式方式 hash、写入和复核。

Python `zipfile` 与本项目导入流水线支持 ZIP64 结构，并有小型强制 ZIP64 member 回归测试；常规验收没有生成物理大小超过 4 GiB 的 ZIP。所有 member、byte、压缩比、磁盘和 CPU 限制仍然适用。

长时间 CLI/Web 流式导出会有意保持同一个 SQLite read snapshot，直至完成、失败或客户端断开。在 WAL 模式下，长 reader 可能延迟 checkpoint 并在并发写入时造成 WAL 增长；持续时间、CPU/VM 工作、WAL 与临时磁盘仍随所选数据规模增长，不能通过破坏 snapshot 一致性来提前 checkpoint。

`npm run build` 使用 `webui/scripts/build.mjs`，先 typecheck，再构建到同级 staging 目录，校验 staged `index.html` 引用的全部资源，先发布资源，最后原子替换 `dist/index.html`。注入失败自测保证失败构建仍保留旧入口及其引用资源可用。

搜索候选会分别遵守导入元素的 32 Mi 字符与 32 MiB UTF-8 上限，并通过增量 BLOB 读取进行精确验证；可信本地测试可用 `CHATGPT_ARCHIVE_SEARCH_EXACT_VERIFY_CHARS` opt in，最多 100 Mi 字符，该显式 opt-in 也会允许相应的合法 UTF-8 字节容量。更大的 legacy 候选返回 HTTP 413 `search_candidate_exact_verify_limit`，不会伪装成 exact 空结果。长正文 cursor 绑定目标 row revision，无关 row 更新不会使其失效。

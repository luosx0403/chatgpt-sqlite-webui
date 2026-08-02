chatgpt-sqlite-webui 当前源码合并审计报告

审查基线：本轮上传 ZIP /mnt/data/release-64(3).zip

SHA-256：e36e10123101d3d5a08e8c4c8eee50c63de6718c0b4b10c9311cf6d4fb83cbd6

本文件把三类证据合并：当前 ZIP 的重新源码复核与生产复现、此前同一 SHA-256 源码的独立审计报告、以及独立修复 prompt 中能够由当前源码重新证明的新增问题。只保留 confirmed、structural confirmed 或明确要求再次 reproduction 的项目；历史上绑定其他最终 ZIP/hash 的性能数字、浏览器结果和环境失败不作为当前源码已经通过或已经失败的证据。

证据规则

当前源码、SQL、SQLite object inventory、production CLI、production HTTP、production React、确定性故障注入和真实资源测量优先于 README、测试名、旧报告或修复声明。

“confirmed”表示当前源码或 production entry 已直接复现。“structural confirmed”表示当前调用链足以证明，无需依赖特定容器状态。“target reproduction required”表示审计环境出现过值得关注的现象，但可能含文件系统、缓存或平台因素，不应未经目标机复现就修改业务算法。

必须修复的当前问题

1. Multipart upload 磁盘准入发生在 Starlette spool 之后

UploadIngressMiddleware 在 multipart parser 前正确取得 writer admission，并执行 Content-Length/body size 限制，但没有在调用下游 parser 前执行 free-space admission。真正的 require_free_space() 位于 UploadFile = File(...) 已经完成解析后的 route 内。

真实 ASGI receive counter 已证明 2 MiB 和 100 MiB multipart，不论有无 Content-Length，在 disk guard 执行前都已经把 100% body 交给 parser，并且 SpooledTemporaryFile 已 rollover。失败后 cleanup 正常，但无法阻止低磁盘时先写完整 multi-GiB spool。

必须区分 parser spool filesystem、pipeline upload-copy filesystem 和 DB/WAL/TEMP/Web-index filesystem。已知 Content-Length 应在读取 body 前做容量准入；无 Content-Length 的 loopback 模式不能继续允许 framework 在无动态磁盘 guard 下完整 spool。

2. Writer lock release 首错中止，且 post-commit cleanup error 会倒退公开 outcome

_WriterProcessLock.close() 在进入循环前清空 self.fds，任一 unlock/close 异常会中止剩余 FD 的处理。确定性 fault injection 已证明可以遗留仍锁定的 FD。

更严重的是 Web import job：canonical commit、verify、stats、optional Web-index 全部成功后，如果最终 writer-lock release 抛异常，公开 job 可被改写为 failed_before_commit / import_job_start_failed，同时 canonical_commit_succeeded=true。这可能诱导用户重复导入。

CLI 也会在数据已提交、底层锁实际已释放后，仅因最后 cleanup exception 返回失败。

必须改成 all-FD best-effort cleanup、聚合 cleanup warnings，并保证 terminal status/outcome 单调。canonical committed 后的 cleanup failure 不能再表示为 pre-commit failure。

3. 固定 64-shard writer registry 的 hash collision 会让无关数据库 false busy

当前 _process_lock_name() 把完整 path/entity identity 只映射到 64 个整文件 lock。文档说碰撞会“保守串行”，但 acquisition 使用 LOCK_NB，所以实际行为是 unrelated DB 立即得到 writer_process_lock_busy。

已确定性找到两个不同不存在数据库 path 映射到同一 shard。A 持锁时 B 立即失败。这个问题不是随机理论风险，而是当前实现语义。

修复必须同时保留 registry 文件/资源数量固定有界、same-DB realpath/symlink/hardlink/entity alias 互斥、不同 DB 正常并发、crash 后 kernel release、unknown registry object 不修改。不能只把 64 改成更大的常数来降低碰撞概率。

4. ZIP shard-name 检测 regex 对长不匹配 pathname 近似二次复杂度

当前 SHARD_RE = (^|.*/)conversations-(\d+)\.json$ 对整个 pathname 做 regex search。长且不匹配的 member 名会产生近似 O(length²) 行为。

已测单 path 从约 1 KiB 到 65 KiB 时耗时随长度翻倍接近 4 倍；真实 list_source_entries() 对 1,000 个约 4 KiB member name 约 6.25 s，5,000 个约 31.3 s，10,000 个超过 120 s。

应先线性提取 basename，再对短 basename 做 anchored parse，不允许整 path wildcard search。

5. ZIP central-directory bytes 与 ZIP member pathname bytes 缺少资源上限

EOCD/ZIP64 preflight 当前限制 member count，但不限制 central_size，也没有给 ZIP member pathname 应用 directory 输入已有的相对路径 byte limit。随后 zipfile.ZipFile 会一次性物化 ZipInfo/filename。

已用 10,000 个约 4 KiB filename 证明打开/枚举会显著增加 RSS。对最多 100,000 members 与 ZIP filename 16-bit length 组合，central metadata 可以远大于普通 fixture。

应在 ZipFile 之前利用 EOCD/ZIP64 的 central_size 做 byte budget，并为 filename/extra/comment 总量建立预算。

6. Streaming JSON 的 32 MiB UTF-8 byte ceiling 在高宽 Unicode 上拒绝过晚

公开合同同时限制单 element 32 MiB UTF-8 bytes 与 32 MiB decoded chars，但 _coalesce_json_text_chunks 按 32 MiB characters 才向 framer 交付窗口。对 emoji 等 4-byte UTF-8 字符，byte limit 早已超限时仍会继续保留 decoded Python string。

独立流式 generator，没有预先构造完整 payload：

9,000,000 emoji：在拒绝前实际消费约 36,000,004 UTF-8 bytes，RSS 约 250,380 KiB；

31,000,000 emoji：在拒绝前实际消费约 124,000,004 UTF-8 bytes，RSS 约 594,376 KiB。

必须把 source byte accounting 传播到 coalescer/framer，更早按 byte ceiling 拒绝，且不能为了计数反复对 growing string .encode()，避免 many-small/common path 退化。

7. schema predecessor 的 migration disk planner 没有按 step 建模

轻量 predecessor 到 current schema 的 step 主要增加 query generation/trigger/metadata 和 compatibility refresh，却仍使用面向全表 rewrite 的通用 migration_required_bytes()。

100k-node、约 14 MiB predecessor DB 的真实 migration journal/WAL 峰值只有数十 KiB量级，但 planner 要求约 768 MiB free space。小 predecessor DB 也可被错误拒绝。

必须为每个 predecessor edge 建 step-specific plan，保留 outer fast preflight 和 BEGIN IMMEDIATE 后 authoritative locked recheck。

8. Migration compatibility deep scan 不可周期 cancel/progress

最终 refresh_legacy_compatibility_state() 会扫描 conversations、nodes 和 json_each(children_json)，但 migration 只在进入前检查 cancel；revision backfill 有周期 cancel，compatibility scan 没有。

必须使用 SQLite progress handler 或可证明 exact 的 bounded/keyset 机制，使扫描可取消、可报告 content-free progress，任何 cancel/exception 都回滚整个 migration，不能留下半安装 generation/trigger、半升级 user_version 或 false compatibility current。

9. Node timestamp-only reimport 仍过度污染 message/display/Web-index domain

conversation timestamp 和 source-only 已正确只推进 query generation，但 project import 改 node create_time/update_time 时，raw_message_json 也会重序列化变化。当前 message generation 与 display revision trigger 都把 raw_message_json 当 source，所以仅从 _NODE_MESSAGE_FIELDS 删除 timestamp 字段并不足以修复。

真实结果是 node timestamp-only 可推进 message+query、改变 display revision并使 optional text index stale/missing，尽管 resolver-visible text、role 和 canonical text 没变。external SQL 只 UPDATE timestamp 则只推进 query，形成 project/external semantics 分裂。

修复必须建立 authoritative dirty-domain/display-source contract：raw serialization 仍要保存真实更新，但只有会改变实际 display/search source 的 raw 变化才能推进 message/display/index domain。query generation仍必须推进，使 date/sort continuation stale。

10. Cross-site 浏览器 GET 可以触发昂贵本地 API 工作

WriteAccessMiddleware 对 GET/HEAD/OPTIONS/TRACE 直接视为 safe，因此在任何 Fetch Metadata 检查前放行。

production TestClient 已确认带：Origin: https://evil.exampleSec-Fetch-Site: cross-site

的 /api/stats、message search、raw、display 和 export GET 均实际执行并返回 200。SOP 可阻止攻击页面读取响应，但不会阻止本地服务执行 SQLite/search/export 工作。

需要基于 Fetch Metadata 建 resource-isolation policy，而不是把所有 GET 都要求 Origin。Sec-Fetch-Site: cross-site 对昂贵/敏感 /api/* 应拒绝；same-origin 应允许；none 的直接导航以及缺 header 的 curl/API client需按公开兼容合同处理。静态 UI、health 等需明确分类。

11. Frontend search continuation stale 没有 snapshot-safe recovery

Conversation list append 收到 search_continuation_stale 当前只走 generic error，旧 items/continuation/diagnostics 留在 UI。

Message-hit lazy pagination收到 stale 时当前把 hitHasMore=false，旧 hitItems 保留，用户看到的效果相当于“没有更多结果”，而不是“snapshot 失效”。

必须统一 recoverable search continuation helper：stale 时清空旧 snapshot 的 items/token/provisional/total/order metadata，在当前 query/filter/path/sort/request generation 下从 page 0 仅重试一次；第二次 stale 显式报数据变化，不得无限 retry，也不得 old+new append。

12. Search continuation token 文本表示不是 canonical Base64url

Display cursor 已有 decode 后 re-encode equality check，search continuation 没有。修改 Base64 最后字符的 unused padding bits，可产生多个不同 token text 映射到相同 signed bytes，HMAC 仍通过。

这不是签名绕过，但违反 token canonicality。应统一 signed-token codec，非 canonical unpadded URL-safe Base64 文本稳定返回 continuation invalid，而不是 stale。

13. Global effective-current 资源上限检查发生在 TEMP scope 全量写入之后

global mode 先：INSERT INTO effective_current_scope SELECT conversation_id FROM conversations再 count/size 并检查 100k conversation 等限制。

1m conversations synthetic DB 最终会正确 bounded-reject，但拒绝前已经把 1m rows 物化进 TEMP，约 33 MiB，wall约0.59 s。到更大库，拒绝成本仍随全库增长。

应在 materialization 前用 LIMIT max+1、cheap indexed count或等价 early guard，避免先消耗本来想防止的资源。

14. Real-pipeline acceptance harness 不会因为 performance threshold 失败而非零退出

当前 harness aggregate 不聚合 job.elapsed_seconds/stage median-worst，也没有正式 performance_pass。进程退出码只看 sample success，因此 synthetic success=true, wall/job=579s 仍可 exit 0。

必须把 correctness 与 performance 分开，正式模式有明确 300 s job threshold，任一正式 run 超过阈值必须 performance false，并在 strict/正式模式非零退出。不得修改门槛来制造绿色。

历史上绑定其他最终 artifact 的大文件数字只能作为历史测量，不能自动归给当前最终交付文件。

15. Real-pipeline cleanup failure 可 false green

_run_once() 在 workspace cleanup 之前先 metrics["success"]=True。finally 中 shutil.rmtree(workspace) 的 OSError 被吞掉，只更新 cleanup.complete，main 仍只根据 sample success 决定退出码。

因此 pipeline成功但 cleanup PermissionError/残留 workspace 可以 exit 0。

必须分 pipeline_success、cleanup_success，最终 acceptance success 必须包含 cleanup。已有 primary error时 cleanup error只能附加，不能覆盖 primary failure。

16. 当前 multi-GiB scale fixture 主要证明 invalid/skipped streaming，不代表 valid-data end-to-end

shipped logical archive worker 的大 element 是：{"synthetic_scale_index": ..., "padding":"xxxx..."}

它们不是合法 ChatGPT conversation，并且 harness 明确要求这些大 element 全部被 skip，最终只 commit一个 tiny valid conversation。

因此该 fixture可以证明 ZIP64、inflate、streaming、framing、element tolerance、warning aggregation、cleanup，却不能证明 multi-GiB valid canonical insert、core FTS、WAL、optional normalized/trigram staging、publish和真实DB增长。

必须保留该 resource-stress fixture，但改成准确语义名称和 diagnostics；另外新增 valid-many-small、valid-mixed、high-selected-json-ratio、low/high-compression valid、many-shards/single-shard 等 opt-in规模验收。不得把全 skipped logical bytes写成5/10 GiB valid-data pipeline passed。

17. CLI internal wall timer 与 OS subprocess wall 的固定绝对差测试不可移植

cli_controlled_wall_seconds 无法覆盖 interpreter startup和完整process teardown。当前固定 350 ms 差值合同在另一环境真实失败。

应清楚区分 internal controlled wall、pipeline stage wall、external process wall。测试应验证有限/非负、stage不超过controlled、external不小于controlled等可移植关系。完整process wall由外部 harness负责。

18. scale performance harness 的 tracemalloc 会严重污染 wall-time

同一 mapping-predecode workload，开启 tracemalloc时100k/500k/1m约4.5/22/>120 s；独立 subprocess 无 tracing约0.19/0.98/1.90 s。

不能删除 tracemalloc coverage，但正式性能 wall/RSS acceptance必须使用独立无 tracing subprocess；tracemalloc作为单独 diagnostic run，不能把 tracing overhead归罪生产 parser。

19. Visible multi-message copy 缺少跨行统一 snapshot binding

“复制整个 conversation”使用server-side单请求流式copy，snapshot一致；但“复制当前可见消息”会遍历 visible rows，并对每个被截断message分别发 display chunk HTTP requests。

每个message cursor只绑定该row revision，不存在一次可见集合copy的conversation-level/shared snapshot。writer在message A完成后修改message B，B随后可从新snapshot读取，最终clipboard可混合两个数据库状态。

需要 deterministic writer barrier验证。若确认，优先复用或扩展 server-side snapshot copy endpoint，不要让前端通过多个独立row cursors拼接成“逻辑同一时刻”的文本。

20. Clipboard write 后才验证 UI context，存在不可撤销 stale-copy窗口

copyText() 当前先：await navigator.clipboard.writeText(text)之后才检查 reader context/request generation 是否仍匹配。

如果 write promise pending时用户切换conversation，旧文本可能已经进入系统clipboard；代码只能避免显示“copied”，无法撤销 side effect。

应增加 deferred clipboard DOM test并收窄 side effect 时机。不要为了修复破坏当前display cursor安全重试和copy size limits。

21. Dependency pins 包含当前已公开修复的安全问题

当前 pin：

starlette==1.0.1，项目实际使用 StaticFiles(...)。CVE-2026-48818 影响 Starlette 1.0.1及之前Windows StaticFiles，1.1.0修复。

python-multipart==0.0.28。2026年公开的多个上游问题在0.0.30/0.0.31修复，包括 RFC2231/5987 Content-Disposition parameter differential、querystring parser differential/CPU问题，以及 negative Content-Length parse_form() read-until-EOF。

当前 UploadIngress 已经对现有 upload route 的 negative Content-Length 做额外canonical validation，所以不能夸大为当前route可直接利用该特定vector；但供应链仍应升级到兼容的安全版本，并重新生成 constraints和macOS arm64 hash lock。

22. JSON README decoder调用次数描述过强

Hybrid parser允许 common path一次成功decode；跨window元素可能先有一次 bounded failed probe，再进入framer后成功decode。文档应描述“一次成功完整decode，可能外加一次有界失败probe”，不能写成所有元素总共绝对只调用decoder一次。

23. Hash-lock verifier 文档容易把结构验证误解为artifact digest重新验证

verify_web_hash_lock.py --check-installed能检查lock结构、expected package set、平台和installed versions，但不能从installed distribution证明当初wheel artifact digest。

真正artifact hash enforcement发生在 pip install --require-hashes --only-binary=:all:。README、AGENTS和验收报告必须准确区分。

24. Final handoff provenance 必须绑定用户实际收到的最终 ZIP

历史修复报告出现过最终报告 outer hash 与用户实际收到文件不一致。当前上传 ZIP自身 deterministic/reproducible并没有问题；问题在handoff流程。

最终所有commit、dist、manifest、release、复制/重命名完成后，必须重新打开最终交付路径计算 outer SHA-256、size、manifest SHA、payload/member count，并fresh rebuild byte-identical后再次回到同一最终路径重算。不得复用中间A/B artifact hash。

25. MessageBlock 有一个未保存handle的 one-shot requestAnimationFrame

大多数 timer/listener/ResizeObserver/rAF都对称cleanup，但 MessageBlock 有一个命中后scroll的 one-shot requestAnimationFrame 未保留handle/cancel。当前callback只通过ref查DOM，风险较低，但应清理成一致的生命周期实现。

需要目标机器重新验证、不能直接当作当前算法bug的项目

A. 真实大文件完整 pipeline 性能

历史报告的真实大文件数据绑定另一个最终交付hash，不能转移为当前artifact证据。当前 harness 又缺performance gate，所以必须用最终实际工作树、fresh DB、完整HTTP pipeline重新运行至少三次并保留全部run。

如果内部正式门槛仍为300秒，则任一正式run超过300秒都不能写performance passed。

B. 1m full-trigram Web-index tail latency

独立Linux审计环境中100k/500k表现良好，但1m full-trigram builder出现过>180至300秒long-tail；关闭trigram时1m约22秒，说明resolver/normalizer/stable mapping不是普遍O(n²)。该现象可能与SQLite FTS/WAL/checkpoint/fsync/container filesystem有关。

目标Mac必须以独立 subprocess、真实stage progress、WAL/staging/checkpoint/fsync指标重测。未复现前不要盲改FTS merge参数。

C. Physical 5/10 GiB 与 >10 GiB

当前独立审计真正跑过1 GiB physical stored ZIP并近似线性，但没有真正运行5 GiB、10 GiB physical或>10 GiB stream。不能用ZIP64支持或invalid logical workload代替。

Must-preserve regression

不得为了以上修复回退以下已确认正确方向：

schema6及独立query generation；valid current_node优先；finite effective-current；visible-only reader pagination和邻域读取；single SQLite read snapshot；row-local display revision；stable optional integer address且不依赖canonical implicit rowid；VACUUM/rowid变化后Web-index recall；fixed-size signed server-side search continuation与16KiB ID支持；signed canonical display cursor与从offset0安全copy restart；no-op reimport保留generation/current optional index；placeholder shared streaming classifier；hybrid JSON many-small fast path；large/spanning element近似线性；mapping/array/depth/heap predecode budgets；receive-level upload body cap与writer admission发生在multipart parser前；unsafe write统一Origin/Fetch Metadata防护；Windows writer任何副作用前fail-closed；delete-input永久fail-closed；old live Web-index在publish前可读；lease/owner/staging/generation recheck/atomic publish/cancel；TEMP verified-result复用；actual source match span/byte anchor；UTF-16映射；AbortController/timer/listener/ResizeObserver主要cleanup；fallback安全textContent；atomic dist与deterministic release；authoritative manifest；cleaner用户数据保护。

外部官方资料重新核对

Starlette官方文档确认 UploadFile.file 是 SpooledTemporaryFile，支持“route-level disk check已经晚于framework spool”的结构判断。

MDN Fetch Metadata文档明确把 Sec-Fetch-Site 等header用于resource isolation，并区分same-origin、cross-site、none，支持只拒绝明确cross-site昂贵资源、同时保留直接导航和无header API client的设计。

Starlette官方release notes与NVD确认Windows StaticFiles问题在1.1.0修复。

NVD与python-multipart changelog确认当前0.0.28低于相关0.0.30/0.0.31修复版本。

pip官方secure installs文档确认hash checking是all-or-nothing，所有dependency必须pin+hash，--only-binary :all:可用于阻止sdist。

不应重新报告为当前bug

没有重新发现source/timestamp search continuation false exact；没有重新发现persistent optional index identity依赖implicit rowid；没有重新发现普通delete-input不可逆unlink路径；没有重新发现Windows pathname-only writer fallback；没有重新发现C1/noncharacter策略分裂；没有重新发现leading-whitespace placeholder index前后recall差异；没有重新发现display cursor未签名或16KiB ID进入token；没有重新发现no-op reimport推进全部generation；没有重新发现JSON common path旧式每chunk从element起点重复decode；当前release自身deterministic A/B/C重建仍成立。

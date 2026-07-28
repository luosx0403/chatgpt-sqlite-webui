# ChatGPT Export Archiver

言語: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

公式 ChatGPT エクスポート ZIP を、プライベートで検索可能な SQLite ナレッジアーカイブに変換します。

`Local-first` · `SQLite` · `Privacy-first` · `Fast import` · `Fast Web index` · `Chat-style Web UI` · `Markdown/TXT export`

ChatGPT Export Archiver は、OpenAI / ChatGPT の公式 export ZIP を直接 SQLite に取り込み、アーカイブを検証し、検索インデックスを作成し、ローカル Web UI を開き、会話を Markdown または TXT にエクスポートします。長期的な個人アーカイブ、オフライン検索、ナレッジベース移行、そして公式履歴 UI では扱いにくいローカルファイルとインデックスのワークフロー向けです。

## 使う理由

- **ローカル優先でプライベート。** ZIP、データベース、エクスポート、一時アップロードコピー、Web UI、ログは、自分で移動しない限り手元のマシンに残ります。
- **ZIP を直接インポート。** 公式 ChatGPT エクスポート ZIP を、手動展開や shard 結合なしで読み込めます。
- **大きなアーカイブに対応。** 推奨インポート経路は大きな ZIP、増分インポート、遅延 FTS 再構築、最適化された任意 Web 検索インデックスを想定しています。
- **チャット形式のリーダー。** Web UI は既定で ChatGPT に近い表示になり、user は右、assistant は左、system/internal は既定で折りたたまれ、必要に応じて展開できます。
- **従来の技術ビューも保持。** Settings、または `?layout=classic` / `?messageLayout=classic` で、以前の行単位レイアウトに戻せます。
- **アーカイブ向け検索。** 長期アーカイブとローカル検索では、SQLite 検索により公式履歴 UI より細かく制御できます。role/title/source/scope/exclude、フレーズ、OR、ページング、verify、再構築可能なインデックス、エクスポートを利用できます。
- **移行しやすいエクスポート。** Markdown と TXT は決定的に生成され、バックアップ、ローカルナレッジベース、オフライン grep、移行に使いやすい形式です。

## スクリーンショット

安全なスクリーンショットは準備中です。スクリーンショットには synthetic な会話だけを使い、実際のタイトル、snippet、raw JSON、メールアドレス、ローカルパスを含めないでください。

## ローカル Smoke 観測

以下は 1 台のローカルマシンでの例であり、一般的な保証ではありません。

- 約 2.25 GB の実エクスポート ZIP は、大規模アーカイブ経路で約 98 秒でインポートされました。
- そのアーカイブの `verify` は約 4 秒で完了しました。
- より大きな増分アーカイブ後の任意 Web インデックス再構築は約 106 秒で完了しました。
- ローカル Uvicorn Web アプリでの高ヒットメッセージ検索は約 0.3 秒で返りました。

## このプロジェクトでできること

- OpenAI / ChatGPT のエクスポート ZIP、単体の `conversations.json` ファイル、または展開済みのエクスポートディレクトリから `conversations.json` と sharded `conversations-*.json` を SQLite に取り込みます。
- 会話メタデータ、mapping nodes、メッセージの role、本文テキスト、タイムスタンプ、親ノード関係、ソース追跡、インポート警告を保存します。
- 増分インポートに対応します。新しいエクスポートを同じデータベースへ再インポートすると、変更された会話を更新し、未変更データを意図的に重複させません。
- CLI 検索用に任意の FTS5 メッセージインデックスを作成します。
- ブラウザー検索を高速化する任意の Web 部分文字列インデックスを作成します。
- Markdown、TXT、またはその両方にエクスポートできます。
- メッセージ本文を表示しない `verify`、`stats`、プライバシーに配慮した `inspect` を提供します。
- 既存データベースがなくても起動できるローカル Web UI を提供し、ブラウザーから ZIP を選んでインポートできます。
- ログを構造化されたコマンド出力から分離し、タイトル、snippet、raw JSON、メッセージ本文を記録しません。

## プライバシー

すべての処理はローカルで実行されます。データベース、生成されたエクスポート、アップロード時の一時コピー、Web UI、ログは、あなた自身が移動または公開しない限り手元のマシンに残ります。CLI は意図的に、本文の断片ではなく ID、件数、タイムスタンプ、状態行を表示します。CLI summary とログには、会話本文、タイトル、snippet、raw JSON、完全な入力/出力パス、実際の ZIP ファイル名は出力されません。インポート summary は `source zip` のように入力種別だけを表示します。Web UI はローカル利用を想定しており、既定では `127.0.0.1` にバインドします。

インポート summary の `valid_conversations` は、重複 ID の統合前に解析を通過した入力 conversation 要素数です。重複 ID が統合される場合、最終的なデータベース変更件数である `inserted_conversations`、`updated_conversations`、`unchanged_conversations` より大きくなることがあります。

`inspect` と scanner のエラーは、既定では実際の ZIP 名や完全なパスを表示しません。`verify`、`stats`、`search`、`export` など既存データベースを必要とする CLI コマンドは、データベースパスが間違っている場合に `database_not_found` を報告し、空の SQLite ファイルを作成しません。Web 検索は、利用可能な場合に任意の trigram インデックスを候補取得レイヤーとして使い、その後も正規化済み部分文字列フィルターを適用するため、短いクエリ、記号、trigram 非対応のケースは安全にフォールバックします。

`--delete-input-on-success` は fail-closed の互換 option です。現在の Python/OS primitive では最終検証前に同じ inode を開いた非協調 writer を排除できないため、production command は DB、journal、staging、rename の前に `delete_input_secure_identity_unsupported` を返します。通常 import は元入力を削除せず利用でき、`recover-delete-input` は過去の supported run が残した project-owned journal 専用です。

それでも、データベースやエクスポートされた Markdown / TXT には個人的な会話内容が含まれる可能性があります。`archive/*.db`、エクスポート済みファイル、元の ChatGPT エクスポート ZIP は機密データとして扱ってください。

## 必要条件

- Python 3.10 以降。Web 依存関係の再現可能インストールは Python 3.12 で検証しています。
- JSON 対応 SQLite。FTS5 は任意で、利用不能または `message_fts` がない場合も安全な scan 検索を使えます。
- React Web UI を再ビルドしたりフロントエンド検査を実行したりする場合のみ、Node.js と npm が必要です。runnable 配布には `webui/dist` が含まれるため、通常のローカル Web UI 利用ではフロントエンドの再ビルドは不要です。
- コア CLI は Python 標準ライブラリだけで動作します。ZIP upload を含む Web UI を使う場合は `requirements-web.txt` をインストールしてください。この profile がなければ `web` command は install hint とともに即時失敗します。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

Windows PowerShell:

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

Windows cmd.exe:

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py312.txt
```

Python 3.12 constraints ファイルは解決済み Web 依存関係の全バージョンを固定しますが、クロスプラットフォームの hash lock ではありません。将来のリリース手順では、対応する Python/OS マトリクスごとの hash を生成・検証する必要があります。それまでは信頼できる package index のみを利用し、npm lockfile と audit を別個のフロントエンド制御として維持してください。

## クイックスタート

ChatGPT エクスポート ZIP をリポジトリの外に置き、最速で安全なインポートコマンドを実行します。このコマンドは入力ハッシュ計算を省略し、最後に FTS を一度だけ再構築します。大きなアーカイブでは、行ごとに FTS を保守するよりかなり高速です。

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows PowerShell での同等の書き方:

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows cmd.exe での同等の書き方:

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

ローカル Web UI を起動します。

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

データベースがまだ存在しない場合でも、Web UI は起動し、空状態とインポートパネルを表示します。ブラウザーで ChatGPT エクスポート ZIP を選ぶと、バックエンドがローカルの一時コピーへ書き込み、インポート後に `verify`、`stats`、`web-index` を自動実行します。

```bash
python chatgpt_archive.py web --port 8787
```

## よく使う CLI ワークフロー

チャット内容を表示せずにエクスポートを検査します。

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

空のデータベースを明示的に作成します。

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

大規模アーカイブ向けの経路でインポートします。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

`--input` には公式エクスポート ZIP、単体の `conversations.json`、または展開済みのエクスポートディレクトリを指定できます。展開済みディレクトリには `conversations.json`、または sharded `conversations-*.json` ファイルを含められます。shard を手作業で結合しないでください。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
```

構造上の整合性を確認します。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

構造化された件数と期間境界を表示します。

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

CLI 検索経路でメッセージ本文を検索します。表示されるのは conversation ID、node ID、role で、snippet は表示されません。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

会話を Markdown、TXT、または同じ実行内で両方の形式としてエクスポートします。`--format md` は Markdown 本文ファイルを書き出して manifest を更新し、`--format txt` は plain text 本文ファイルを書き出して manifest を更新し、`--format all` は両方の本文形式を書き出して manifest を更新します。

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

日付範囲を指定し、必要なら既存ファイルを書き直します。`--from` と `--to` の日付境界は `YYYY-MM-DD` だけを受け付けます。

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

エクスポート summary は本文ファイルの件数を示します。`written` は最終バイト列が変わった Markdown/TXT 本文ファイル数、`skipped_unchanged` は変更のなかった Markdown/TXT 本文ファイル数です。manifest は必要に応じて更新されますが、この 2 つの件数には含まれません。

CLI と Web のエクスポートは、有効な現在パスと表示メッセージのみを既定にします。分岐や内部メッセージを含める場合は `--path all` と `--include-internal` を明示し、manifest にも両方の選択を記録します。CLI は会話ノードを上限付きバッチで読み、Web ダウンロードと`現在のパスの会話をコピー`は専用の上限付きサーバー側テキストストリームを使います。完全な標準テキストと対象となる legacy/raw 復元テキストは reader 応答予算により切り詰められません。

任意の Web 検索インデックスを再構築します。

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

`web-index` は明示的な段階で message/title を正規化し、対応時に trigram index を構築します。build ごとに予測不能な staging 名と永続 owner-token lease を使い、2 個目の build は `web_index_build_in_progress` で拒否され、stale cleanup も厳密な所有権を検証します。全段階は上限付き keyset と個別の input/normalized/derived/FTS-bind byte budget を使い、実測 peak を報告します。writer lock は batch 間で解放され、最後の短い `BEGIN IMMEDIATE` transaction で canonical generation、object ownership、metadata を再確認して atomic publish します。それまでは reader が旧 index を参照します。generation change、interrupt、disk error、cancel は旧 index を維持し、その lease が所有する object だけを cleanup します。`POST /api/import/jobs/{job_id}/web-index/cancel` は import job の index stage にのみ適用されます。

Web UI を起動します。

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## インポートモード

大規模アーカイブでは次のコマンドを推奨します。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

入力は ZIP、単体の `conversations.json`、または `conversations.json` か sharded `conversations-*.json` ファイルを含む展開済みディレクトリにできます。scanner discovery は `__MACOSX`、AppleDouble `._*` ファイル、`.DS_Store` などの macOS metadata paths を無視するため、これらのローカル artifact が conversation source になることはありません。

ディレクトリ入力では、POSIX プラットフォームは利用可能な場合に各 component を `dir_fd` と `O_NOFOLLOW` で開きます。portable fallback は symlink/reparse component を拒否し、パスによる open の直前に containment を検証しますが、Python 標準ライブラリだけでは全プラットフォームのローカル replacement race を完全には排除できません。信頼できないローカルユーザーや並行プロセスが変更できる展開済みディレクトリをインポートせず、この脅威モデルでは元の読み取り専用 ZIP を使用してください。

インポート後に SQLite の planner statistics と FTS インデックスをさらに整理したい場合は、次を使います。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` は既定で無効で、現在は全 platform で unsupported です。指定時は staging/import 前に `delete_input_secure_identity_unsupported` で失敗し、race のある unlink へ downgrade しません。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

増分インポートは通常の使い方です。新しいエクスポートを同じデータベースへ入れると、変更済みの会話だけが更新され、残りのアーカイブは保持されます。

## Web UI ワークフロー

Web UI は FastAPI が配信するローカル React アプリです。推奨される使い方は、runnable tree に含まれるビルド済みの `webui/dist` をそのまま配信することです。

```bash
python chatgpt_archive.py web --port 8787
```

既定のリーダーレイアウトは `chat` です。user メッセージは右、assistant メッセージは左に配置され、system/internal メッセージは折りたたみ表示になります。以前の行単位の技術レイアウトを使うには、Settings で `Classic` を選ぶか、Web UI の URL に `?layout=classic` または `?messageLayout=classic` を追加してください。

すべての経路で同じ `path=current` effective-current 規則を使います。conversation に属する有効な `current_node` とその親 chain が raw flag 全ゼロでも最優先です。次に決定的な利用可能 `is_on_current_path=1` leaf chain を選び、どちらもなければその conversation だけ all に fallback します。raw flag は変更せず、応答は `current_node_exists`、`current_collection_source`、`current_path_fallback_to_all`、`effective_path`、各 node の effective visibility を返します。壊れた親や cycle は有限かつ決定的に診断されます。

global current-path search は、normalized message/title index と安全な source/date/role 条件から path 非依存の conversation candidate を先に求め、その candidate だけに effective-current membership を構築します。絞り込めない exclude-only query のみ明示的な full-database fallback を使います。Reader の hit navigation は初回に compact page を 1 回だけ取得し、読み込み済み境界へ近づいたときに追加します。Search と Web-index SQL は SQLite の `AS MATERIALIZED` を要求せず、portable な non-flattening query shape で各 legacy raw candidate を論理 stage ごとに最大 1 回だけ resolve します。

リーダーのコピーとエクスポートは、表示中のリーダー契約に従います。`現在のパスの会話をコピー` は現在の reader パス専用の完全テキストストリームを使い、Show internal messages の切り替えを尊重し、現在の検索フィルターは無視します。ブラウザーに reader ページを蓄積しません。`表示中をコピー` は、すでに読み込まれている表示メッセージだけをコピーします。ダウンロードリンクも同じ現在のパスと Show internal 設定を使います。Raw メッセージアクセスは、メッセージ単位 endpoint による上限付きの大きな raw プレビューです。切り詰められた応答では `raw_text` をプレーンなプレビューテキストとして描画し、UI はその capped preview だけを表示します。

reader が `around_node_id` でヒットへ移動する場合は、reader と同じページング集合を使います。Show internal がオフなら visible-only rows、Show internal がオンなら完全な node collection、current-path node がない壊れた conversation では effective all-node collection です。

データベースがある場合は明示的に指定するか、既定のパスを使えます。データベースがない場合でも Web UI を起動し、インポートパネルから ChatGPT エクスポート ZIP をアップロードできます。アップロードインポートは直列化され、同じプロセス内で同時に動く SQLite writer は 1 つだけです。

Web アップロードインポートが成功すると、バックエンドは CLI と同じコア import pipeline を使い、その後 `verify`、`stats`、`web-index` を実行します。アップロードされた ZIP はサーバー側の一時コピーであり、ディスク上の元ファイルとは独立して削除されます。

preflight failure と terminal import job は複数の `cleanup_warnings` を返す場合があります。React UI は各 safe warning code と `path_kind` を localized user text で表示し、deprecated single `cleanup_warning` fallback も維持します。一時 path、filename、OS message、error class detail は表示せず、polling は同じ job snapshot を置換するため warning を重複追加しません。

ビルド済み React アプリを提供できない場合の fallback HTML は、機能を限定した緊急 UI であり、完全な reader の代替ではありません。検索/reader 操作は少なく、download は明示指定しない限り internal node を除外します。完全な UI には `webui/dist` を再ビルドしてください。

## Web アップロードの安全制限

Web アップロードは、インポート job の開始前にアプリケーションレベルの安全制限を適用します。これらは環境変数で制御され、CLI の `import`（これらの制限を使用しません）とは独立しています。

Web アップロードは、ファイル読み取り前に pending slot を予約するため、大きなアップロードが別の writer と競合することはありません。その予約後のあらゆるエラー、たとえば一時アップロードパスの作成失敗では、slot を解放し、サーバー側の一時ディレクトリを削除する必要があります。成功した import job は slot と一時コピーを引き継ぎます。

process kill、OOM、または host crash は通常の cleanup を回避し、OS の一時ディレクトリに古い `chatgpt-archive-upload-*` ディレクトリを残す場合があります。本リリースは ownership/age の誤判定による無関係なデータ削除を避けるため、自動削除しません。サーバー停止後、管理者は正確な prefix、現在のアカウントの ownership、経過時間、symlink/reparse point がないこと、使用中 job がないことを確認した個別の古いディレクトリだけを削除できます。ユーザーの export ZIP や未確認 wildcard は絶対に削除しないでください。

Web UI が loopback アドレス（`127.0.0.1`、`localhost`、`::1`）にバインドされている場合、デフォルトで大規模な信頼できるアーカイブを許可します：

| 環境変数 | ローカルデフォルト | 制御対象 |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | 圧縮 ZIP アップロードの総サイズ |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | 単一 JSON member の最大非圧縮サイズ |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | conversation JSON member の最大数 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | 非圧縮 JSON データ総量の上限 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | 大規模 JSON member の最大圧縮率 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | ZIP 内の総 member 数の上限 |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | unset | 信頼できる非 loopback ネットワークでのみ `local` に設定し、未設定のアップロード制限にローカル既定値を使う |

**リモートバインドポリシー。** 非 loopback は `CHATGPT_ARCHIVE_ALLOW_REMOTE_ACCESS=true`、`CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true`、または `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local` による明示的 opt-in が必要です。remote-safe 既定値は 128 MiB ZIP、256 MiB/JSON member、512 MiB 総非圧縮、圧縮比 200.0、200 JSON members、10,000 ZIP members です。信頼済み loopback/local の圧縮比既定値は 1000.0 です。`ALLOW_REMOTE_UPLOADS` は明示設定した制限だけを緩和し、未設定値は remote-safe のままです。`REMOTE_UPLOAD_PROFILE=local` は未設定の全制限を local の大容量既定値へ戻すため、信頼済み LAN だけで使ってください。

`/api/schema` は multipart body 上限（ZIP byte 上限と有限の overhead）を含む有効な policy を報告します。writer slot と receive-level body cap は multipart parse 前に動作します。parser の spool と pipeline の server-side temporary ZIP が重なるため、圧縮コピー約 2 個分と DB 増加分の一時 disk を見込んでください。JSON decode、SQLite write、`web-index` は展開サイズに比例して RAM/disk/CPU を使います。remote upload は有効な `Content-Length` が必須で、loopback chunked upload も streaming cap で制限されます。

正規の大規模アーカイブのためにローカル制限を引き上げるには、Web UI 起動前に対応する変数を設定します：

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

信頼できる内部ネットワークで明示的な圧縮 ZIP 上限だけを上げ、他の未設定制限を remote-safe のままにするには：

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

信頼できる内部ネットワークで完全なローカルアップロード profile を使うには：

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

信頼できるローカルファイルにのみ、より高い制限を設定してください。値を大きくすると、ZIP bomb、ディスク圧迫、CPU/メモリのリスクが高まります。


## Web UI 受け入れチェックリスト

Web 経路を変更したとき、または runnable delivery を準備するときは、次を確認します。

- データベースなしで Web UI を起動し、空状態の契約どおりに表示されることを確認する。
- ブラウザーから小さな ChatGPT エクスポート ZIP をインポートし、job が完了することを確認する。
- アップロードインポート後にバックエンドが `verify`、`stats`、`web-index` を実行することを確認する。
- ページを更新し、会話を一覧表示して開けることを確認する。
- より新しい ZIP を再インポートし、増分経路が引き続き動くことを確認する。

runnable delivery の Web 経路は `webui/node_modules` を必要としないはずです。ビルド済みの React assets は `webui/dist` から配信されます。

## 検索構文

CLI 検索は安全な検索構文を使います。通常語は normalized substring `contains` で既定は AND、大文字 `OR` は選択肢を作ります。引用符は phrase を保ち、`-term`/`-"quoted phrase"` は除外です。`word` 境界は ASCII の文字・数字・underscore だけに適用し、CJK は保守的な normalized contains のままです。query 内の raw `path:`/`scope:` は UI selector より優先されます。

除外語は会話結果では conversation-level の意味です。選択中の検索 scope と path 内で、タイトルまたは任意のメッセージが除外 fragment に一致すると、その conversation は返されません。`/api/search/messages` は、除外 fragment を含まないメッセージヒットだけを返します。`path:current` は conversation ごとの reader パスに従います。壊れたアーカイブに current-path node が 1 つもない場合、current-path 検索は reader が表示する同じ all-node ビューへ fallback します。

日付 filter は UTC calendar day を使い、開始は `00:00:00Z` を含み、終了は翌日 `00:00:00Z` を排他的上限にします。CLI export の timestamp と deterministic filename date は UTC、browser 表示は browser local timezone です。Web 検索欄は 500 文字までです。virtual list の未 render 行は Cmd/Ctrl+F では見つからないため、archive search または conversation copy を使ってください。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web 検索は `web-index` が作成する任意の normalized trigram インデックスを使います。ブラウザーで実用的な部分文字列検索を行うためのものです。これらの任意インデックスが存在しない、または壊れている場合は再構築してください。

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

検索 diagnostics は best-effort の性能ヒントです。normalized trigram、normalized scan、normalized title scan、full scan など、normalized-safe な候補層または scan fallback だけを報告します。legacy raw FTS の存在は別項目として報告できますが、正規化等価テキストを見落とす可能性があるため、実際の candidate backend として表示してはいけません。

`VACUUM`、`VACUUM INTO` を手動で実行した場合、または外部の圧縮/バックアップツールで SQLite データベースを書き直した場合は、Web UI 検索に頼る前に `python chatgpt_archive.py web-index --db <archive.db>` をもう一度実行してください。任意 Web インデックスは正規の会話テーブルから安全に再構築でき、元の会話データは変更しません。

## 検証と任意 Web インデックス

`verify` は SQLite integrity とプロジェクト固有の整合性を確認します。missing current node、broken parent link、空の会話、親ノードの cycle も対象です。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

`message_fts_rebuildable` は定数ではなく実際の runtime capability です。FTS missing かつ FTS5 available なら `missing` と rebuildable true、FTS5 module unavailable なら `capability_unavailable` と rebuildable false、damaged table なら `damaged` を返し、rebuildability は同じ bounded runtime probe で決まります。その他の SQLite error は structured database error classifier に伝播します。

`PRAGMA integrity_check` が `web_message_trigram` または `web_title_trigram` の FTS5 inverted index 破損を報告した場合、コアの会話データは構造的に有効で、任意の Web 検索インデックスだけが壊れている可能性があります。その場合、`verify` は `optional_web_index_error true` と復旧ヒントを表示します。任意 Web インデックスは次で再構築します。

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

この診断は保守的です。すべての integrity-check エラーが任意 Web インデックステーブルまたはその FTS5 shadow tables に帰属できる場合だけ、任意 Web インデックス問題として扱います。

## ログ

ログレベルは `debug`、`info`、`warning`、`error`、`none` です。既定値は `warning` です。詳細なレベルほど、それより静かなレベルの内容も含みます。ログにはタイトル、snippet、raw JSON、メッセージ本文を含めません。

ログフラグはサブコマンドの前にも後にも置けます。

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

JSON ログは `logs/` のような ignore 済みの場所に置いてください。`*.jsonl` はローカルログ成果物として扱われ、delivery clean で拒否されます。

インポートの計測フィールドには `source_scan_seconds`、`parse_and_upsert_seconds`、`fts_rebuild_seconds`、`finalize_commit_seconds`、`close_seconds`、`legacy_pre_commit_seconds`、`wall_total_seconds`、`total_import_seconds` が含まれます。`total_import_seconds` は最終 commit と close を含むエンドツーエンドの wall time です。

インポートトランザクションが成功した後の summary update は best-effort です。`summary_update_after_commit_failed`、`import_connection_close_failed`、`summary_update_after_close_failed` は警告であり、成功済みのインポートを失敗扱いにはしません。

## 開発と受け入れ確認

Python のチェックを実行し、最初の delivery clean の前に安全な生成物を削除します。

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

Web UI をビルドし、smoke test を実行します。

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

`clean_generated_artifacts.py` はクロスプラットフォームで、`webui/dist` は保持します。安全に再生成できるファイルだけを削除し、データベース、ZIP、SQLite sidecar、`archive/`、`exports/`、`logs/` は削除しません。delivery clean がこれらの機密パスをまだ報告する場合は、プロジェクトルート外へ移動するか手動で削除してください。受け入れ確認コマンドでは `--fail-on-blocked` を使うため、機密ファイルが残っている場合は delivery flow がすぐに停止します。

Windows PowerShell または cmd では、空白を含む search query に二重引用符を使ってください。例: `"python sqlite"`、`"role:user path:current python -pandas"`。上記の Python、Web、Web index、typecheck、build、cleanup、delivery-check コマンドは、Python と Node が `PATH` にあれば macOS、Windows、Linux で使えます。Windows で Python launcher を使う場合は、`py -3 tools/clean_generated_artifacts.py --fail-on-blocked` で cleanup helper を実行できます。

ZIP 配布物を確認する場合:

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## 配布時の注意

runnable delivery には Python ソース、テスト、ドキュメント、`requirements-web.txt`、`constraints-web-py312.txt`、`webui/src` と `webui/tests` のフロントエンドソースとテスト、フロントエンド設定/package ファイル、`webui/dist` のビルド済み assets を含めます。`webui/node_modules`、`webui/tsconfig.tsbuildinfo`、Python cache ディレクトリや bytecode、coverage/typecheck cache、`.DS_Store`、AppleDouble `._*` ファイル、`__MACOSX`、`Thumbs.db`、`Desktop.ini`、`.gitignore.md`、一時ログ、ローカル受け入れログ、`*.log`、`*.ndjson`、`*.jsonl`、`archive/`、`exports/`、任意の `*.zip`、`conversations*.json`、`*.db`、`*.sqlite`、`*.sqlite3` などの実データベースファイル、または `*.db-journal`、`*.sqlite-wal`、`*.sqlite-shm`、`*.sqlite-journal`、`*.sqlite3-wal`、`*.sqlite3-shm`、`*.sqlite3-journal` などの SQLite sidecar は含めないでください。ディレクトリ検査では対象ルート直下の `.git` は許可されるため通常の Git clone をそのまま検査できますが、入れ子の `.git` は失敗します。ZIP delivery ではどの `.git` エントリも失敗します。

source-only delivery では `webui/dist` を省略できますが、その場合は完全な React UI を配信する前にフロントエンドを再ビルドする必要があります。

## ソースツリー案内

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

## データベース概要

メインデータベースは conversations、mapping nodes、import runs、warnings を保存します。message object だけが raw message JSON object を保持し、conversation と mapping-node object は正規化され、byte-for-byte 保存ではありません。入力 ZIP SHA-256 は任意で、`source_files`/`file_index` の entry SHA 列は予約済みですが現在は未設定です。CLI FTS テーブルは `message_fts` です。任意 Web 検索用の補助テーブルには `web_message_norm`、`web_title_norm`、`web_message_trigram`、`web_title_trigram` と SQLite FTS5 shadow tables が含まれます。

canonical DB は `PRAGMA user_version` の version 5 です。version 3 は `NOT NULL` identity、version 4 は field-scoped address/graph revision、version 5 は durable row-local display revision と compatibility state を追加します。Migration は write lock 前に DB/WAL/journal/TEMP 容量を保守的に preflight し、同じ transaction で row/trigger を導入して content-free progress を報告します。cancel、interrupt、ENOSPC、SQLite failure は完全 rollback します。readonly path は migration DDL を実行せず、旧 compatible DB は `database_migration_required` を返します。

Health と `verify` は任意の `message_fts` の欠落と破損を区別します。破損時は `optional_message_fts_error` と `--rebuild-fts` の復旧ヒントを返します。一般的な malformed、locked、readonly、I/O、SQL runtime failure は能力欠落として扱わず、`database_malformed`、`database_locked`、`database_readonly`、`database_io_error`、`database_runtime_failure` を使います。

## Round 10 のリソース・所有権・復旧契約

managed FTS、任意 Web index、staging、metadata、generation、shadow object は所有権が完全一致した場合だけ破壊的 DDL の対象です。任意 Web index format 6 は logical conversation/node identity に対応する project-owned stable integer address を使い、durable/cross-request object は canonical implicit rowid に依存しません。address-map、resolver/normalization、generation、family ownership が currentness を決めます。placeholder classifier は BLOB chunk をまたいで任意の ASCII/Unicode leading whitespace と marker 全体を streaming 処理し、fixed prefix に依存しません。

長い display cursor は target message row に直接保存される durable な行別 revision に結び付けられます。managed insert/update trigger は表示に影響するすべての write で revision を進めるため、直接の外部 SQLite writer が `content_hash` を更新しなくても stale cursor を検出し、無関係な行の write は cursor を無効化しません。pre-version-5 database は migration-required gate となり、明示的 writer migration が revision を backfill します。cursor は stable row identity digest にも bind し、rowid 再利用で旧 cursor が復活するのを防ぎます。

exact search は canonical BLOB を 64 KiB chunk で読み、connection-local TEMP artifact を再利用します。固定小サイズ signed continuation は bounded server-instance session を参照し、long ID を埋め込みません。restart、expiry、secret change、affinity のない cross-worker、index/metadata/generation change、DB replacement は invalid/stale になります。continuation がある場合 `next_offset=null` です。`total_exact` は count のみ、`order_exact`、`scan_complete`、`provisional_order` は global order/scan の真偽を独立表示し、UI は provisional segment を final として黙って混在させません。

新規 import の 1 conversation element は joint profile（UTF-8/decoded 各 32 MiB、string/primitive token 2,500,000、mapping entry 1,250,000、array item 1,000,000、depth 256、integer 1,000 digits、estimated decoded heap 384 MiB、mapping node 5,000、bounded import-batch）をすべて満たす必要があります。どれかを超える element は structured warning で skip し、後続の valid element は継続します。single-pass framer は boundary を一度見つけて C decoder を一度だけ呼びます。

project bulk import は同じ write-lock transaction 内で完全一致する project-owned generation trigger を一時置換し、dirty field domain ごとに 1 回だけ generation を進めて trigger を復元・検証します。rollback/crash は旧 DDL/data を復元し、external writer は通常の statement trigger を使います。単一 100,000-node legacy conversation を含む finite effective-current scope は SQLite TEMP relation で正確に比較し、raw-flag cycle traversal は重複した Python string graph ではなく compact integer array を使います。archive export は plan と node を temporary SQLite に spool し、conversation/node を keyset stream するため、archive 全体の Python node graph を保持しません。

strict `--delete-input-on-success` は全 supported platform で staging 前に拒否されます。path identity と advisory lock は pre-open writer を止められません。過去の identity-bound journal は CLI の bounded token で回復でき、replacement を上書きしません。Web constraints は Starlette 1.0.1 など resolved version を固定しますが cross-platform hash lock ではありません。

## Round 11 の stable identity・outcome・performance 契約

writer process lock は path を含まない magic/version/database identity record を private per-user registry に置きます。既存 DB は normalized path と file entity の両方を lock するため、real/relative/`..`、file/parent symlink、hardlink alias は lease/staging/write 前に競合します。新 DB は作成直後に entity bind し、publish 前に再検証します。未知 registry object は変更・削除せず fail closed です。

完全に同じ reimport は node rewrite、generation advance、optional index invalidation、有意な WAL を発生させません。Web job の `completion_outcome`/`canonical_import_outcome` は success、warning/partial、commit 前後 failure、cleanup warning、cancel を区別し、UI は committed conversation/node、skipped element、warning 集計、canonical commit、optional-index 結果だけを安全に表示します。

`tools/benchmark_round11.py` は opt-in synthetic JSON harness で、environment/fixture hash、wall/CPU/RSS と該当する DB/WAL/TEMP/index、SQL/VM、BLOB、decoder、resolver、normalizer、lock、cleanup counter を出力します。large run は独立 subprocess で繰り返し median/worst を比較します。

## 既知の制限

- これはローカルアーカイブツールであり、クラウド同期サービスではありません。
- Web UI はローカル利用を想定しています。独自のアクセス制御なしに信頼できないネットワークへ公開しないでください。
- エクスポート解析は、現在確認されている OpenAI / ChatGPT のエクスポート形式に従います。上流の形式が変わった場合は、新しいインポート経路を信頼する前に `inspect` とテストを更新してください。
- エクスポートファイル名の部品は Windows と Unix 系の両方に向けてサニタイズされます。`CON`、`AUX`、`COM1`、`LPT9`、`COM¹`、`LPT²` などの予約デバイス名、末尾のドットや空白も対象です。
- 非常に大きなアーカイブでは、インポート、FTS 再構築、Web trigram インデックス作成に時間がかかることがあります。大規模インポートでは `--rebuild-fts` 経路を優先してください。

## セキュリティとレスポンス契約

アップロード入口は `Origin`、`Content-Length`、`Sec-Fetch-Site` をそれぞれ 1 値だけ受け付けます。Origin は userinfo、path、query、fragment、制御文字、カンマチェーンを含まない単一の HTTP(S) origin、Content-Length は正規形の非負 ASCII 10 進整数でなければなりません。重複または不正な安全ヘッダーは multipart 解析前に拒否し、無効または非有限の圧縮率設定は有限な安全 profile 既定値へ戻します。

Loopback Web が受け入れる Host は `localhost`、`127.0.0.1`、`::1`、明示した loopback bind host、および明示設定した Host だけです。非 loopback bind では実際の browser hostname/LAN IP を `CHATGPT_ARCHIVE_ALLOWED_HOSTS`（または `--allowed-hosts`）で指定し、`*` は拒否されます。`CHATGPT_ARCHIVE_TRUSTED_PROXIES`（または `--trusted-proxies`）は厳格な単一 edge proxy モデルです。未信頼 peer の forwarded header は無視し、信頼済み direct edge は client 値を上書きする必要があります。重複 Host/Forwarded、カンマ区切り chain、不正構文、`Forwarded` と `X-Forwarded-Host/Proto` の競合は拒否されます。全リクエストで Host を検証し、remote write は same-origin `Origin` が必要です。Origin のない client を許容するのは信頼済み loopback profile だけで、upload は常に `Sec-Fetch-Site: cross-site` を拒否します。

失敗 stage には source read も含み、`upload_preflight_failed`、`input_source_open_failed`、`input_source_not_regular_file`、`source_read_failed`、`source_changed_during_read`、`invalid_conversation_encoding`、`json_integer_too_large` を安定 code として返します。cleanup は構造化 `cleanup_warnings` 配列で、旧 `cleanup_warning` は先頭項です。

upload、canonical import、schema migration、optional Web index rebuild は capacity preflight と 256 MiB reserve を使います。ENOSPC は `upload_disk_space_insufficient`、`import_disk_space_insufficient`、`migration_disk_space_insufficient`、`web_index_disk_space_insufficient` を返し、import/migration は rollback します。

単独 JSON、directory、ZIP は同じ single-pass framer と一つの transaction を使います。framer は element boundary を一度走査して C decoder を一度呼び、上記 joint profile を適用します。query-based `/api/by-id/*` は legacy ID を 16 Ki characters まで扱い、固定小サイズ continuation は ID を埋め込みません。

descriptor-bound stat/hash/read で file identity を検証します。strict delete は staging 前に拒否し、recovery は historical project-owned journal のみ扱います。Migration は定義が完全一致する既知 predecessor のみ受け入れます。

非標準 JSON `NaN` / `Infinity`（`1e9999` のような overflow する標準数値を含む）は拒否され、無効な timestamp は内容を含まない warning とともに `NULL` になります。既定 message API は reader budget で制限された `display_text` を一つだけ返し、truncation/total-exactness metadata で完全復元の可否を示し、`content_text`/`render_text` を重複しません。通常の CLI/Web read と既定 `/api/health` は bounded schema gate を使い、`foreign_key_check` を実行しません。`verify` と `/api/health?deep=true` は完全で正確な検査と freshness fields を返します。複数 statement の CLI/Web logical read は schema/capability probe 前に一つの SQLite read snapshot を開始し、stream の完了・失敗時に解放します。Effective-current、pagination、around-node の意味は維持されます。

「URL をコピー」は `match_mode`、`layout`、`show_internal` と共有可能な search/reader state を必ず明示します。URL の明示値は `localStorage` より優先され、欠落値だけがローカル設定を使います。本版は `replaceState` を使い、ブラウザーの戻る/進むで段階的な検索・選択履歴を復元しません。

Release ZIP は固定 member metadata で byte-reproducible に生成し、全 payload manifest と member hash を検証してから原子的に置換します。失敗時は既存 release を変更しません。

Rollback summary は `attempted_*` とゼロの `committed_*` を分離します。failed run は新しい接続で永続化し、secondary persistence failure も明示します。pre-job cleanup failure は primary HTTP code を保持し、安全な `cleanup_warning`/`cleanup_error_type` を追加します。job ID は小文字 32 桁 hex のみです。

JSON は `NaN`/`Infinity` と `1e9999` のような overflow を拒否します。invalid timestamp は `NULL` と型だけの warning になります。通常の CLI/Web read と既定の `/api/health` は bounded schema gate のみを実行し、`foreign_key_check` は行いません。`verify` と `/api/health?deep=true` が全 database check を stream 処理します。total は exact、sample は bounded で、mode、完了時刻、generation、stale fields が契約を示しますが、CPU/VM work は database size に比例します。

長い表示本文は revision-bound opaque cursor と SQLite incremental BLOB read を使います。numeric offset の compatibility scan は 1,048,576 文字までで、それ以降は cursor が必要です。raw preview は NUL-safe な bounded BLOB query 1 回で、byte size と exact 状態を返します。visible NUL と孤立 surrogate は一貫して U+FFFD、raw JSON は安全な escape のままです。検索結果 scalar は明示的な表示 budget と truncation/元 length metadata を持ち、ID は切り詰めません。

CLI/Web conversation export は、materialize 前に 100,000 node、1 node の canonical/raw input 32 MiB、conversation input 合計 128 MiB を超える処理を拒否し、stream output は 256 MiB 上限です。effective-current の online materialization も conversation ごとに 100,000 node と graph ID input 128 MiB が上限です。CLI は同じ target directory の temporary file に分割書き込みし、hash と旧 file の streaming 比較を同時に行い、変更時だけ atomic replace します。browser copy は `ReadableStream` を使い、16 MiB UTF-8 または 8 Mi 文字を超えると clipboard write 前に中止して Download を案内し、partial text は書き込みません。

archive-wide export は conversation を 1 回 keyset scan して同じ output directory の temporary SQLite plan に置き、filename allocation、hash、JSONL/CSV manifest を stream 処理します。上限は 1,000,000 conversations、plan metadata 1 GiB、manifest ごと 2 GiB です。global effective-current は 100,000 conversations、1,000,000 nodes、graph input 512 MiB、推定 temp 1 GiB、batch 20,000 rows/nodes と 64 MiB input に制限されます。

message search page は常に `total_exact` を返し、scan 完了時は `count_total=false` でも true にできます。`order_exact`、`scan_complete`、`provisional_order` は order/scan を独立表示し、continuation がある場合 `next_offset=null` です。conversation search も同じ契約です。streaming placeholder classifier は任意 leading whitespace で reader/search/index/copy/export を一致させます。

filter-only/exclude-only は conversation を絞れますが、message hit、reader highlight、hit navigation には正の message text term が必要です。Copy URL は同じ applied search/list/selection context を使い、debounce 前の入力を古い selection と混ぜません。日本語とスペイン語 UI は partial translation と明示表示されます。release は collector とは独立した authoritative required-file list を先に検証し、必要な source/config/doc が欠ければ旧 ZIP を置換せず失敗します。

request validation response は最大 16 件の安全な item のみで、allowlist 済み `location`、`field`、stable public `code` だけを含みます。message-search candidate exact verifier、late snippet、enrichment、serialized response は bounded です。後続 candidate が request budget を使い切っても confirmed hit は捨てず、response は partial/pending を示し、scan 再開可能なら bound continuation を返します。hard per-row verifier limit 超過 candidate は false-exact miss ではなく明示的 pending のままです。Web index は read/normalize/FTS bind の byte を計測し、release payload は chunk stream で hash/write/verify します。

Python `zipfile` と本プロジェクトの import pipeline は ZIP64 構造をサポートし、小さな forced-ZIP64 member の回帰テストがあります。ただし通常の acceptance suite では物理サイズ 4 GiB 超の ZIP は生成していません。member、byte、compression ratio、disk、CPU の全上限は引き続き適用されます。

長時間の CLI/Web streaming export は、完了、失敗、または client disconnect まで一つの SQLite read snapshot を保持します。WAL mode では長い reader が checkpoint を遅らせ、concurrent writer により WAL が増える場合があります。duration、CPU/VM work、WAL、temporary disk は選択データ量に比例し、snapshot consistency を壊して早期 checkpoint してはいけません。

`npm run build` は `webui/scripts/build.mjs` を使い、typecheck 後に同階層の staging directory へ build し、staged `index.html` が参照する全 asset を検証して、asset を先に公開し、最後に `dist/index.html` を atomic replace します。failure injection self-test は失敗時にも旧 entry point とその asset が使用可能なことを確認します。

Search candidate は既定の 32 Mi-character と 32 MiB UTF-8 上限内で incremental BLOB read により exact verify されます。trusted local test は `CHATGPT_ARCHIVE_SEARCH_EXACT_VERIFY_CHARS` で最大 100 Mi characters まで opt in でき、この明示的 opt-in は対応する有効な UTF-8 byte capacity も許可します。candidate budget 枯渇時は partial/pending と利用可能な signed continuation を返し、hard per-row limit 超過 legacy candidate は false-exact な空結果ではなく pending のままです。long display cursor は target row revision と identity に bind し、search anchor は UTF-8 byte offset を直接記録します。

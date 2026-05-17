# ChatGPT Export Archiver

言語: English | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

ChatGPT Export Archiver は、OpenAI / ChatGPT のエクスポート ZIP を検索可能な SQLite アーカイブへ変換する、ローカル優先でプライバシーに配慮したツールです。元のエクスポートをブラウザーへ渡さず、再実行しやすい増分インポート、CLI でのエクスポートと検索、ZIP を選択して取り込めるローカル React Web UI を提供します。

## このプロジェクトでできること

- OpenAI / ChatGPT のエクスポート ZIP、または展開済みのエクスポートディレクトリから `conversations.json` を SQLite に取り込みます。
- 会話メタデータ、mapping nodes、メッセージの role、本文テキスト、タイムスタンプ、親ノード関係、インポート警告を保存します。
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

`--delete-input-on-success` はメインのインポートトランザクションが成功した後だけ実行されます。明示された入力が symlink の場合、リンク先の実 ZIP ではなく、コマンドラインで指定された symlink 自体を削除します。

それでも、データベースやエクスポートされた Markdown / TXT には個人的な会話内容が含まれる可能性があります。`archive/*.db`、エクスポート済みファイル、元の ChatGPT エクスポート ZIP は機密データとして扱ってください。

## 必要条件

- Python 3.10 以降。
- JSON1 と FTS5 が有効な SQLite。現在の macOS、Windows、Linux の多くの Python ビルドには通常含まれています。
- React Web UI を再ビルドしたりフロントエンド検査を実行したりする場合のみ、Node.js と npm が必要です。runnable 配布には `webui/dist` が含まれるため、通常のローカル Web UI 利用ではフロントエンドの再ビルドは不要です。
- Web ZIP アップロードを使う場合は、`requirements-web.txt` の Web 依存関係をインストールしてください。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

Windows PowerShell:

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

Windows cmd.exe:

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt
```

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

任意の Web 検索インデックスを再構築します。

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

Web UI を起動します。

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## インポートモード

大規模アーカイブでは次のコマンドを推奨します。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

インポート後に SQLite の planner statistics と FTS インデックスをさらに整理したい場合は、次を使います。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` は既定で無効です。ZIP の別バックアップがある場合にだけ使用してください。削除はメインのインポートトランザクションが成功した後にだけ実行されます。削除に成功した場合、CLI はパスを出さずに `deleted_input True` を表示します。削除に失敗してもインポートは成功扱いで、run は `finished` のまま、構造化された `delete_input_failed` warning が保存され、CLI には `delete_input_failed True` と例外型だけが表示されます。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

増分インポートは通常の使い方です。新しいエクスポートを同じデータベースへ入れると、変更済みの会話だけが更新され、残りのアーカイブは保持されます。

## Web UI ワークフロー

Web UI は FastAPI が配信するローカル React アプリです。推奨される使い方は、runnable tree に含まれるビルド済みの `webui/dist` をそのまま配信することです。

```bash
python chatgpt_archive.py web --port 8787
```

データベースがある場合は明示的に指定するか、既定のパスを使えます。データベースがない場合でも Web UI を起動し、インポートパネルから ChatGPT エクスポート ZIP をアップロードできます。アップロードインポートは直列化され、同じプロセス内で同時に動く SQLite writer は 1 つだけです。

Web アップロードインポートが成功すると、バックエンドは CLI と同じコア import pipeline を使い、その後 `verify`、`stats`、`web-index` を実行します。アップロードされた ZIP はサーバー側の一時コピーであり、ディスク上の元ファイルとは独立して削除されます。


## Web UI 受け入れチェックリスト

Web 経路を変更したとき、または runnable delivery を準備するときは、次を確認します。

- データベースなしで Web UI を起動し、空状態の契約どおりに表示されることを確認する。
- ブラウザーから小さな ChatGPT エクスポート ZIP をインポートし、job が完了することを確認する。
- アップロードインポート後にバックエンドが `verify`、`stats`、`web-index` を実行することを確認する。
- ページを更新し、会話を一覧表示して開けることを確認する。
- より新しい ZIP を再インポートし、増分経路が引き続き動くことを確認する。

runnable delivery の Web 経路は `webui/node_modules` を必要としないはずです。ビルド済みの React assets は `webui/dist` から配信されます。

## 検索構文

CLI 検索は、SQLite のクエリ文字列を直接渡すのではなく、プロジェクト共通の安全な検索構文を使います。通常のキーワード、引用符付きフレーズ、`-term` による除外、`OR`、`role:user`、`source:zip`、`path:current`、`path:all`、`scope:title`、`scope:message` などのフィルターを使えます。表示されるのは conversation ID、node ID、role で、snippet は表示されません。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web 検索は `web-index` が作成する任意の normalized trigram インデックスを使います。ブラウザーで実用的な部分文字列検索を行うためのものです。これらの任意インデックスが存在しない、または壊れている場合は再構築してください。

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## 検証と任意 Web インデックス

`verify` は SQLite integrity とプロジェクト固有の整合性を確認します。missing current node、broken parent link、空の会話、親ノードの cycle も対象です。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

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

runnable delivery には Python ソース、テスト、ドキュメント、`requirements-web.txt`、`webui/dist` を含めます。`webui/node_modules`、`webui/tsconfig.tsbuildinfo`、Python cache ディレクトリや bytecode、coverage/typecheck cache、`.DS_Store`、`__MACOSX`、`Thumbs.db`、`Desktop.ini`、`.gitignore.md`、一時ログ、ローカル受け入れログ、`*.log`、`*.ndjson`、`*.jsonl`、`archive/`、`exports/`、任意の `*.zip`、`conversations*.json`、`*.db`、`*.sqlite`、`*.sqlite3` などの実データベースファイル、または `*.db-journal`、`*.sqlite-wal`、`*.sqlite-shm`、`*.sqlite-journal`、`*.sqlite3-wal`、`*.sqlite3-shm`、`*.sqlite3-journal` などの SQLite sidecar は含めないでください。ディレクトリ検査では対象ルート直下の `.git` は許可されるため通常の Git clone をそのまま検査できますが、入れ子の `.git` は失敗します。ZIP delivery ではどの `.git` エントリも失敗します。

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

メインデータベースは conversations、mapping nodes、import runs、warnings を保存します。CLI FTS テーブルは `message_fts` です。任意 Web 検索用の補助テーブルには `web_message_norm`、`web_title_norm`、`web_message_trigram`、`web_title_trigram` と SQLite FTS5 shadow tables が含まれます。

明示的に計画され文書化された migration がない限り、小さな堅牢性修正でデータベース schema を変更することはありません。

## 既知の制限

- これはローカルアーカイブツールであり、クラウド同期サービスではありません。
- Web UI はローカル利用を想定しています。独自のアクセス制御なしに信頼できないネットワークへ公開しないでください。
- エクスポート解析は、現在確認されている OpenAI / ChatGPT のエクスポート形式に従います。上流の形式が変わった場合は、新しいインポート経路を信頼する前に `inspect` とテストを更新してください。
- 非常に大きなアーカイブでは、インポート、FTS 再構築、Web trigram インデックス作成に時間がかかることがあります。大規模インポートでは `--rebuild-fts` 経路を優先してください。

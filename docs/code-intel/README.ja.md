# 🔎 Code Intel：サブエージェントのための四層コード検索

[**English**](README.md) · [中文](README.zh.md) · **日本語** · [한국어](README.ko.md)

> 本稿は[サブエージェントシステム README](../../agent/tools/subagent/README.ja.md)と[サブエージェント設計ページ](../subagent/README.ja.md)の設計レイヤの姉妹編です。前者は spawn パイプライン、ロール別ツールポリシー、`sessions_*` ツールのランタイムと API のリファレンスであり、後者は二軸ロールモデル（深さロール × 機能ロール）を述べます。本ページはそれらのロールが使うコード検索フレームワーク——tree-sitter シンボル索引、ast-grep の構造検索と書き換え、LSP 精密検索ツール、埋め込みベースのセマンティックコード検索——を記録します。

事実の出典：`agent/tools/code_intel/**`、`config/features/agent_side/code_intel.py`、`config/features/agent_side/code_intel_semantic.py`、`config/features/agent_side/ast_grep.py`、`config/features/agent_side/lsp.py`、`config/path.py`、`agent/tools/subagent/types/functional_role.py`、`agent/tools/subagent/spawn/core.py`、`agent/tools/subagent/spawn/system_prompt.py`、`agent/tools/subagent/roles/definitions/librarian/AGENTS.md`。以下の記述はすべてこのコードに対して検証済みです。

## 目次

- [概要](#-概要)
- [設計不変条件](#-設計不変条件)
- [第 0 層：キーワード検索（ripgrep）](#-第-0-層キーワード検索ripgrep)
- [第 1 層：Tree-sitter シンボル索引](#-第-1-層tree-sitter-シンボル索引)
- [第 2 層：ast-grep 構造検索と書き換え](#-第-2-層ast-grep-構造検索と書き換え)
- [第 3 層：LSP 精密検索](#-第-3-層lsp-精密検索)
- [第 4 層：セマンティックコード検索](#-第-4-層セマンティックコード検索)
- [ロールモデルとツール面](#-ロールモデルとツール面)
- [設定](#-設定)
- [環境能力マトリクス](#-環境能力マトリクス)
- [制限と失敗モード](#-制限と失敗モード)
- [テストマップ](#-テストマップ)
- [関連ドキュメント](#-関連ドキュメント)

## 🎯 概要

Code Intel は 5 つの検索エンジンでコード理解の問いに答えます。各エンジンは異なる問いの形に対応します。うち 4 つはサブエージェント専用で、キーワード層はメインエージェントが共有する唯一の層です：

| 層 | 答える問い | ツール | 開放範囲 |
|----|------------|--------|----------|
| **キーワード検索** | このリテラルや正規表現はどこに出るか——内容か、ファイル名か？ | `search_files` | メインエージェント + `researcher` + `librarian` |
| **Tree-sitter シンボル索引** | シンボルはどこで定義され、誰が呼ぶか？ | `explore`、`callers`、`callees`、`impact` | `researcher` + `librarian` |
| **ast-grep** | どのコードがこの構造形を持ち、どう書き換えるか？ | `ast_grep_search`、`ast_grep_rewrite` | すべてのサブエージェント |
| **LSP** | 型チェッカーはこの位置について何を知るか？ | `lsp_goto_definition`、`lsp_find_references`、`lsp_workspace_symbol`、`lsp_call_hierarchy`、`lsp_rename`、`lsp_diagnostics`、`lsp_format`、`lsp_status` | `researcher` + `librarian` |
| **セマンティック検索** | この概念を実装するのはどのコードか？ | `semantic_code_search` | `researcher` + `librarian` |

4 層は 1 つのワークフローとして組み合わさります：`explore` がシンボルと呼び出し経路を特定し、`ast_grep_search` が言語横断で構造パターンを探し、LSP ツールが型を意識して定義と参照を解決し、`semantic_code_search` が名前で一致しないときに概念で検索します。4 層すべては子エージェントの組み立て時に構築・注入されます。

```text
child agent assembly (spawn/core.py)
  base tools → role policy (allow / deny / main_only gate)
    ├─ role ∈ CODE_INTEL_ROLES?  → + explore/callers/callees/impact/semantic_code_search
    │                              + the eight lsp_* tools
    └─ always                    → + ast_grep_search / ast_grep_rewrite
```

## 🧱 設計不変条件

1. **メインエージェントはこれらのツールを決して見ません。** code-intel と LSP のビルダーは `_MAIN_TOOLS_BUILDERS` に登録されません。注入は `_build_child_agent` の内部、ロールツールポリシーの後だけで行われます。
2. **ツール面はロールで制御されます。** tree-sitter スイートと 8 つの LSP ツールは `CODE_INTEL_ROLES`（`researcher`、`librarian`）にのみ渡されます。ast-grep は中核的な構造能力のため、すべての機能ロールが利用できます。
3. **fail-open が契約です。** バイナリ欠如、解析不能なファイル、利用不能なモデル、タイムアウト、拒否された要求はすべて実行可能なメッセージ付きの JSON ペイロードを返します。検索の失敗が子エージェントのターンに例外を投げることはありません。
4. **すべてが構造的に有界です。** ファイル数、ファイルあたりのバイト数、構築時間、バッチ、結果数、パス数、パターンサイズ、開くファイル数に上限があります。LSP マネージャはさらに LRU 同時実行上限とアイドル回収を課します。
5. **書き込みはオプトインで、封じ込めを検証します。** `ast_grep_rewrite` は `dry_run=false` でない限りプレビューし、`lsp_rename` と `lsp_format` は適用フラグなしではプレビューだけです。すべての書き込み経路はディスクに触れる前にトラバーサルとプロジェクトルート内包を再検証します。
6. **新しいモデルプロバイダを導入しません。** セマンティック検索は既存の `models/embed_model` と `models/reranker_model` ラッパーを再利用し、新しいストレージは SQLite 索引だけです。
7. **ルートは呼び出しごとに解決されます。** `SHERRY_CODE_INTEL_ROOT`、`SHERRY_SG_ROOT`、`SHERRY_LSP_ROOT` がドリルとテスト用に作業ルートを上書きします。未設定時はプロセスの cwd と共有の `resolve_project_path` ゲートが適用されます。

## 🔍 第 0 層：キーワード検索（ripgrep）

`search_files` は最も安価な層であり、メインエージェントが持つ唯一の検索ツールです：ファイル内容（`target='content'`）またはファイル名（`target='files'`）への正規表現スキャンです。内部では 2 つのエンジンが 1 つの契約を共有します——ripgrep を解決できればそれを使い、できなければ純 Python 走査に切り替えます——どちらが動いたかは**呼び出し側から見えません**。結果エンベロープ、ページの切れ目、3 段階の打ち切りマーカーはすべて `agent/tools/file_tools/search_scan.py` の `ScanState` が生成するためです。

| 属性 | 実装 |
|---|---|
| エンジン | `agent/tools/pub_base/rg_backend.py` が `rg --json` を行単位でストリーム解析し、`--sort=path` でページングを安定させます |
| フォールバック | `agent/tools/file_tools/search_files.py` の走査が 3 つの場合を担います：`rg` の無いホスト、Rust エンジンが表現できないパターン（ルックアラウンド、後方参照）、rg のハード失敗（`exit >= 2`） |
| 探索順 | `agent/tools/pub_base/rg_resolver.py`：`SHERRY_RG_PATH` → インタプリタ自身のディレクトリと `sys.prefix/bin` → `~/.sherry/runtime/ripgrep/<slug>` → code-intel bin キャッシュ → `PATH` → Homebrew。各候補は `--version` プローブを通ります |
| 依存 | `ripgrep-bin`（wheel は linux aarch64/x86_64/riscv64、macOS 両方、Windows 両方をカバー）——`uv sync` が `rg` をインタプリタの隣に置きます。リゾルバは `PATH` より**先**にそのディレクトリを見ます。`./.venv/bin/python -m server` で起動したサーバーでは venv の `bin` が `PATH` に無いためです |
| 上限 | 5.0 秒のスキャン予算と 10 000 件のマッチ上限（`TOOLS_TIMEOUTS`）、ページは `offset+limit` の 1 件先で切断、`SKIP_DIR_NAMES` と設定されたプルーン先を `--glob` 除外に変換 |
| ループを塞がない | 非同期経路はスキャンをワーカースレッドで実行します |

本リポジトリでの実測：同じ範囲の同じ内容検索は走査で **16.6 s**、ripgrep で **1.2 s**（`--sort=path` で 2.0 s）。重要なのは速度ではなく予算です：走査はリポジトリ全体を 5 秒以内に走り切れないため、存在しないパターンの検索は `scan_stop_reason: "time_budget"` を返しました——正直ですが不完全です。ripgrep は走り切ります。

実装で突き当たり、それぞれテストで固定した 3 つの罠：

- **glob は 1 つの argv として渡す**（`--glob=!**/x/**`）。裸で渡すと**検索パターン**とパスとして解釈され、rg は全体を走査し、誤ったものに一致して終了コード 2 を返します——フォールバックがそれを隠します。
- **include glob は exclude より前**。rg は重複する glob を「後勝ち」で解決するため、除外の後に置いた include は剪定済みディレクトリを再び取り込みます。
- **stderr は上限付きのスレッドで排出する**。こちらは stdout でブロックしているため、子プロセスが stderr パイプを埋めるとウォッチドッグに殺されるまで止まります。

他の spawn 箇所と共有する有界の後始末は `agent/tools/pub_base/process_reap.py` にあります：SIGTERM → 待機 → SIGKILL → 待機 → ハンドルを放棄して警告——割り込み不能 I/O で固まった子（あるいは孫がパイプを握っている子）がタイムアウトをハングに変えてはならないからです。

## 🌳 第 1 層：Tree-sitter シンボル索引

索引は 4 つのモジュールで実装されます：`agent/tools/code_intel/extract.py`（純粋なバイト → シンボルと呼び出し箇所）、`agent/tools/code_intel/indexer.py`（SQLite 永続化と増分走査）、`agent/tools/code_intel/query.py`（マッチングと呼び出しグラフ照会）、`agent/tools/code_intel/tools.py`（LangChain ラッパー）。

| 文法 | 言語キー | 拡張子 | シンボルノード |
|------|----------|--------|----------------|
| Python | `python` | `.py` | `function_definition`、`class_definition`、`call` |
| TypeScript | `typescript`、`tsx` | `.ts`、`.tsx`、`.js`、`.jsx` | `function_declaration`、`class_declaration`、`method_definition`、`variable_declarator`（アロー関数と関数式）、`call_expression` |
| Rust | `rust` | `.rs` | `function_item`、`struct_item`、`enum_item`、`trait_item`、`impl_item`、`call_expression`、`macro_invocation` |
| Go | `go` | `.go` | `function_declaration`、`method_declaration`、`type_spec`（`struct_type` と `interface_type`）、`call_expression` |

ノード名と文法バージョンは固定されたパッケージに対して実測されています——`tree-sitter 0.26.0`、`tree-sitter-python 0.25.0`、`tree-sitter-typescript 0.23.2`、`tree-sitter-rust 0.24.2`、`tree-sitter-go 0.25.0`——パーサーは `Parser(Language(capsule))` として構築されます。`javascript` は `typescript` 文法に、`jsx` は `tsx` に別名解決されます。結果は 3 つの SQLite テーブル `symbols`、`call_edges`、`index_meta` に格納されます。呼び出しグラフは 2 パス目で優先順位どおりに解決されます：同一ファイル、同一ディレクトリ、任意のグローバル一致、最後に距離 ≤ 2 の Levenshtein ファジーマッチです。

増分性と上限：各ファイルの `mtime` と `size` が `index_meta` と比較され、変更されたファイルだけが再解析され、削除されたファイルは解決済みの入辺の切り離しを含めて破棄され、書き込みは 100 ファイル単位のトランザクションでバッチ処理されます。走査は `code_intel_index_max_files`（5000）または `code_intel_index_timeout_s`（60）で停止し、`truncated` を報告します。fail-open はファイル単位です：構文エラー、巨大ファイル（1 MB 超）、未知の拡張子、読み取りエラーはそれぞれ `index_meta` 行を記録してスキップされます——構文エラーのファイルが部分的に索引付けされることは決してありません。変更操作はインスタンスロックで直列化され、接続は WAL で 30 秒の busy timeout を持ちます。

照会は読み取り前に増分索引を更新するため、編集後の検索が古いシンボルを返すことはなく、ツリーが変わっていなければ安価な no-op です。4 つのツールは次のとおりです：

| ツール | 入力 | 出力 | 上限 |
|--------|------|------|------|
| `explore` | 曖昧な概念、シンボル名、自然言語の意図 | 一致したシンボルのソース、docstring、直接の呼び出し元・呼び出し先。一致なしの場合は提案 | 10 シンボル、8000 ソース文字 |
| `callers` | シンボル名 | それを呼ぶすべての関数・メソッドと呼び出し箇所の行 | 完全名優先、ファジーをフォールバック |
| `callees` | シンボル名 | そのシンボルが発するすべての呼び出し（判明時は解決済みの対象ファイルと行） | 重複する呼び出し箇所を集約 |
| `impact` | シンボル名 | 推移的な呼び出し元（ブラスト半径）、深さと経由した名前付き | 深さ 3、`truncated` フラグ |

## 🧬 第 2 層：ast-grep 構造検索と書き換え

ast-grep は検索パターンを正規表現ではなくコードとして扱うため、`$NAME` は 1 つの AST ノードに、`$$$NAME` は 0 個以上のノードに一致します。実装は `agent/tools/code_intel/ast_grep/` にあります：`resolver.py`（バイナリ発見）、`provisioner.py`（検証付きダウンロード）、`install_hints.py`（復旧ヒント）、`runner.py`（2 つのツール）。単体で使えるフォールバックインストーラは同ディレクトリの `scripts/install.sh` と `scripts/install.ps1` です。

バイナリ発見は 5 層で、各候補は非空の通常ファイルであり、出力に `ast-grep` を含む短い `--version` プローブを通過しなければなりません：

1. 明示的オーバーライド——環境変数 `SHERRY_SG_PATH`。
2. プロビジョニング済みランタイム——`~/.sherry/runtime/ast-grep/<platform>-<arch>/sg`。
3. code-intel bin キャッシュ——`.codeintel/ast-grep/bin`（`ast-grep` の次に `sg`）。
4. `PATH` 検索——`ast-grep` の次に `sg`、Windows の `PATHEXT` に対応。
5. Homebrew と Linuxbrew のプレフィックス。

どの層も解決しない場合、最初のツール呼び出しが固定された `0.43.0` リリースを自動プロビジョニングします：プラットフォーム別 URL と SHA-256 は `config/features/agent_side/ast_grep.py` にあり、ダウンロードは 60 秒のタイムアウトで有界され、チェックサム不一致は致命的です（fail-closed——検証されていないバイトをインストールすることはありません）。解凍には標準ライブラリの `zipfile` を使います（ホストに `unzip` がない可能性があるため）。`sg` ランチャー（自身のパス基準で再実行し、一部のサンドボックスで動かない）より実体の `ast-grep` バイナリが優先され、抽出物はモード 755 でアトミックに書き込まれます。解決結果はファイルが存在する間プロセス単位でキャッシュされます。

両ツールは環境を洗浄したうえで `sg` CLI をサブプロセスとして 30 秒のタイムアウトで実行します：

| ツール | 動作 | 安全な既定 |
|--------|------|------------|
| `ast_grep_search` | 既定で `--strictness smart` を付けて `sg run --json=stream` を実行。コンパクトな一致（ファイル、1 始まりの行、テキスト、メタ変数）を返す | 最大 50 一致、64 パス、16 KiB パターン |
| `ast_grep_rewrite` | 置換リストをプレビュー。`dry_run=false`（`--update-all`）のときだけその場で適用 | `dry_run` の既定は `true` |

サブプロセスの起動前にすべてのパスが審査されます：オーバーライドなしでは正規の `resolve_project_path` ゲートを使い、`SHERRY_SG_ROOT` 設定時は同じトラバーサル判定とそのルートへの内包検証を適用します——したがって適用型の書き換えはプロジェクト内にしか書き込めません。

## 🛰️ 第 3 層：LSP 精密検索

LSP 層は `agent/tools/code_intel/lsp/` にあります：`protocol.py`（URI、1 始まりと 0 始まりの位置、結果整形）、`resolver.py`（バイナリ発見）、`installer.py`（許可リストによる自動インストール）、`fallback.py`（可用性から実行可能メッセージへ）、`client.py`（stdio 上の JSON-RPC）、`manager.py`（プロセスライフサイクル）、`tools.py`（8 つのツール）。ツールは 1 始まりの `line` と `character` を受け取り、内部で LSP ネイティブの 0 始まり位置に変換します。

| ツール | 目的 | 既定の動作 |
|--------|------|------------|
| `lsp_goto_definition` | 型を意識した定義へのジャンプ | 読み取り専用 |
| `lsp_find_references` | シンボルへのすべての参照 | 読み取り専用 |
| `lsp_workspace_symbol` | ワークスペースシンボルのファジー検索 | 読み取り専用、結果数に上限 |
| `lsp_call_hierarchy` | 呼び出し元（incoming）と呼び出し先（outgoing） | 読み取り専用 |
| `lsp_rename` | ワークスペース全体のリネーム | `WorkspaceEdit` をプレビュー。`dry_run=false` のときだけ適用 |
| `lsp_diagnostics` | ファイルのエラーと警告 | 非同期の `publishDiagnostics` 通知を待ち、`timed_out` を報告 |
| `lsp_format` | ファイル全体または範囲の整形 | プレビュー。`write=true` のときだけ適用。未対応時は成功を装わず `supported=false` を報告 |
| `lsp_status` | 設定済みすべてのサーバーの正直な可用性 | 何も起動しない |

発見手順は ast-grep の各層に対応しますが、marker によるゲートが加わります：明示的な絶対パス。次に、同じ階層に対応する marker ファイルが存在するときだけ信頼されるリポジトリローカルの bin ディレクトリ（`pyproject.toml` → `.venv/bin`、`package.json` → `node_modules/.bin`、`Cargo.toml` → `target/debug`、`go.mod` → `bin`、リポジトリルートまで上方走査）。次に `~/.sherry/runtime/lsp` の配置、`PATH`、Homebrew の順です。解決結果は `(cwd, command, platform)` 単位でキャッシュされます。

言語サーバーは重量級の常駐サブプロセスなので、プロセス単位のマネージャが制約します：サーバーは `(language, cwd)` への最初の要求で遅延起動し、同時に生きるのは最大 `lsp_max_concurrent_servers`（2）個で、超えるときは最も長く使われていないものを追い出します。アイドルスイーパーは `lsp_idle_shutdown_s`（300 秒）を超えて未使用のサーバーを回収し、`shutdown_all` は `atexit` フックから実行されます——クライアントが孤児プロセスを残すことはありません。各クライアントは `Content-Length` で JSON-RPC をフレーム化し、リーダースレッドで応答を振り分け、サーバーからクライアントへの要求には空の結果を返し、すべての待機をタイムアウトで有界し、開くファイルを 32 に制限し（最も古いものを閉じる）、1 MB 超のファイルを拒否します。

可用性は 3 状態で報告されます：`available`、`not_installed`（設定済みだが発見を通過するバイナリがない——インストールヒントとローカルインストールコマンドを返す）、`not_configured`（言語が `lsp_enabled_languages` にない）。利用不能な経路はすべてツール名、インストールヒント、そして `explore` または `terminal`（rg/grep）へのフォールバックを返します。自動インストールは既定で無効（`lsp_auto_install=False`）で、有効時も対象言語の許可リスト済みコマンドだけを 60 秒のタイムアウトと洗浄済み環境で実行します。

## 🧠 第 4 層：セマンティックコード検索

セマンティック検索は任意の行窓を切るのではなく第 1 層のシンボルテーブルを埋め込むため、各ベクトルは正確に 1 つの関数、メソッド、クラスを記述します。`agent/tools/code_intel/semantic/chunker.py` はシンボルとその位置を示すヘッダー行とシンボル本体からチャンクを作ります（読み取り失敗時は保存済みスナップショットにフォールバック）。`agent/tools/code_intel/semantic/indexer.py` が `code_embeddings` テーブルを所有し、`agent/tools/code_intel/semantic/search.py` がコサインで順位付けし任意で再順位付けします。

索引は構造的に増分です：まずシンボル索引を更新し、シンボルが存在しなくなった埋め込みを削除し、ベクトルを持たないシンボルだけを埋め込みます。行は 16 チャンク単位（設定可能）で書かれ、1 回の構築で最大 1000 チャンク、1 ファイルあたり最大 60、各チャンク最大 2000 文字です。ベクトルは `array('d')` の BLOB として `code_embeddings` テーブルに格納され、同テーブルは `model` 名と `dim` も保持します。保存済みのモデル変更や次元衝突を検出すると、同じ呼び出し内で索引を消去して再構築します。部分的に埋め込まれた索引は、埋め込み済みシンボルをスキップするため再開可能です。埋め込みバックエンドは既存の `models.build_embed_model`（`EMBEDDING_*` 環境変数で選択、既定はローカル `bge-m3`）であり、新しいプロバイダは導入しません。

検索はまず索引を自己修復し、クエリを埋め込み、格納済みの全チャンクを純 Python のコサイン類似度で順位付けし、候補プール 40 を保ち、リランカーが設定されていれば既存の `models.build_reranker_model` に上位候補を渡します。最終ページは既定で `code_intel_semantic_default_top_k`（5）件、最大 20 件です。fail-open の状態はペイロードに明示されます：索引済み埋め込みがなければ先に索引を構築するよう促し、クエリモデルが利用不能なら `degraded` メッセージを返し、リランカー未設定ならコサイン順のまま `reranked=false` となり、索引とクエリの次元が不一致なら無意味な採点をせず再構築を求めます。

## 🧭 ロールモデルとツール面

`agent/tools/subagent/types/functional_role.py` の `CODE_INTEL_ROLES` は、ツール注入（`agent/tools/subagent/spawn/core.py`）とプロンプト指針（`agent/tools/subagent/spawn/system_prompt.py`）の両方にとって唯一の真実の源であり、ツール面とその文書がずれることはありません：

| 機能ロール | Tree-sitter スイート | LSP ツール | ast-grep |
|------------|----------------------|------------|----------|
| `researcher` | あり | あり | あり |
| `librarian` | あり | あり | あり |
| `general` | なし | なし | あり |
| `executor` | なし | なし | あり |
| `reviewer` | なし | なし | あり |

メインエージェントはこの表の外にあります：ビルダーは `_MAIN_TOOLS_BUILDERS` に決して追加されず、分離テストが `build_lsp_tools` の不在をそのリストと `agent.tools` 名前空間の両方で主張します。`agent/tools/subagent/roles/definitions/librarian/AGENTS.md` の `librarian` 定義は想定される外部リポジトリのワークフロー——`terminal` でクローンし、`explore` と `semantic_code_search` で索引化・検索し、パーマリンクで答える——を記録しています。`search_files` はそのゲートの例外です：メインのツール（`_MAIN_TOOLS_BUILDERS` に含まれます）であり、`researcher` / `librarian` が許可リストで名指ししているため、キーワード検索はメインエージェントが code-intel の役割と共有する唯一の検索面です。二軸ロールモデル自体（深さロール × 機能ロール）は[サブエージェント設計ページ](../subagent/README.ja.md)に、ロール別のツール一覧は[サブエージェントシステム README](../../agent/tools/subagent/README.ja.md)にあります。

## ⚙️ 設定

| オブジェクト | モジュール | 主要項目（既定値） |
|--------------|------------|--------------------|
| `CODE_INTEL` | `config/features/agent_side/code_intel.py` | 最大 5000 ファイル、1 ファイル 1 MB、構築予算 60 秒、バッチ 100、explore 10 シンボルと 8000 文字、呼び出し深さ 3、ファジースコア 0.3 |
| `CODE_INTEL_SEMANTIC` | `config/features/agent_side/code_intel_semantic.py` | モデル `bge-m3`、top-K 5（最大 20）、候補プール 40、バッチ 16、1 構築 1000 チャンク、1 ファイル 60、1 チャンク 2000 文字 |
| `AST_GREP` | `config/features/agent_side/ast_grep.py` | 固定 `0.43.0`、50 一致、16 KiB パターン、30 秒実行タイムアウト、64 パス、60 秒プロビジョニングタイムアウト、5 秒バージョンプローブ |
| `LSP` | `config/features/agent_side/lsp.py` | 要求 10 秒、起動 15 秒、診断 15 秒、同時サーバー 2、アイドル停止 300 秒、50 結果、開くファイル 32、1 ファイル 1 MB、自動インストール無効 |
| `RIPGREP` | `config/features/agent_side/ripgrep.py` | enabled、`SHERRY_RG_PATH`、runtime ディレクトリ、5 s のバージョンプローブ、5 s / 5 s の回収猶予。スキャン予算とマッチ上限は `TOOLS_TIMEOUTS` |
| `CODE_INTEL_ROLES` | `agent/tools/subagent/types/functional_role.py` | `researcher` と `librarian` |

インデックスのデータベースは**セッションのプロジェクトディレクトリ**に従います：spawn 時は `<project_root>/.codeintel/index.db` に索引します（`agent/tools/code_intel/roots.py::index_db_for_root`）。プロジェクトを交互に使っても共有インデックスを再構築しません（保存されるパスはプロジェクト相対で、ルートをまたぐと衝突します）。プロジェクトルートが無い呼び出しは共有の `CODE_INTEL_DIR / "index.db"`（`config/path.py` の `CODE_INTEL_DIR = ROOT_DIR / ".codeintel"`）にフォールバックし、`code_intel_index_db_path` で明示的に固定でき、`SHERRY_CODE_INTEL_DB` が呼び出し時にパスを上書きします（`SHERRY_CODE_INTEL_ROOT` はルートを上書き）。

## 🖥️ 環境能力マトリクス

発見は能力ではありません：下表は 2026-09-23 に開発ホストで実測した状態で、「発見」は解決器が候補バイナリを見つけたことだけを意味し、サーバーが起動・応答することの証明ではありません。`python` だけが真のエンドツーエンドスモークを持ち、`typescript` は発見のみ、`rust` は rustup シムに解決しますが `rust-analyzer` コンポーネントが未インストールのため起動に失敗しフォールバックメッセージへ降格し、残る 7 言語は `not_installed` です。

| 言語 | サーバーコマンド | 本機での発見 | 検証の深さ |
|------|------------------|--------------|------------|
| `python` | `basedpyright-langserver --stdio` | あり——リポジトリの `.venv/bin` | 実スモーク：定義、参照、診断、リネームプレビュー、ステータス |
| `typescript` | `typescript-language-server --stdio` | あり——`PATH` | 発見のみ |
| `rust` | `rust-analyzer` | バイナリあり——ツールチェーン部品が欠如 | 起動がフォールバックへ降格 |
| `go` | `gopls` | なし | インストールヒントとフォールバック |
| `cpp` | `clangd` | なし | インストールヒントとフォールバック |
| `java` | `jdtls` | なし | インストールヒントとフォールバック |
| `ruby` | `ruby-lsp` | なし | インストールヒントとフォールバック |
| `bash` | `bash-language-server start` | なし | インストールヒントとフォールバック |
| `vue` | `vue-language-server --stdio` | なし | インストールヒントとフォールバック |
| `yaml` | `yaml-language-server --stdio` | なし | インストールヒントとフォールバック |

同じホスト上の他の 3 エンジン：

| エンジン | 状態 | 証拠 |
|----------|------|------|
| Tree-sitter 索引 | 4 文法がインストール済み（`tree-sitter 0.26.0` ファミリ） | 完全な code-intel スイートが通過（281 テスト） |
| ast-grep | プロビジョニング済みランタイム層から利用可能（`ast-grep 0.43.0`） | 基本の構造検索が実マッチを返す |
| セマンティック検索 | ローカル `bge-m3` バックエンドが利用可能 | 実埋め込みスモークが概念クエリに関連シンボルを返す |

これらの能力を欠くマシンは失敗せず降格します：ast-grep が解決できなければ検証付き自動プロビジョニング経路が起動し、埋め込みバックエンドが利用不能なら `semantic_code_search` は `degraded` メッセージを返して `explore` と `terminal` を指します。

## ⚠️ 制限と失敗モード

- **索引は呼び出し側が起動し、自己修復します。** バックグラウンド索引器はありません：最初の照会が索引を構築し、以降の照会は増分更新します。大規模リポジトリは最初の呼び出しで構築コストを払い、ファイルと時間の上限で有界されます（`truncated` が報告されます）。
- **ファイルイベント自動同期は採用していません。** セマンティック検索がクエリごとにインデックスを自己修復し、インデックスはオンデマンドで再構築されるため、常駐するファイル監視層はプロセスオーバーヘッドを増やすだけで効果は限定的です。
- **構文エラー、巨大、未知拡張子のファイルはスキップされます**。それぞれ理由を記録した `index_meta` 行が付き、部分索引されることはありません。
- **埋め込みバッチはモデルを再ロードします。** 各 `embed_documents` 呼び出しがバックエンドをロードする（ローカル GGUF ローダーはバッチごとに呼ばれる）ため、バッチはロード時間とピークメモリのトレードオフです。1 構築と 1 ファイルの上限がこれを有界に保ちます。
- **リランカー未設定は順位品質の低下だけです。** なければ結果はコサイン順のままでペイロードは `reranked=false` を示し、あっても失敗時は同じくコサイン順にフォールバックします。
- **ast-grep の初回使用はバイナリをダウンロードします。** プロビジョニング経路は 60 秒のタイムアウトで有界され、チェックサム不一致ならインストールを拒否します。オフラインやアセットのないプラットフォームではインストールヒントを返します。
- **LSP サーバーは重量級です。** マネージャは同時 2 に制限し、300 秒のアイドル後に回収し、最も長く使われていないサーバーを追い出します。ある言語の最初の要求はサーバー起動コストを払い、サーバーがウィンドウ内に何も発行しなければ `lsp_diagnostics` は `timed_out` を返すことがあります。
- **本ホストの LSP カバレッジは部分的です。** 発見できるのは `python` と `typescript` だけです。`rust` は解決できますがツールチェーン部品なしでは起動できず、残る 7 言語はインストールが必要です。自動インストールは既定で無効なので、サーバー欠如がパッケージマネージャ実行を引き起こすことはありません。
- **librarian のツール面は `read_file` / `terminal` / `web_search` と code-intel スイートです。** ここに `search_files` は含まれません —— そのビルダーは `_MAIN_TOOLS_BUILDERS` にないため、外部リポジトリのキーワード検索は `terminal`（rg/grep）と `explore` で行います。
- **書き込みは設計上 2 段階です。** `ast_grep_rewrite`、`lsp_rename`、`lsp_format` は明示的に要求されたときだけ書き込みます。プレビューが安全な既定であり、適用された編集も封じ込め検証を受けます。

## 🗺️ テストマップ

| 領域 | テスト |
|------|--------|
| シンボル索引、呼び出しグラフ、エンドツーエンド照会 | `tests/agent/tools/code_intel/test_indexer.py`、`tests/agent/tools/code_intel/test_query.py`、`tests/agent/tools/code_intel/test_e2e.py`、`tests/agent/tools/code_intel/test_integration.py` |
| 索引ツールとロール分離 | `tests/agent/tools/code_intel/test_tools.py`、`tests/agent/tools/code_intel/test_integration.py` |
| ast-grep の発見、プロビジョニング、ツール | `tests/agent/tools/code_intel/ast_grep/test_resolver.py`、`tests/agent/tools/code_intel/ast_grep/test_provisioner.py`、`tests/agent/tools/code_intel/ast_grep/test_runner.py`、`tests/agent/tools/code_intel/ast_grep/test_install_hints.py`、`tests/agent/tools/code_intel/ast_grep/test_ast_grep_e2e.py` |
| LSP のプロトコル、リゾルバ、インストーラ、フォールバック、クライアント、マネージャ、ツール | `tests/agent/tools/code_intel/lsp/test_protocol.py`、`tests/agent/tools/code_intel/lsp/test_resolver.py`、`tests/agent/tools/code_intel/lsp/test_installer.py`、`tests/agent/tools/code_intel/lsp/test_fallback.py`、`tests/agent/tools/code_intel/lsp/test_client.py`、`tests/agent/tools/code_intel/lsp/test_manager.py`、`tests/agent/tools/code_intel/lsp/test_lsp_tools.py`、`tests/agent/tools/code_intel/lsp/test_lsp_extended.py` |
| LSP ロール分離と実スモーク | `tests/agent/tools/code_intel/lsp/test_role_isolation.py`、`tests/agent/tools/code_intel/lsp/test_lsp_smoke.py`、`tests/agent/tools/code_intel/lsp/test_lsp_e2e.py` |
| セマンティックのチャンク、索引、検索、スモーク | `tests/agent/tools/code_intel/semantic/test_chunker.py`、`tests/agent/tools/code_intel/semantic/test_indexer.py`、`tests/agent/tools/code_intel/semantic/test_search.py`、`tests/agent/tools/code_intel/semantic/test_semantic_e2e.py`、`tests/agent/tools/code_intel/semantic/test_semantic_smoke.py` |
| キーワード層：ripgrep と走査、探索順、回収 | `tests/agent/tools/pub_base/test_rg_resolver.py`、`tests/agent/tools/pub_base/test_rg_backend.py`、`tests/agent/tools/pub_base/test_process_reap.py`、`tests/agent/tools/file_tools/test_search_engines.py`、`tests/agent/tools/file_tools/test_search_bounds.py` |
| ロール配線とプロンプト節 | `tests/agent/tools/subagent/types/test_functional_role.py`、`tests/agent/tools/subagent/roles/test_loader.py`、`tests/agent/tools/subagent/spawn/test_functional_role_integration.py`、`tests/agent/tools/subagent/spawn/test_system_prompt_role.py` |

## 🔗 関連ドキュメント

| ページ | 内容 |
|--------|------|
| [サブエージェントシステム README](../../agent/tools/subagent/README.ja.md) | ランタイムと API リファレンス：spawn パイプライン、レジストリ、ロール別ツール一覧 |
| [サブエージェント設計](../subagent/README.ja.md) | 二軸ロールモデル（深さロール × 機能ロール）と spawn 権限ガード |
| [Context Engine README](../../context_engine/README.ja.md) | セマンティック層が再利用する埋め込み保存と検索のパターン |

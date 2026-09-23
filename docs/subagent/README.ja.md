# 🧩 サブエージェント設計：二軸ロール、権限ガード、階層化された完了ゲート

[**English**](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> 本稿は[サブエージェントシステム README](../../agent/tools/subagent/README.ja.md) の設計レイヤの姉妹編です。後者はランタイムと API のリファレンス——spawn パイプラインの各フェーズ、レジストリ状態機械、announce 配信、ツール schema、完全な設定表——です。本ページはロールモデル、spawn 権限ガード、四層の完了ゲートスタックの背後にある設計不変条件を述べ、各層が受け入れる失敗モードを明示します。

事実の出典：`agent/tools/subagent/**`（`types/`、`capabilities/`、`roles/`、`spawn/`）、`agent/tools/taskflow/step_judge.py` と `agent/tools/taskflow/tools/`、`agent/middlewares/subagent_completion_drain/`、`config/features/agent_side/`。以下の記述はすべてこのコードに対して検証済みです。

## 目次

- [概要](#-概要)
- [二つの役割軸](#-二つの役割軸)
- [機能ロール：定義・読み込み・作用](#-機能ロール定義読み込み作用)
  - [定義ファイル](#-定義ファイル)
  - [フェイルオープンローダー](#-フェイルオープンローダー)
  - [ロールが駆動するもの](#-ロールが駆動するもの)
- [spawn 権限ガード](#-spawn-権限ガード)
- [完了判定器と goal loop](#-完了判定器と-goal-loop)
- [階層化された完了ゲート](#-階層化された完了ゲート)
- [他サブシステムとの関係](#-他サブシステムとの関係)
  - [合成集約](#-合成集約)
- [設定](#-設定)
- [テストマップ](#-テストマップ)
- [限界](#-限界)

## 🎯 概要

派遣された worker は、二つの異なる問いへの二つの直交する答えで記述されます：

1. **誰が spawn でき、何を制御できるか？**——**深度ロール**（`SubagentSessionRole`）。ネスト深度から導出され、spawn 権限と制御スコープを単独で決めます。
2. **どの種類の worker か？**——**機能ロール**（`FunctionalRole`）。明示的な特化であり、子 LLM のティア、ツール許可リスト、ロール固有のプロンプト節を単独で決めます。

完了判定はその後、子実行から親ターンへと外側に積まれた四つの常時有効な層で検証されます：子実行の完了判定器と goal loop、TaskFlow のステップ判定器、`taskflow_finish` のフローゲート、そして親ターンの完了 drain プログラム的ゲートです。二つのロール軸とゲートスタックが本ページの主題です。API の詳細は[モジュール README](../../agent/tools/subagent/README.ja.md) にあります。

### 🧱 不変条件

1. **二つの軸は決して融合しない。** 機能ロールが spawn 権限を与えることはなく、深度ロールがツール許可リストやプロンプトを変えることもありません。すべての spawn で両者が合成されます。
2. **`general` は恒等ロール。** 機能ロールのヒントがなければ spawn 挙動は不変です：定義は読み込まれず、LLM ティアは深度ロールが供給します。
3. **spawn 権限は二度にわたり深度でゲートされる**——組み立て時（ツールポリシーの交差）と、呼び出し時（ツール自身の中の権限チェック）。
4. **完了ゲートは常時有効。** スタックのどこにも有効/無効スイッチはなく、入力は予算と判定基準だけです。
5. **フェイルオープンはモデルエラー、解析不能な出力、利用不能な参照に限られ、否定的な判定には決して適用されない。** 解析済みの `RETRY`、`BLOCK`、失敗した証跡行は依然としてブロックします。
6. **子コンテキストは常に独立。** 子は自身の空のメッセージリストから始まり、親のトランスクリプトを継承せず、これを変えるモードも存在しません。
7. **完了キャリアは主張であり、検証済みの結果ではない。** 親ターンが受け取るのは子の自己申告です。セッションに合格証跡がなければ、drain ゲートが検証必須メッセージを追記します。

## 🧭 二つの役割軸

| 軸 | 解決元 | 決定するもの |
|----|--------|--------------|
| **深度ロール**（`SubagentSessionRole`） | ネスト深度 | spawn 権限、制御スコープ |
| **機能ロール**（`FunctionalRole`） | 明示ヒント → `agent_id` 一致 → 設定デフォルト | 子 LLM、ツール許可リスト、プロンプト節 |

**深度ロール。** `resolve_subagent_capabilities(depth, max_depth)` は深度 `0` を `MAIN` と `ControlScope.CHILDREN` に、`depth >= max_depth` を `LEAF` と `ControlScope.NONE` に、その間を `ORCHESTRATOR` と `CHILDREN` に写像します。権限判定の唯一の述語は `can_spawn_children(role)` で、`MAIN` と `ORCHESTRATOR` にのみ真です。深度自体はまず実行レコードから、次にセッションキー内の `:subagent:` の出現回数から解決されます（`get_subagent_depth`）。`max_spawn_depth` の既定値は `2`、ハード上限も `2` です。

**機能ロール。** `_resolve_functional_role(hint, agent_id)` は明示ヒント（未知のヒントは警告を記録して次へ落ちる）、`agent_id` 名、設定された `default_functional_role`、最後に `GENERAL` の順に試します。`GENERAL` は定義を読み込む前に短絡し、これがヒントなし spawn で深度ベースの挙動を保つ理由です。

二つの軸は双方向に直交します：深度 1 の `researcher` は依然として spawn 可能な `ORCHESTRATOR` であり、深度上限の `general` は依然として spawn 不可の `LEAF` です。

## 🧰 機能ロール：定義・読み込み・作用

### 📄 定義ファイル

組み込み定義はパッケージ内に同梱され、追跡・配布可能な形で `agent/tools/subagent/roles/definitions/<name>/AGENTS.md` に置かれます。任意のユーザー上書き（未追跡）は `workspace/subagent_roles/<name>/AGENTS.md` に置けます。解決順は**上書き → パッケージ既定 → なし**で、上書きディレクトリ名は設定可能です。各ファイルは YAML frontmatter（`name`、`description`、`model_tier`、`tools`）と、子プロンプトに追記される markdown 本文を持ちます。`tools: inherit` は「全ツール」に解決され、未知の `model_tier` は `inherit` にフォールバックします。

| ロール | 用途 | 有効 LLM ティア | ロールのツールセット |
|--------|------|------------------|----------------------|
| `general` | 既定 worker；恒等ロール（パッケージ定義は決して読み込まれない） | 深度ロール | 全ツール + ast-grep `ast_grep_search`、`ast_grep_rewrite` |
| `researcher` | 読み取り専用のコードベース・Web 調査 | `auxiliary` | `read_file`、`terminal`、`web_search` + コードインテリジェンス `explore`、`callers`、`callees`、`impact`、`semantic_code_search` + LSP `lsp_goto_definition`、`lsp_find_references`、`lsp_workspace_symbol`、`lsp_call_hierarchy`、`lsp_rename`、`lsp_diagnostics`、`lsp_format`、`lsp_status` + ast-grep `ast_grep_search`、`ast_grep_rewrite` |
| `executor` | 書き込み可能な実装とコマンド実行 | `auxiliary` | `read_file`、`write_file`、`patch_file`、`terminal`、`python_repl` + ast-grep `ast_grep_search`、`ast_grep_rewrite` |
| `reviewer` | 読み取り専用の diff・品質監査 | `auxiliary` | `read_file`、`terminal` + ast-grep `ast_grep_search`、`ast_grep_rewrite` |
| `librarian` | 読み取り専用の外部コードベース検索：サードパーティリポジトリの clone・索引・検索 | `auxiliary` | `read_file`、`terminal`、`web_search` + コードインテリジェンス `explore`、`callers`、`callees`、`impact`、`semantic_code_search` + LSP `lsp_goto_definition`、`lsp_find_references`、`lsp_workspace_symbol`、`lsp_call_hierarchy`、`lsp_rename`、`lsp_diagnostics`、`lsp_format`、`lsp_status` + ast-grep `ast_grep_search`、`ast_grep_rewrite` |

**ast-grep は全ロール共通、コードインテリジェンスは違う。** すべての機能ロール（`general` を含む）は ast-grep の構造検索/リライト `ast_grep_search`、`ast_grep_rewrite` も受け取ります。tree-sitter のコードインテリジェンス `explore` / `callers` / `callees` / `impact` / `semantic_code_search` はシンボル索引を要するため、`researcher` と `librarian` の二つのコード検索ロールに与えられます（`semantic_code_search` はその索引に対する埋め込みベースの概念検索です）。LSP ツール `lsp_goto_definition` / `lsp_find_references` / `lsp_workspace_symbol` / `lsp_call_hierarchy` / `lsp_rename` / `lsp_diagnostics` / `lsp_format` / `lsp_status` も同じくこの二つのロールに与えられ、実行中の言語サーバーを必要とし、子エージェントが必要時に遅延起動しアイドル時に自動停止します。`lsp_rename` と `lsp_format` は既定でプレビューのみ、`lsp_diagnostics` は非同期の診断通知を待ち、`lsp_status` は何も起動せず可用性のみを報告します。

### 🛡️ フェイルオープンローダー

`load_role_definition(role)` は候補パスを順に辿り、最初に存在するファイルを解析し、あらゆる失敗で `None` を返します：ファイルが無い、frontmatter が無い/閉じていない、frontmatter がマッピングでない、`tools` 値が不正、ファイルが読めない。解析失敗ごとに警告が記録され、呼び出し側は `None` を `general` として扱います。ローダーがフェイルオープンになるのは*モデル・ファイル・解析*の問題に対してのみであり——壊れた定義が特権的な定義に変わることは決してありません。`load_all_role_definitions()` は結果をプロセス内でキャッシュします。ワークスペース上書きを編集した後は `invalidate_role_cache()` を呼んでください。

### ⚙️ ロールが駆動するもの

**LLM 選択。** 明示的な `model_override` が最優先。次に `general` 以外のロールの `model_tier` が適用され、それも無ければ深度ロールが決めます（ORCHESTRATOR → メイン LLM、LEAF → 補助 LLM）。

```text
明示的な model_override
  └─► 機能ロールの model_tier（"main" | "auxiliary"）
        └─► 深度ロール（ORCHESTRATOR → メイン LLM、LEAF → 補助 LLM）
```

**システムプロンプト。** `general` 以外のロール（または非空のロール説明/本文）は `## Your Role` に `{ROLE}` 特化行を加え、定義本文を運ぶ `## Role Instructions` 節を追記します。

**ツールポリシー。** ロールの `tools` リストは許可リストになり、許可リスト自体が既に制限しているため既定の拒否リストは空になります。spawn ごとの `extra_tools` はその許可リストに合流し、両者はいずれも無条件の `main_only` メタデータゲートと明示的な拒否リストの対象のままです。

## 🔒 spawn 権限ガード

spawn 権限は二つの独立した地点で執行されるため、単一のリークで昇格することはありません：

- **組み立て時（主ゲート）。** 最終的な許可リストがロール許可リストと `extra_tools` から構築された後、Phase 8.6 がそれを `can_spawn_children(role)` と交差させます：spawn できないロールでは `sessions_spawn` と `sessions_yield` がリストから剥がされます。継承モードではリストが空で既定の拒否リストが既にこのペアを塞いでいるため、交差はそこでは無操作です。
- **呼び出し時（多層防御）。** `check_spawn_permission(session_id)` はまず正規の呼び出し元キーを解決し（子と swarm のキーはそのまま、それ以外の id には `agent:main:session:` を前置）、次に深度を解決し（実行レコード優先、セッションキー形状が次点）、最後に深度ロールを解決して spawn 不可の呼び出し元を拒否します。`(allowed, reason)` タプルを返し、決して例外を投げません。ツールは既存の文字列契約を通じて拒否を描画します。

```text
組み立て：最終許可リスト ∩ can_spawn_children(role)   → sessions_spawn / sessions_yield を剥がす
呼び出し：check_spawn_permission(session_id)          → "status=forbidden"、例外は投げない
```

呼び出し時のガードは `sessions_spawn`（昇格経路）をツール側とランタイム spawn ラッパーの両方で支えます。`sessions_yield` は組み立て時の剥がしのみで保護されます。

## 🧪 完了判定器と goal loop

完了判定器（`spawn/completion_judge.py`）はサブエージェント層のゲートです。温度 `0` の補助 LLM が、タスク、子の最新応答（`8000` 文字に切り詰め）、子セッションの検証証跡サマリを受け取り、`DONE` か `CONTINUE` を一文の理由とともに返します。`CONTINUE` 判定は短い継続プロンプトも運びます。解析は段階的に安全側へ倒れ——まず JSON 修復、次に正規表現スキャン——モデルエラーや使用不能な出力は明示的なフェイルオープン理由とともに `DONE` として確定します。

goal loop は予算が 1 ターンを超えるすべての spawn で実行され、`COMPLETION_JUDGE["goal_max_turns"]`（既定 `5`）を予算とし、最初のターンも算入します。spawn ごとの `goal_max_turns` パラメータがこれを上書きします。`CONTINUE` のとき、判定器のプロンプトは**同じ checkpoint スレッド**上の次の `HumanMessage` として注入されるため、継続は新しい会話ではなく同じ子会話を延長します。

```text
ターン 1 ─► 判定器 DONE ────────────────────────────────────► 確定
         └─► 判定器 CONTINUE（continuation_prompt）─► ターン 2 ─► 判定器 …
                （DONE、継続プロンプトが空、または turns_used == goal_max_turns まで繰り返し）
```

予算を使い切っても実行は `OK` として確定し、`error="goal_loop_budget_exhausted"` が記録されます——このループは枯渇を失敗に変えることはありません。継続プロンプトが空の場合もループは終わります。予算 `1` では子は単一ターンのまま、つまり判定器は一切呼ばれません。

## 🚦 階層化された完了ゲート

四つのゲートが子実行から外側へ積まれます。四つとも常時有効で、入力は予算と判定基準だけです。

| # | 層 | ゲート | 必要な入力 | フェイルオープン時の挙動 |
|---|----|--------|------------|--------------------------|
| 1 | 子実行 | 完了判定器 + goal loop（`spawn/completion_judge.py`） | タスク、最新応答、証跡サマリ；予算 `goal_max_turns`（既定 `5`） | モデルエラー / 解析不能 → `done` |
| 2 | ステップ | TaskFlow ステップ判定器（`agent/tools/taskflow/step_judge.py`） | ステップの `validation_criteria`（無ければ判定器を呼ばない） | モデルエラー / 解析不能 → `pass` |
| 3 | フロー | `taskflow_finish` のゲート A–D（`agent/tools/taskflow/tools/taskflow_finish.py`） | DAG 状態とフロー証跡；ゲート D には `todo` と `plan_path` も必要 | 台帳が読めない / 検証器エラー → 通す |
| 4 | 親ターン | 完了 drain のプログラム的ゲート（`agent/middlewares/subagent_completion_drain/`） | セッションの検証証跡サマリ | 参照失敗 → ゲートを省略 |

**第 2 層——ステップ判定器。** `taskflow_resume` が子結果を注入した後、`validation_criteria` を持つステップは `PASS` / `RETRY` / `BLOCK` と判定されます。`RETRY` はステップのリトライ回数が `STEP_JUDGE["max_retries"]`（既定 `2`）未満なら判定器のフィードバック付きで再ディスパッチし、予算を使い切るとステップをブロックします。判定基準は判定器の入力でありスイッチではありません——基準を持たないステップが判定器に送られることはありません。

**第 3 層——フローゲート。** `taskflow_finish` は次をすべて満たすまで `DONE` への遷移を拒否します：ゲート A が全ステップを `done` または `blocked` と見なし、ゲート B がブロック済みステップを見ず、ゲート C が `FAIL` や `[stale]` のフロー証跡を見ず、そして——呼び出し元が `todo` と `plan_path` の両方を渡した場合に限り——ゲート D が `SisyphusVerifier` を通過すること。ゲート D の連携はスイッチではなく入力です：それが無ければ完了に独立した判定は無く、省略されたゲートは静かなままです。

**第 4 層——親ターンゲート。** drain が待機中の完了キャリアを注入するとき、親セッションの証跡も検査します。キャリアはそのまま注入され、セッションに合格証跡が無ければ検証必須メッセージがその後ろに追記されます。この検査は無条件で——どの設定でも無効化できず——証跡参照自体が利用不能なときにのみフェイルオープンします。

解析された否定的判定は常にブロックします：フェイルオープンが覆うのはモデルエラー、解析不能な出力、利用不能な参照であり、実際の `RETRY`、`BLOCK`、失敗した証跡行ではありません。ゲート内部の詳細（ブレーカー閾値、announce 再試行、証跡台帳）は[暴走ループ防止 README](../loop-prevention/README.ja.md) と[長期タスク README](../long-running-tasks/README.ja.md) にあります。

## 🔗 他サブシステムとの関係

| サブシステム | この境界を越えるもの | 詳細 |
|--------------|----------------------|------|
| TaskFlow エンジン | ステップディスパッチ、`validation_criteria`、`aggregate_deps`、フローゲート A–D | [長期タスク](../long-running-tasks/README.ja.md) |
| 暴走ループ防止 | 完了ゲート、判定器予算、drain と announce の再試行挙動 | [暴走ループ防止](../loop-prevention/README.ja.md) |
| コンテキストエンジン | MesMemory に永続化されるのは完了キャリアのみ；子のトランスクリプトは checkpoint のみ | [Context Engine README](../../context_engine/README.ja.md) |
| ランタイムレーン | `SUBAGENT` レーンが同時子実行を制限；超過 spawn は `PENDING` で待機 | [暴走ループ防止](../loop-prevention/README.ja.md) |
| メモリファイル | 非空 drain は `MEMORY.md` と `USER.md` を再調整（読み込み → 永続化、フェイルオープン） | [サブエージェントシステム README](../../agent/tools/subagent/README.ja.md) |

### 🧵 合成集約

`taskflow_run_task(aggregate_deps=True)` は合成ステップを印付けし、ディスパッチされるタスクテキストに依存ステップの記録済み結果を載せます。フラグはステップに保存され、`build_task_with_dep_results(step, steps, results)` が `depends_on` 順に `## Upstream Results` ブロックを追記します。各依存につき `### {step_id}` 節を一つ、依存の `child_session_key` をフローの `{child_session_key, result, result_hash}` レコードと照合します。記録が無い依存は `no result recorded` プレースホルダを寄与します。集約はディスパッチや再試行のたびに安定したレコードから再導出されるため、保存済みステップタスクが書き換わることは決してありません。フラグが無ければタスクテキストは不変です。完全な挙動は[長期タスク README](../long-running-tasks/README.ja.md) にあります。

## ⚙️ 設定

| つまみ | 場所 | 既定値 | 効果 |
|--------|------|--------|------|
| `goal_max_turns` | `config/features/agent_side/completion_judge.py` | `5` | goal loop のターン予算（最初のターンを含む） |
| spawn ごとの `goal_max_turns` | `sessions_spawn` パラメータ | `None` → 設定値 | 単一 spawn の予算を上書き |
| `max_retries` | `config/features/agent_side/step_judge.py` | `2` | ステップがブロックされるまでの `RETRY` 回数 |
| `default_functional_role` | `SubagentConfig` | `"general"` | ヒントも `agent_id` 一致も解決しない場合のフォールバック |
| `roles_override_dir_name` | `SubagentConfig` | `"subagent_roles"` | 任意のロール上書きを置くワークスペースディレクトリ |
| `max_spawn_depth` | `SubagentConfig` | `2` | ロールが `LEAF` に反転する深度（ハード上限 `2`） |
| `verify_commands` | `config/features/agent_side/evidence_ledger.py` | test / lint / build / typecheck / format のコマンド群 | 検証証跡として自動記録されるコマンド |

## 🗺️ テストマップ

| 領域 | テスト |
|------|--------|
| ロール enum とローダー | `tests/agent/tools/subagent/types/test_functional_role.py`、`tests/agent/tools/subagent/roles/test_loader.py` |
| ロール駆動の spawn 挙動 | `tests/agent/tools/subagent/spawn/test_functional_role_integration.py`、`tests/agent/tools/subagent/spawn/test_system_prompt_role.py` |
| spawn 権限ガード | `tests/agent/tools/subagent/test_spawn_privilege_guard.py` |
| 完了判定器と drain | `tests/agent/tools/subagent/test_completion_judge.py`、`tests/agent/tools/subagent/test_completion_drain.py` |
| 親ターンゲート | `tests/agent/middlewares/test_completion_drain_gate.py` |
| ステップ判定器 | `tests/agent/tools/taskflow/test_step_judge.py`、`tests/agent/tools/taskflow/test_resume_with_judge.py` |
| フローゲート | `tests/agent/tools/taskflow/test_finish_gate.py` |
| 合成集約 | `tests/agent/tools/taskflow/test_synthesize.py`、`tests/agent/tools/taskflow/test_synthesize_e2e.py` |

## ⚠️ 限界

- すべての判定器は設計上フェイルオープンです：到達不能または解析不能な判定器は `done` / `pass` として確定します。可用性が厳格さに優先します——詰まった判定器が実行を閉じ込めてはなりません。
- `goal_max_turns` が `1` の子は単一ターンとなり、その spawn では完了判定器が呼ばれません。
- 呼び出し時の権限ガードが支えるのは `sessions_spawn` です；`sessions_yield` は組み立て時の剥がしのみに依存します。
- ゲート D は `todo` と `plan_path` の両方が揃ったときだけ有効です；その連携が無ければ `taskflow_finish` に独立した完了判定はありません。
- 親ターンゲートは構造化台帳ではなく、テキストの証跡サマリとその `FAIL` / `[stale]` マーカーに基づいて推論します。
- `aggregate_deps` は生の依存結果を連結するだけで要約しません。そのため巨大な結果はディスパッチされるタスクテキストを膨らませます。
- 子コンテキストの独立は設計上の固定事項です：親トランスクリプトを継承するモードは存在しません。

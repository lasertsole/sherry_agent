# 🔁 LDW リプレイ

[English](README.md) · [中文](README.zh.md) · **日本語** · [한국어](README.ko.md)

> 検証済み TaskFlow 計画のワークフロー単位リプレイです。ステップ DAG は構造化メタデータへコンパイルされ、vendored の [langchain-dynamic-workflow](https://github.com/OWNER/langchain-dynamic-workflow) コアが内容ハッシュ journal で実行します。LDW リプレイが失敗すると、同じ検証済み DAG から新しい TaskFlow flow を自動的に作成し、成功した結果を journal へ書き戻します。

情報源: `vendor/ldw/**`、`agent/tools/ldw/**`、`agent/tools/subagent/ldw_shim.py`、`config/features/agent_side/ldw.py`、`tests/agent/tools/ldw/**`、`tests/full/test_ldw_real_plan_e2e.py`、`tests/agent/tools/subagent/test_ldw_shim.py`。

## 🎯 概要

LDW リプレイは、すでに TaskFlow で成功した計画の**第二の実行経路**です:

| 経路 | 粒度 | キャッシュ | トリガー |
|---|---|---|---|
| TaskFlow | step / subagent | evidence ledger + registry | 通常の計画実行 |
| PTC リプレイ | step 内のツール呼び出し | skill スクリプト | executor spawn のプリフィル |
| LDW リプレイ | step DAG | prompt + agent_type + model + schema で鍵付けした journal | リプレイ回数がしきい値に達した後の `workflow_replay` ツール |

LDW コアは `vendor/ldw/` に全体を vendored し、import 時に最小の `deepagents` stub を導入します。Sherry 自身の LangGraph leaf runnable を使うため、LDW の sandbox/leaf 構築モジュールは呼ばれません。

## 🧭 トリガーとツール

main agent には main-only の `workflow_replay` ツールがあります:

```json
{"action": "status", "variant_input": "", "flow_id": "", "plan_key": "", "force": false}
```

- `status` はリプレイ回数、最小しきい値、コンパイル済みスクリプトのパス、現在の session に読める TaskFlow DAG があるかを返します。
- `compile` は現在の TaskFlow flow DAG から `ldw__<plan_key>.py` を生成します。
- `replay` はリプレイ回数を増やし、`LDW["min_replays"]` 未満なら TaskFlow のまま、それ以上なら LDW を実行します。LDW の部分失敗は自動的に TaskFlow 降格へ入ります。
- 返り値の JSON は構造化されています: `status=ok|taskflow|degraded|partial|no_workflow|no_plan|disabled|error` と `failed_steps`、`journal_hits`、`leaf_calls`、`degraded`、該当する repair フィールド。`ok` は `ok`、`taskflow`、`degraded` で真です。

リプレイしきい値は意図的に遅延させています。低頻度の計画が LDW の組み立てコストを払うことはありません。計画はしきい値（既定 3）に達した時点で現在の TaskFlow flow からコンパイルされ、skill review が事前にコンパイルするわけではありません。

`variant_input` は現在のタスクの具体入力です。各 step の prompt に入るため journal key にも入り、同じ計画でも変体が違えば古いキャッシュに当たりません。step 接尾辞も `agent_type` の一部です。PTC スクリプトが step 単位だからです。

## 🧱 コンパイルと成果物

`compile_plan_to_workflow(plan_knowledge)` は JSON 永続化可能なメタデータを返します:

```json
{"plan_key": "...", "flow_id": "...", "steps": [{"step_id": "step-1", "task_template": "...", "agent_type": "executor@step-1", "deps": [], "response_schema": null, "step_timeout_seconds": null, "retry_policy": null}]}
```

`build_orchestrate(meta, variant_input)` は LDW の `orchestrate` 呼び出し可能オブジェクトを作ります。各 `DagNode` は前駆結果を受け取り、prompt には `[Current task input]`、`[Hard constraints]`、`[Previous step results]` が入ります。これにより内容ハッシュ journal が変体や上流出力の変化でも正しくなります。schema 付き step は `ctx.agent(..., schema=...)` を呼び、leaf は自由文ではなく検証済みの構造化オブジェクトを返します。

成果物は所有 skill の最上位 `scripts/` に置きます:

```text
skills/auto/<owning-skill>/scripts/ldw__<plan_key>.py
skills/auto/<owning-skill>/scripts/ptc__<plan_key>__<step_id>.py
```

LDW スクリプトは `PLAN_KEY`、`REQUIRED_AGENT_TYPES`、`WORKFLOW_META` を Python モジュール定数として保存します。クロージャ、ソース文字列、`workflow.json` のサイドカーは保存しません。書き込み側は JSON テキストではなく Python リテラルを使います。`json.dumps` は小文字の `true` / `false` / `null` を出すため、`additionalProperties: false` を含む schema ではスクリプトが読み込めなくなるからです。

## ⚙️ ランタイムと Journal

- `register_roster(meta, session_id)` は **step ごとに 1 つの builder** を登録します。builder により LDW は schema 束縛の runnable を要求でき、`agent_type` の step 接尾辞が journal と PTC 検索を step 単位に保ちます。
- leaf は実際の child 契約を再利用します。`_assemble_child_tools` がロール allow-list を適用し、code intelligence、ast-grep、executor 専用 PTC を注入します。session id は runnable config を通り、共有グラフへ焼き込まれません。
- leaf のモデルはロール tier と `step_model` 上書きに従います。schema 付き leaf は thinking を強制 off にし、child agent の構造化出力の最終段と揃えます。
- journal miss のとき executor leaf はまず `run_ptc` でその step の PTC スクリプトを実行できます。PTC 失敗は `[PTC Pre-run Progress]` を追加して ReAct へ続行します。PTC の schema miss は失敗であり、成功にはなりません。
- `src/data/workflow_runs.db` の `SqliteWorkflowStore` がプロセス間再開を支えます。同じ prompt / agent_type / model / schema の再リプレイは journal 結果を返し、leaf 呼び出しはゼロです。各結果は `journal_hits` と `leaf_calls` を報告します。
- 計画ごとのロックが同時リプレイを直列化します。`LDW["real_replay_timeout_s"]` は 1 回の LDW 実行を制限し、タイムアウトはハングではなく失敗・降格候補になります。

## 🪜 自動 TaskFlow 降格

LDW 実行が失敗し `LDW["auto_degrade_enabled"]` が有効なとき、アダプターは**同じ検証済み DAG** から実行を再構築します。古い flow を reset したり再実行したりはしません。

1. 新しい `ldw-<plan_key>-<nonce>` TaskFlow flow を作り、元 flow がまだ存在すれば `resumed_from=<origin>` を付けます。
2. 非 None の LDW 結果をすべて DONE step として、合成 `ldw-journal:<flow>:<step>` child key と結果レコード付きで予置します。失敗または未完了の step だけが ready/blocked のままです。
3. ready step を通常の TaskFlow で dispatch するため、retry policy、schema gate、StepJudge、child 完了の意味は通常実行と完全に同じです。
4. `taskflow_wait_all` でポーリングし、settled child ごとに `taskflow_resume` を呼びます。タイムアウトまたは kill された child は failure として宣言されます。`auto_dispatch` が次の波を開始します。
5. すべて完了した flow は通常どおり finish します。finish 拒否や未完了作業がある場合は flow を終端（fail または cancel）し、活動中の孤児 flow を残しません。
6. 同じ prompt/agent_type/model/schema key で TaskFlow の成功結果を LDW journal へ書き戻すため、その変体の次の LDW リプレイはゼロ呼び出しになります。

step 失敗時は `repair_extraction` が replay flow の child handoff を読み、実際の成功経路と失敗分岐を計画知識へマージし、`success-steps.json` を永続化し、LDW スクリプトを再コンパイルします。Repair は fail-open です。永続化や再コンパイルのエラーは結果に返りますが、リプレイ自体をクラッシュさせません。PTC スクリプトは skill-review コンパイラの所有で、LDW repair は自分の成果物だけを再コンパイルします。

## 🔧 設定

| キー | 既定値 | 効果 |
|---|---|---|
| `LDW["enabled"]` | `True` | マスタースイッチ。False なら全リプレイを TaskFlow のままにします |
| `LDW["min_replays"]` | `3` | 計画が TaskFlow を離れて LDW スクリプトをコンパイルするまでのリプレイ回数 |
| `LDW["script_max_bytes"]` | `65536` | 1 つの `ldw__<plan_key>.py` の上限 |
| `LDW["journal_path"]` | `src/data/workflow_runs.db` | プロセス間 journal の位置 |
| `LDW["auto_degrade_enabled"]` | `True` | LDW 失敗後に TaskFlow replay flow を作成・駆動する |
| `LDW["repair_enabled"]` | `True` | 降格失敗後に repair 抽出 / 再コンパイルを行う |
| `LDW["real_replay_timeout_s"]` | `900.0` | 1 回の LDW 実行の実時間上限 |
| `LDW["max_failed_steps"]` | `5` | 失敗 step がこれを超えたら降格を拒否する |
| `PTC["ptc_prefill_enabled"]` | `False` | LDW leaf が使う任意の PTC スクリプトを有効化する |

## 🔒 制限

- これはリプレイ加速器であり、sandbox でも再試行エンジンでもありません。
- LDW leaf はロールのツールポリシーを継承します。LDW の sandbox/deepagents 経路は意図的に使いません。
- 計画や変体が変われば別の journal key になり再実行されます。古いスクリプトが現在の prompt より優先されることはありません。
- 再実行した失敗 step の副作用はロールバックされません。journal は以前成功した step だけをスキップします。
- 降格時に成功 step が再度 spawn されることはありません。失敗 step は TaskFlow の retry/judge に従い、LDW 独自の再試行ループはありません。

## 🗺️ テスト

| 領域 | テスト |
|---|---|
| Vendored コア import + DAG + journal + 失敗伝播 | `tests/agent/tools/subagent/test_ldw_shim.py` |
| メタデータ / 依存 / 変体 / schema prompt | `tests/agent/tools/ldw/test_compiler.py` |
| スクリプトリテラル / 往復 / 不安全 key | `tests/agent/tools/ldw/test_scripts.py` |
| しきい値 / SQLite journal / 自動降格結果 | `tests/agent/tools/ldw/test_replay.py` |
| Leaf ツール面 / schema / PTC 成功とフォールバック | `tests/agent/tools/ldw/test_leaf.py` |
| 降格の予置 / resume / journal 書き戻し | `tests/agent/tools/ldw/test_taskflow_adapter.py` |
| Repair マージ / 永続化 / 再コンパイル | `tests/agent/tools/ldw/test_repair.py` |
| Main-only ツール面と degraded 状態 | `tests/agent/tools/ldw/test_tool.py` |
| 実 TaskFlow + 実モデルリプレイ / 2 回目 / 降格 / repair / 実 agent | `tests/full/test_ldw_real_plan_e2e.py` |

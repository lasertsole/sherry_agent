# 🔁 LDW リプレイ

[English](README.md) · [中文](README.zh.md) · **日本語** · [한국어](README.ko.md)

> 検証済み TaskFlow plan の workflow レベルリプレイです。step DAG を構造化メタデータにコンパイルし、vendored の [langchain-dynamic-workflow](https://github.com/OWNER/langchain-dynamic-workflow) コアが内容ハッシュ journal で実行します。[PTC](../ptc/README.md) のツール呼び出しレベル prefill と補完関係にあります。

情報源：`vendor/ldw/**`、`agent/tools/ldw/**`、`agent/tools/subagent/ldw_shim.py`、`config/features/agent_side/ldw.py`、`tests/agent/tools/ldw/**`、`tests/agent/tools/subagent/test_ldw_shim.py`。

## 🎯 概要

LDW リプレイは、TaskFlow で成功済みの plan に対する**第二の実行経路**です。

| 経路 | 粒度 | キャッシュ | トリガー |
|---|---|---|---|
| TaskFlow | step / subagent | evidence ledger + registry | 通常の plan 実行 |
| PTC リプレイ | step 内のツール呼び出し | skill スクリプト | executor spawn prefill |
| LDW リプレイ | step DAG | prompt + agent_type + model + schema の journal | replay 回数がしきい値に達した後の `workflow_replay` ツール |

LDW コアは `vendor/ldw/` に vendoring され、import 時に最小の `deepagents` stub をインストールします。sherry は独自の LangGraph leaf runnable を使うため、LDW の sandbox/leaf 構築経路は呼びません。

## 🧭 トリガーとツール

main agent には main-only の `workflow_replay` ツールがあります。

```json
{"action": "status", "variant_input": "", "flow_id": "", "plan_key": "", "force": false}
```

- `status` は replay 回数とスクリプトパスを返します。
- `compile` は現在の TaskFlow flow DAG から `ldw__<plan_key>.py` を生成します。
- `replay` は回数を増やし、`LDW["min_replays"]` 未満なら TaskFlow のまま、しきい値以上で LDW を実行します。

しきい値は意図的に lazy です。低頻度 plan に LDW の初期化コストを払わせません。既定では 3 回目の replay で現在の TaskFlow flow からコンパイルし、skill review が事前コンパイルすることはありません。

`variant_input` は現在のタスク入力で、各 step prompt に入り journal key を分けます。step 接尾辞も `agent_type` に含まれます（PTC スクリプトが step 単位のため）。

## 🧱 コンパイルと成果物

`compile_plan_to_workflow(plan_knowledge)` は JSON 永続化可能なメタデータを返します。

```json
{"plan_key": "...", "flow_id": "...", "steps": [{"step_id": "step-1", "task_template": "...", "agent_type": "executor@step-1", "deps": [], "response_schema": null}]}
```

`build_orchestrate(meta, variant_input)` が LDW の `orchestrate` を構築します。各 `DagNode` は前駆結果を受け取り、prompt に `[Current task input]` と `[Previous step results]` を載せるため、変体や上流出力が違えば journal key も変わります。

成果物は所有 skill の `scripts/` 直下に置きます。

```text
skills/auto/<owning-skill>/scripts/ldw__<plan_key>.py
skills/auto/<owning-skill>/scripts/ptc__<plan_key>__<step_id>.py
```

LDW スクリプトは `PLAN_KEY`、`REQUIRED_AGENT_TYPES`、`WORKFLOW_META` をモジュール定数として持ちます。closure、source 文字列、`workflow.json` sidecar は保存しません。

## ⚙️ 実行と journal

- `register_roster(meta, session_id)` は step ごとに session-bound runnable を作り、`session_id` を runnable config で渡します。
- leaf は role のツール許可リストで構築した LangGraph グラフです。session を共有グラフに焼き込みません。
- journal miss 時、executor leaf はまず step の PTC スクリプトを `run_ptc` で実行できます。PTC 失敗は hard gate で ReAct にフォールバックします。
- `SqliteWorkflowStore`（`src/data/workflow_runs.db`）が cross-process resume を支えます。同じ prompt / agent_type / model / schema の再 replay はゼロ呼び出しです。
- 同じ plan の replay は per-plan ロックで直列化します。

## 🪜 降級と修復

LDW には step retry/judge がありません。

- 失敗した DAG ノードは `None` になり、依存先は skip されます。
- 旧「dag 再実行」L2 は削除済みです。root 失敗、失敗率 >50%、失敗 step に依存先がある場合は **L2 TaskFlow 降級**へ直行し、それ以外の失敗 leaf は partial を受け入れます。
- TaskFlow 降級は失敗 step だけを再実行します。成功 step は journal で skip し、成功結果は journal に書き戻します。
- 修復抽出は実際の成功経路と failed_branches を plan knowledge にマージし、条件を満たせば LDW スクリプトを再コンパイルします。`merge_knowledge`、`extract_react_repair_experience`、`extract_failure_experience`、`repair_extraction` を提供します。

## 🔧 設定

| キー | 既定 | 効果 |
|---|---|---|
| `LDW["min_replays"]` | `3` | LDW workflow をコンパイルする前の replay 回数 |
| `LDW["script_max_bytes"]` | `65536` | `ldw__<plan_key>.py` 1 本の上限 |
| `LDW["journal_path"]` | `src/data/workflow_runs.db` | cross-process journal のパス |
| `PTC["ptc_prefill_enabled"]` | `False` | LDW leaf が使う PTC スクリプトを有効化 |

## 🔒 制限

- これはリプレイ高速化であり、sandbox でも retry エンジンでもありません。
- LDW leaf は role のツールポリシーを継承します。LDW の sandbox/deepagents 経路は意図的に未使用です。
- plan / 変体が変わると journal key が変わり再実行されます。古いスクリプトが現在の prompt に優先することはありません。
- 最初の 2 回は明示的に TaskFlow のままです。3 回目以降だけ LDW コストを払います。
- LDW 実行が失敗した場合、ツールは partial/error を返し、呼び出し側は TaskFlow に戻ります。skip を成功扱いしません。

## 🗺️ テスト

| 範囲 | テスト |
|---|---|
| vendored core の import + DAG + journal + 失敗伝播 | `tests/agent/tools/subagent/test_ldw_shim.py` |
| メタデータ / deps / 変体 prompt | `tests/agent/tools/ldw/test_compiler.py` |
| しきい値 / SQLite journal / compile-on-miss | `tests/agent/tools/ldw/test_replay.py` |
| 修復マージ / 抽出経路 | `tests/agent/tools/ldw/test_repair.py` |
| main-only ツール面 | `tests/agent/tools/ldw/test_tool.py` |
| 実 main agent が `workflow_replay` を呼ぶ | `tests/full/test_ldw_replay_e2e.py` |

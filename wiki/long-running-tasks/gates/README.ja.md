# 🚦 判定ゲート — ステップ／フローが「完了」と言えるのは誰の判定か

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> [長時間タスク](../README.ja.md)の一部：「結果が返ってきた」から「このステップ／フローは完了」までのすべてのゲート — 呼び出し側が宣言する結末、Tier 1 スキーマゲート、失敗分類器、補助 LLM の StepJudge、`taskflow_finish` の A–E — と、それらの優先順位。

申告は事実ではありません。子サブエージェントが「完了しました」と言っても、意味があるかどうかを決めるのはエンジンです。境界ごとに二つのレベルで判定します：

| レベル | 問い | ゲート |
| :--- | :--- | :--- |
| **ステップ**（`taskflow_resume`） | この結果は呼び出し側の要求を満たしたか？ | 宣言された `step_outcome` → Tier 1 スキーマ → 失敗分類器 → Tier 2 StepJudge |
| **フロー**（`taskflow_finish`） | この DAG は本当に完成したか？ | A DAG 完全性 → B 未解決ステップ → C エビデンス → E 目標判定 |

**ソースコード:** `agent/tools/taskflow/tools/taskflow_resume.py`、`agent/tools/taskflow/tools/taskflow_finish.py`、`agent/tools/taskflow/tools/_retry.py`、`agent/tools/taskflow/step_judge.py`、`agent/tools/taskflow/evidence_collector.py`、`config/features/agent_side/step_judge.py`、`config/features/agent_side/goal_gate.py`。以下の定数・判定・拒否文言はすべてこのコードに対して検証済みです。

## 優先順位（覚えるべき唯一の規則）

**呼び出し側が宣言した結末はすべてのゲートに優先し、それ以外に他のゲートを短絡するものはありません。** `step_outcome` は呼び出し側が実行の終わり方を述べる手段です — `failure` はステップを `failed` にして判定器を完全に飛ばし、`skipped` は `skipped` にし（依存先は `skipped` へ連鎖）、`success` / `partial` は結果に記録したうえでゲートは通常どおり走ります。未知の値は状態変更の前に拒否されます。以下はすべて、呼び出し側が何も宣言しなかった場合の話です。

## ステップレベル（`taskflow_resume`）

### 0. 失敗検出 — そもそもこれは失敗なのか？

Tier 1 スキーマ検証が**先に走り**（下記参照）、`schema_validated` を産出します。その結果は失敗分類器が消費する二つの信号の一つです — もう一つは結果テキスト：

- **構造。** `schema_validated=false`（Tier 1 の出力）はテキスト分類が見られない失敗で、ここで再ディスパッチされます — **判定呼び出しの前**なので、壊れた形が判定トークンを消費することはありません。
- **結果テキスト。** `classify_failure`（`_retry.py`）はパターン表に対する小文字の部分文字列走査で、否定された言い回しを事前に除去します（`cannot connect` を接続失敗と読んではいけません）。パターン表にない言い回しの失敗は捕捉されません — 分類器はヒューリスティックであり判定ではありません。

`retry_policy` を持つステップは、分類された失敗を決着させずに代替の子サブエージェントへ再ディスパッチします（ポリシーの予算が続く限り）。`taskflow_wait_all` はテキストを返さなかった死んだ子を分類できないため、予算が残っている限り必ず一つ消費します。ポリシー未設定＝失敗を記録してステップは確定します（呼び出し側が別段の指示をしていなければ `failed`）。

### Tier 1 — 構造（無料）

ステップが `response_schema` を宣言している場合、結果はパースされ（呼び出し側が `structured_result` を渡したならそれ、なければ結果テキスト）、そのスキーマで検証されます（`validate_structured_output`）：

- **不一致**はエラーとともに `schema_validated=false` として記録され、失敗として扱われ、リトライ種別は `schema_error` です — 判定呼び出しは一切消費せず、リトライされなかった不一致はステップを `failed` にします。
- **不一致はリトライに教えます。** schema の失敗は判定より前に短絡するため `judge_feedback` は空です。そこで置換子エージェントのタスクは検証器自身のメッセージ（`Your previous output failed schema validation: <フィールドパス, expected/got>.`）を `## Previous Attempt Feedback` として運び、子はステップの spawn 契約一式（`response_schema` → 構造化出力の要件、機能ロール、モデル上書き、実行タイムアウト）を継承します — schema も失敗理由も見えないリトライは推測するしかありません。
- **形は生成時にも拘束されます。** ステップが `response_schema` を宣言すると、子エージェントは LangChain の `response_format` 付きで構築されます（生の schema は `AutoStrategy` に解決され、プロバイダーが対応していればネイティブな構造化出力、なければ構造化出力ツール呼び出しになります）。明らかな不一致はこの検証器ではなくモデルの側で止まり、schema 本文は子の system prompt にプロンプトレベルのフォールバックとして残り、この Tier 1 チェックは第二の網として残ります — プロバイダーのエッジケースや構造化出力ツールを呼ばないモデルは、ここで捕まります。`sessions_spawn` は同じ `output_schema` フィールドを公開するので、ツール駆動の子も同様に拘束できます。
- **一致**は `structured_result` + `schema_validated=true` を保存し、Tier 2 へ進みます。構造だけで内容が正しいことは決して証明されません — これが第二層の存在理由そのものです。

### Tier 2 — 意味論（補助 LLM）

ステップに `judge_criteria`（または旧フィールド `validation_criteria`）があると、`judge_step_result`（`step_judge.py`、temperature 0）が結果を審査し（検証済みの `structured_result` があればそれ、なければ結果テキスト）、三つの判定のいずれかを返します：

| 判定 | 効果 |
| :--- | :--- |
| **`pass`** | ステップを `done` にし、依存先を解放します |
| **`retry`** | 共有の `_retry` シームで再ディスパッチし、ステップ自身の `retry_count` 予算（`STEP_JUDGE["max_retries"]`、既定 2）を再利用、判定器の指針を `## Previous Attempt Feedback` として代替タスクに付加します（`with_judge_feedback`）。予算を使い切るとステップは `blocked` になります |
| **`block`** | 判定器の理由付きでステップを `blocked` にします（呼び出し側の判断：修正・その枝の取消・再計画） |

挙動を変える細部：

- **エビデンス要約が同乗します。** `STEP_JUDGE["evidence_aware"]`（既定オン）なら、プロンプトはそのフローの `collect_evidence_summary` を運びます — 子が記録した検証行と、そこから導出される陳腐性です（[エビデンス台帳](../gates/evidence-ledger/README.ja.md)参照）。
- **常にフェイルオープン。** モデルエラーやパース不能な応答は `pass` に降格します：到達不能な判定器が進行を妨げることはありません。代償は明示的で、判定はベストエフォートであり、基準の充足がハード保証になることはありません。
- **確定したステップだけ。** 判定器はステップが実際に確定したときに走ります（`redispatched_key is None`）。リトライポリシーで再ディスパッチされたステップは基準を保持したまま、最終の resume で判定されます。
- **`taskflow_resume` に `validation_criteria` を渡すと**その呼び出しの保存値を**上書き**します — 子が実際に何をしたかに基づいて締めたり正したりするためです。
- **結果テキストの予算。** `STEP_JUDGE["max_result_chars"]`（8000）が判定プロンプトの見る範囲を定めます。

### そして：`done` は「設定されたゲートを通過した」という意味

ステップの `done` は、それ自体で「作業が正しい」ということにはなりません。**何の**期待も設定されていなければ（`response_schema`、`judge_criteria` / `validation_criteria`、`retry_policy`）、古い意味 —「結果が注入された」— に降格し、エラーを報告した子でも `done` になり後続を解放できます。期待を宣言する（`response_schema` は無料で、判定呼び出しの前に検証されます）か `step_outcome="failure"` を報告すれば、閉ループが捕まえます：`failed` / `skipped` / `cancelled` は実際のステップ状態になり、失敗は依存先を決して解放せず、`taskflow_finish` は未解決ステップを抱えた完了を拒否します。

## フローレベル（`taskflow_finish`）

`taskflow_finish` は DONE 遷移を四つの検査で順にゲートします：

| ゲート | 検査 | 拒否理由 |
| :--- | :--- | :--- |
| **A — DAG 完全性** | すべてのステップが `done` または `blocked` | `step(s) not done/blocked` |
| **B — 未解決ステップなし** | `failed`・`skipped`・`cancelled`・`blocked` のステップが無い | 該当ステップを id で示し、記録された理由を添える |
| **C — エビデンス** | フローのエビデンスに `FAIL`、`[broken]`、`[stale]` 行が無い（[エビデンス台帳](../gates/evidence-ledger/README.ja.md)参照） | `failing verification evidence` / `confirmed regression` / `stale evidence` |
| **E — 目標判定** | 収集された結果がフローの `description` を満たすかを補助モデルが判定 | `taskflow_replan` を名指しする拒否 |

すべてのゲートは**フェイルオープン**です：読めない collector、モデルエラー、パース不能な判定は完了を妨げず通過させます。Gate E は `description` を持ち、ステップ数が `GOAL_GATE["min_steps"]`（3）以上の場合にのみ走ります — 1〜2 ステップのフローは子レベルの[完了判定器](../../subagent/README.ja.md)がすでに覆っており、同じ仕事への二つ目の判定はまさにその重複だからです。`GOAL_GATE["enabled"]` はそのマスタースイッチ、`GOAL_GATE["max_result_chars"]`（4000）は渡される各結果の窓です。

## 隣接するゲート（別ページ、同じ考え方）

このページが持つのは TaskFlow のゲートです。「申告は事実ではない」という規則は他所でも繰り返され、それぞれの場所に記録されています：

- **[エビデンス台帳](../gates/evidence-ledger/README.ja.md)** — 記録された検証トレイル。その要約は Gate C の入力であり、StepJudge プロンプトのエビデンス節でもあります。
- **[サブエージェント完了ゲート](../../subagent/README.ja.md)** — 子レベルの層：CompletionJudge ゴールループ（DONE / CONTINUE、`COMPLETION_JUDGE["goal_max_turns"]` で有界。子の台帳が通過済みで陳腐化していない行を示すときは最初の呼び出しをスキップ）と完了 drain のプログラム的ゲート（`SubagentCompletionDrainMiddleware`、親側）。
- **[TodoList 強制層](../todolist/enforcement/README.ja.md)** — 計画側：E1–E7 強制層（ターン終了時の継続ゲートとその予算を含む）。
- **ループ防止** — [wiki/loop-prevention](../../loop-prevention/README.ja.md) が各ガードの高度マトリクス（ターン／ステップ／サブエージェント／パイプライン）と層ごとのフェイルオープン方針をまとめています。

## 設定

| ノブ | 既定 | 効果 |
| :--- | :--- | :--- |
| `STEP_JUDGE["max_retries"]` | 2 | 判定器が費やせる `retry` 判定数（ステップ自身の `retry_count` 予算） |
| `STEP_JUDGE["max_result_chars"]` | 8000 | 判定プロンプトの結果テキスト窓 |
| `STEP_JUDGE["evidence_aware"]` | True | エビデンス要約を判定プロンプトに含めるか |
| `GOAL_GATE["enabled"]` | True | Gate E のマスタースイッチ |
| `GOAL_GATE["min_steps"]` | 3 | Gate E が走る最小フロー規模 |
| `GOAL_GATE["max_result_chars"]` | 4000 | 目標判定プロンプトの結果ごとの窓 |

## テスト

`tests/agent/tools/taskflow/` がこの層を固定します：`test_validation.py`（基準の保存、判定、上書き、フェイルオープン）、`test_step_judge.py`（解析、判定の正規化、プロンプト組み立て）、`test_resume_with_judge.py`（resume と判定：pass / retry / block、リトライフィードバック注入、予算枯渇）、`test_finish_gate.py`（ゲート A–C）、`test_retry_policy.py`（分類と再ディスパッチ）、`test_expectation_closure.py`（期待→実績の閉ループ規則）、`test_chain_smoke.py`（判定 → リトライフィードバック → ゴールループ → 完了ゲートをスタブ補助 LLM に対して通すスモーク）。

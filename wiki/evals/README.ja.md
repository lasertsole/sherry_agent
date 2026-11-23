# 評価

このツリーは、エージェントを**外部**ベンチマークでどう測るか、そして各結果が何を
証明し、何を証明しないかを記録します。製品ドキュメントとは意図的に分けています。
ベンチマークは harness・モデル・エージェントを同時に測るため、スコアは能力の
パーセンテージではありません。

## スイート

| スイート | 測るもの | エントリ |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | 隠しテストパッチと benchmark 自身の grader を使う外部コーディング課題。既定の経路は docker または podman で benchmark のタスクイメージを実行する | `evals/deepswe/suite.py` |
| [DevBench（SWE-bench Verified）](../../evals/devbench/README.md) | 公式 F2P/P2P リストをイメージ内 pytest で判定。agent はホスト側の `/testbed` コピーを編集 | `evals/devbench/suite.py` |
| [Multi-SWE-bench](../../evals/multi_swe_bench/README.md) | devbench コアで ByteDance の多言語データを実行(`--lang` フィルタ、イメージはレコードから) | `evals/multi_swe_bench/suite.py` |
| [TAU-bench](../../evals/tau_bench/README.md) | 模擬ビジネス API を sherry ツールへラップ。記録された呼び出し+出力で汎用判定 | `evals/tau_bench/suite.py` |
| [GAIA](../../evals/gaia/README.md) | 汎用アシスタント課題、ホストモード、全ツール面。公式の回答正規化 | `evals/gaia/suite.py` |
| [CyberSecEval 系](../../evals/cybersec/README.md) | セキュリティ選択式/コード分析、ホストモード、選択肢の文字一致 | `evals/cybersec/suite.py` |
| [WebArena](../../evals/webarena/README.md) | docker-compose スタックを常駐。browser ツール保持。URL 系評価をネイティブ判定 | `evals/webarena/suite.py` |
| [VisualWebArena](../../evals/visualwebarena/README.md) | WebArena + ビジョン差分(JS-evaluate 有効、vision-model 証跡を報告) | `evals/visualwebarena/suite.py` |
| [OSWorld](../../evals/osworld/README.md) | Ubuntu タスクコンテナ、TERMINAL-FIRST(計画どおり VNC は降級)。vm_* 状態評価をネイティブ判定 | `evals/osworld/suite.py` |

共有基盤は `evals/shared/`: データセット解決、バッチ+再開、統一レポート(計画の token/文脈命中率/code-intel サマリ付き)、compose を含むコンテナのライフサイクル、2 つのレポートアナライザ(`context_analyzer`、`code_intel_tracker`)。これらは製品層 `pub.func.session_report` アナライザの薄いアダプタで、アルゴリズムは共有しつつ eval 側のレポート形式は維持します。token の集計は製品の `session_tokens()` に委譲し、追跡コードの重複はありません。全スイートは契約としてデータオプション：データセット・コンテナ実行環境・キーの欠落は大声で SKIP(exit 3)、黙通過はしません。
| コーディング能力 | 自作のコーディング課題と同梱プロンプト arm | `evals/coding_capability/` |
| 外部ベンチ | ブリッジされたコーディングベンチと harness アダプタ | `evals/` |

## 測定ルール

- 選択したスライスで **N of M** を報告し、パーセンテージをリーダーボード順位として
  提示しない。
- **harness** の結果と**モデル**の結果を分ける。provider エラー、テストパッチの
  未適用、タイムアウトは「修正失敗」ではない。
- **arm**（中立プロンプト、コーディングテンプレート、
  delegation-pressure）、モデル、git revision、正確なコマンドを記録する。
- クリーンな diff も判定の一部：`tests/` 配下の変更や PASS_TO_PASS の破壊は、
  対象テストが通っても成功ではない。
- メイン session だけの token/cost はその旨を明記する。子 session の使用量は
  現在すべてのレポートで集計されているわけではない。

## 実行

```bash
# DeepSWE のコンテナ経路（benchmark のタスクイメージ、docker または podman）：
uv run --no-sync python evals/deepswe/suite.py --preflight-only  # イメージ取得 + verifier ビルド + GOLD チェック（モデル呼び出しなし）
SHERRY_EVAL_CONTAINER_BIN=podman uv run --no-sync python evals/deepswe/suite.py --timeout 1800

```

`SHERRY_EVAL_CONTAINER_BIN` はエンジンを固定します（`docker` / `podman`。既定は docker を
優先し、なければ podman）。`--preflight-only` はモデル呼び出しを使わずにコンテナ経路全体
（イメージ取得・verifier ビルド・GOLD 自己チェック）を検証します。採点実行には
`MAIN_LLM_API_KEY` が別途必要です。podman ではタスク／verifier コンテナを
`--http-proxy=false` で作成し（podman はホストのプロキシを全コンテナに注入するため、
死んだプロキシがあると benchmark 自身の localhost テストが落ちます）、verifier は
`--network none` でビルドします（WSL2 には nftables がなく、ビルド時の bridge を作れません）。

各実行は `evals/results/deepswe/<run_id>/` に `eval_report.json` とタスクごとの diff を
書き出します。ディレクトリは gitignore され、この wiki には安定した結論だけを残します。

## harness が強制するもの

DeepSWE は実際のメインエージェントを動かすため、harness はルールを提示したうえで
**検証**します。プロンプトを信じるだけではありません。

- **オーケストレーションゲート** — ファイル編集や子エージェントの spawn の前に、
  `todowrite` と TaskFlow の flow 1 つ（`taskflow_flow` の `action="plan"`）が必要です。
  満たさない場合、変更系ツールはルールを返します。使用量はツール別に集計され、
  `orchestration_compliant` として報告されます。
- **受け入れチェックリスト** — タスク文の判定可能な条項（*must* / *raise* / *reject* /
  *only* / *charset* などの規範語）を番号付きチェックリストにしてプロンプトへ入れ、
  ゲートが各 `sessions_spawn(functional_role="reviewer")` のタスクにも追記します。
  これにより検証は計画への同意ではなく、条項ごとの PASS/FAIL になります。
- **プラン継続** — 本番の自動ターントリガはサーバーの hook なので、harness が自ら
  駆動します。各ターン後にサンドボックスのプラン registry を読み、未完了項目がある間は
  上限付きの `[PLAN CONTINUATION]` ターンを送ります（最大 3 回）。diff が変わらない
  継続は即座に停止します（`no_progress_stop`）。
- **レポート項目** — `orchestration`（ポリシー、タスク別カウント、ゲートカウンタ、
  `acceptance_checklist`、`reviewer_checklist_injections`）、`continuations`、
  `no_progress_stop`、そして verifier の判定と patch。

既知のギャップ：コンテナ経路は各ターンを `asyncio.wait_for` で制限します。実行中の
provider 呼び出しに対する強制終了はなく、期限を超えて残る可能性があります（削除済みの
`--local` モードの worker プロセス強制終了が修正の参考）。

## タスク 1–14 の実測

ディレクトリ順の最初の 14 タスクを、2026-11-22 のコンテナ実行
（`20261122_044351` … `20261122_191618`）で測定した。「最良」はそのタスクの
最も良い実行であり、`F2P` は benchmark の fail-to-pass 集合、`P2P` は緑を
保たねばならない pass-to-pass 回帰集合、編排（オーケストレーション）は
`todowrite` と TaskFlow の flow の両方を使ったことを示す。

| # | タスク | 言語 | 最良 F2P | P2P | 実時間 | 編排 |
|---|---|---|---|---|---|---|
| 1 | abs-module-cache-flags | Go | **20/20 — resolved** | 3/3 | 3243 s | ok |
| 2 | abs-stepped-slices | Go | 5/6 | 6/6 | 1859 s | ok |
| 3 | actionlint-action-pinning-lint | Go | **55/55 — resolved** | 145/145 | 446 s | VIOLATED（flow なし）|
| 4 | adaptix-name-mapping-aliases | Python | 38/44 | 2738/2738 | 1608 s | ok |
| 5 | aiomonitor-task-snapshots-diff | Python | 51/53 | 8/8 | 2241 s | ok |
| 6 | anko-default-function-arguments | Go | 0/2 | 119/119 | 1468 s | VIOLATED（flow なし）|
| 7 | anko-typed-variable-bindings | Go | 7/9 | 94/94 | 787 s | ok |
| 8 | arcane-drift-detection-baselines | Go | **82/82 — resolved** | 2/2 | 2072 s | ok（todo 66）|
| 9 | arktype-json-schema-refs-dependencies | TypeScript | 23/25 | 1679/1679 | 2147 s | ok |
| 10 | awilix-async-container-initialization | TypeScript | 19/24 | 162/162 | 870 s | ok |
| 11 | bandit-incremental-cache-control | Python | 79/88 | 275/275 | 1989 s | ok |
| 12 | bandit-interprocedural-taint-checks | Python | 65/66 | 293/293 | 949 s | ok |
| 13 | bandit-structured-nosec-directives | Python | 65/69 | 281/282 | 1238 s | ok |
| 14 | boa-hierarchical-evaluation-cancellation | Rust | 0/17 | 7/7 | 2196 s | ok |

この表が示すこと：

- **14 タスク中 3 タスクが resolved**（21%）。さらに 5 タスクは数項目差
  （12: 65/66、13: 65/69、5: 51/53、9: 23/25、11: 79/88）。
- **F2P は二値ではなく勾配**である。部分点は「機能の大半を実装した」と
  「何もしなかった」を分離する。resolved の 1 ビットでは両者が混ざる。
- **予算は個別の修正より重要**。同じモデルで、タスク 1 は 1800 s のターン予算では
  9/20、7200 s では 20/20 だった。時間切れの時点でも進捗していた。
- **VIOLATED の 2 行は実記録**。タスク 3 と 6 は `terminal` だけで作業しており、
  当時はシェルツールが編排ゲートの対象外だった。
- **回帰の信号が 1 件**。タスク 13 は P2P を 1 件落とした（281/282）。隠しテストが
  挙動変化を検出した。
- タスク 10–13 の初回は実行されていない。provider が HTTP 402（残高不足）を返し、
  各タスクは約 20 秒・ツール呼び出しゼロで失敗した。これは provider 障害であり、
  モデルの結果ではない。
- タスク 14 の初回は harness 失敗（16.9 秒）で、再実行は正常だった。現在は単発の
  イメージ取得失敗を再試行し、スイープ全体を中止しない。

## 結果

- [DeepSWE](../../evals/deepswe/README.md) —— ローカルモードの結果と失敗分析は
  `evals/deepswe/RESULTS.md`。

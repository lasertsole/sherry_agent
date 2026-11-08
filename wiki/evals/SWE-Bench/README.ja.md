# SWE-Bench（host harness）

SWE-Bench は公式 Docker 評価ではなく、リポジトリ自身の host harness で実行します。
harness は各インスタンスの base commit を固定し、環境を作り、インスタンス自身の
FAIL_TO_PASS / PASS_TO_PASS で判定します。答えるのは「この agent がこのマシンで
この 2 つの HARD インスタンスを修正できたか」であり、「リーダーボード順位は何か」
ではありません。

## 測るもの

- `server.service.messages.async_generate` を通した**メイン agent**。実際の
  ミドルウェアチェーンと選択されたツール面を使う。
- それが spawn する**サブ agent**。ファイル書き込みと、harness が diff を読む前の
  決着を含む。
- **harness**：インスタンス初期化、公式テストパッチ、正確なテスト判定。harness
  エラーは invalid として記録し、修正失敗とはしない。

## Harness とスライス

- データセット：`princeton-nlp/SWE-bench_Lite`、test split。
- HARD スライス：`django__django-17051` と `django__django-16820`。`sympy__sympy-20639` は 2026-11-08 に実行スライスから削除しました。test patch が NumberSymbol と Symbol の分母に関する旧来の意味論を変更しており、その選択は issue と修正前テストから導出できないためです。Sympy の過去実行は `evals/swebench/RESULTS.md` に残しています。
- インスタンスキャッシュ：`--instance-cache-dir /tmp/swebench-instance-cache`。
- Arm：中立プロンプト、`--persona coding`、delegation pressure
  （`SHERRY_EVAL_DELEGATION_PRESSURE=1`）。delegate-only arm とそのミドルウェアは
  2026-11-02 に削除され、古い結果行は履歴としてのみ残します。
- レポートは `evals/results/swebench/<run_id>/eval_report.json`。インスタンス diff は
  同じディレクトリにある。

- 2026-11-04 以降、タスク prompt はインスタンスの test patch と FAIL_TO_PASS
  名を（読み取り専用で）含みます。checkout は実行前に test patch でパッチされ、agent が対象テストを実行できます。prompt は両方のセットの正確なコマンドも示します（Django は `python tests/runtests.py <labels>`、それ以外は `python -m pytest <node ids>`）。判定時にファイルをリセットしてから再適用し、レポートは `target_tests_exposed` と `tests_preapplied` を記録します。
- 決定論的デコード：各 eval ターンは temperature 0 に固定した `built_agent` で構築されます。
  `SHERRY_EVAL_MAX_ITERATIONS` はメインのモデル呼び出し上限を任意に引き上げます
  （未設定/0 は出荷時の 180）。レポートは `model.temperature` と
  `budget.main_max_iterations` を記録します。

## 失敗の履歴（2026-11-01）

| 実行 | Arm | 結果 | 何が起きたか |
| :--- | :--- | :--- | :--- |
| 2026-11-01 000025 | プロンプト + no-edit ゲート | 1/2 汚い | Django がテストファイルを追加；Sympy の 726B patch が P2P を破壊 |
| 2026-11-01 002305 | delegate-only | 2/2 クリーン | メインに直接ツールなし；子が実装と検証 |
| 2026-11-01 143952 | 最初のソフトゲート（10/20） | 1/2 | ゲートが spawn を強制した後、メインが patch を繰り返して P2P を破壊 |
| 2026-11-01 174750 | ソフトゲート（8/16、累積） | 2/2 クリーン | 16 回の読み取り後にメインの読み書きを停止し、実装を委譲 |
| 2026-11-03 010944 | 7/14 助言型（累積） | 0/2 | Django は条件だけの変更で一意制約エラーが残り、Sympy は diff なしで 90 回の呼び出し予算を消費 |
| 2026-11-03 194320 | ネットワーク橋渡し + シグネチャ連続（Sympy のみ） | 0/1 | 非空 diff だが `test_pretty_sqrt` の P2P が回帰。連続警告は WARN のみで 90 回予算を消費 |
| 2026-11-03 203547 | + 正確なシグネチャ BLOCK（Sympy のみ） | 0/1 | 約 20 回でブロックが効いたが、agent は `probe1.py`…`probe19.py`（出力同一・引数違い）に切り替え、90 回を使い切った |
| 2026-11-03 205218 | + 結果反復（Sympy のみ） | 0/1 | terminal/python_repl は 6 回の同一結果でブロック。agent は 72/90 で patch し、その後検証できなかった |
| 2026-11-03 210023 | + patch_file 成功でブロック解除（Sympy のみ） | 0/1 | 28 回のクリーンな実行でガードは不作動。P2P は通過、F2P は NumberSymbol vs Symbol の隠れた意味論を外した（既存の `test_issue_6285` は旧挙動を断言） |
| 2026-11-04 003521 | + 対象テストを prompt に提示（HARD 2） | 0/2 | テストは見えるが実行できず、両インスタンスが 90 回を使い切った |
| 2026-11-04 004714 | + 対象テストを checkout に事前適用（HARD 2） | 1/2 | Django は通過。Sympy は `e.is_Symbol`（`1/x_17` には誤り）を使い、repro スクリプトでループ |
| 2026-11-04 005721 | 失敗後の指示を強化（Sympy のみ） | 0/1 | 同じ誤った条件のまま。F2P は 0 |
| 2026-11-08 111407 | HARD は Django のみ、prompt に F2P+P2P | 1/1 | Django 通過。初回は P2P の `NoneType` 回帰で 0/1、2 回目で通過 |
| 2026-11-08 133112 | temperature=0 + prompt に F2P/P2P | 隔離失敗 | 温度 pin が workspace 上書きより前に `config.path` を import し、live workspace で実行された。1/1 は集計しない |
| 2026-11-08 133743 | pin 順序修正後の temperature=0 | 1/1 | Django 通過。17 回呼び出し / 60.6 秒。F2P/P2P とも通過、一時ファイルなし |
| 2026-11-08 135604 | temperature=0、有効な反復は計 4 回 | 4/4 | すべて通過。呼び出し回数は 16～82 回（60.6～286.3 秒）。最長は 90 回予算のうち 82 回を使用 |
| 2026-11-08 145011 | 新 HARD インスタンス `django__django-16820`、temperature=0 | 0/1 | gold check は通過（F2P 5 + P2P 91）。エージェントは 92 回で 90 回予算を使い切り、F2P は通過、P2P は未通過 |
| 2026-11-08 184144 | 同インスタンス、`SHERRY_EVAL_MAX_ITERATIONS=180` | 1/1 | 24 回呼び出し / 96.5 秒で通過。上限拡大は打ち切りの天井を外したが、修正の原因ではない（同温度でも軌跡が大きく異なる） |

## 露呈した問題

1. **旧イテレーション予算はツールペアを二重に数えた。** ツール呼び出しは
   `AIMessage` に 1、`ToolMessage` に 1 を消費し、90 は約 45 回のツールループに
   見えた。現在はモデル呼び出しだけを数える。
2. **`terminal` はループ検出の盲点。** `ToolGuardrails` は冪等ツールだけをハッシュ
   する。`terminal` は非冪等なので、同一コマンド 32 回でも no-progress 判定にならない。
3. **「情報が足りた」に収束点がなかった。** 変更箇所を特定した後も検証を続けるか、
   上流 PR を探しに行った。編集と検証へ移ることを強制する仕組みがなかった。
4. **評価 harness がテストファイル編集を拒否しなかった。** クリーン diff は製品契約の
   一部だが、`changed_paths` は記録されるだけだった。テストファイルは対象テストを
   通っても汚い成功になる。この穴は閉じた。`dirty_test_edits` が harness の
   patch 以外でエージェントが変更したテストファイルを検出し、テスト結果に
   かかわらず `resolved=False` にする。
5. **書くことは検証ではない。** 最初の no-edit arm は patch を作ったが既存テストを
   実行せず、P2P 回帰は判定時に初めて分かった。
6. **子の結果と token が観測しにくい。** タイムアウトした子の checkpoint は消えて
   いることがあり、メインは状態確認のため再 spawn し、レポートはメイン session の
   token だけを数えていた。
7. **計測用の pin がサンドボックスを壊しうる。** `SHERRY_WORKSPACE_DIR` の上書き前に
   eval の接合点を import すると、`WORKSPACE_DIR` が live persona ディレクトリに
   固定された。実行は 1/1 と報告したが、`eval_workspace_has_sessions=false` と
   `live_sessions_unchanged=false` が露呈させた。レポートの isolation ブロックが、
   そのような実行を能力集計から排除するゲートである。

### 現在の解決

1. **修正済み。** `IterationBudget` はモデル呼び出しごとに 1 単位。ツール結果は無料。
2. **修正済み。** `ToolGuardrails` にステータス非依存のシグネチャ連続（ツール + 引数ハッシュ、3 回の同一呼び出し、terminal は自身の metadata で 5）を追加し、1 ターン最大 3 回警告した後、その正確なシグネチャをブロックします。さらに非冪等ツールの結果反復（3 回警告、6 回連続同一結果でツールをブロック）も持ちます。`patch_file` 成功時（metadata `clears_guardrail_blocks`）はブロック・連続・失敗カウンタを消去し、検証を再開できます。読み取り専用コマンド分類器は不要です。
3. **ツールガードによる緩和であり、ハードな終了ゲートではない。** ブロックされたシグネチャは別の方法を強制し、20261103_210023 の Sympy 実行は 28 回のモデル呼び出しで収束して隣接テストファイルを実行し、prompt もそれを要求します。「終了前にテスト通過必須」の独立ゲートは存在しません。
4. **未解決。** harness は `changed_paths` を記録するが `tests/` の編集を invalid にしない。ソフトゲートは確率を下げるが公式のガードではない。
5. **部分的に緩和。** escalated リマインダーは spawn 後の主エージェントの patch ループを止め実装を委譲するよう促すが、「終了前にテスト通過必須」のハードゲートはない。
6. **部分的に修正。** ターンごとの token 予算はメイン + 全階層の子孫をルートターンに帰属する。eval レポートは依然メイン session のみを数え、子 checkpoint/結果の読み戻しは未解決。

## no-edit ゲートとその失敗

旧 eval ゲートは 15 回のツール呼び出しで警告し、イテレーション予算の 70% で調査を
ブロックした。書き込みはどれでもリセットした。これにより 3 つの失敗が予測できた：

- 書き込みを**ツール名**（`patch_file` / `write_file`）で検出したため、
  `terminal cp` や heredoc 編集は見えなかった。Django はまさに `cp` でテスト
  ファイルを作った。
- **どんな**書き込みも成功とみなした。`tests/` 配下の書き込みや、すぐ戻した patch
  も含む。
- 最初の書き込みでそのターンの残りはゲートが無効になった。Sympy の最初の誤った
  patch の後、約 30 回の terminal 探針と P2P 回帰は野放しだった。

削除済みであり、収束機構にはできない。

## read-before-write ライセンスが最初のソフトゲートのループを止めなかった理由

`agent/tools/pub_base/read_state.py` のライセンスが守るのは **`write_file` の上書き**
である。既存ファイルは、それを読んだ session だけが置換でき、読み取り revision が
CAS の前提になる。これは **`patch_file` には適用されない**。`patch_file` は
old_string/new_string、曖昧一致、stale-write CAS で置換し、事前の読み取りを要求せず、
どちらのツールも patch が正しいかテストが通るかを知らない。

最初のソフトゲート（10/20）は製品書き込みが入った時点でブロックを解除し、
`patch_file` を対象にしていなかった。メインは researcher を spawn し、ゲートが
リセットされ、その後 `compiler.py` に約 70 回の `patch_file` を試した。これは
**patch リトライの病理**であり、未ライセンスの `write_file` 上書きではないため、
ライセンスは発火しなかった。以前の 8/16 ゲートは `patch_file` もブロックしており、
2026-11-01 の実行はそうして収束した。現在の 7/12 助言型設計はブロックを
意図的に撤去している。

## 現在の設計：DelegationPressureGate（7/12 助言型）

2026-11-01 の実行は以前の 8/16 ハードブロック版を測定したもので、以下の
7/12 助言型動作が現在の設計です。2026-11-03 の 0/2 再実行は 7/14 設定の測定で、HARD スライスは解決できませんでした——`evals/swebench/RESULTS.md` を参照。

`agent/middlewares/delegation_pressure/core.py`：

- 1 ターン内の**読み取り/探索**呼び出しを累積する。
- **7** 回で、ゲート自身の mid-turn `abefore_model` 経由で `<system-reminder source="delegation_pressure">` 警告を 1 回注入する。
- **12** 回で、より強い escalated システムリマインダーを注入する。ツールは利用可能なままで、ゲートは助言し、ブロックしない。
- ブラウザのデバッグツールは計数対象外。
- accepted/pending の spawn が成功するとカウントをリセットし、
  メインエージェントは検査/検証でき、その後の 7/12 で再び作動する。失敗した
  spawn と書き込みだけではリセットされず、`after_agent` もターン境界でリセットする。

これはメインエージェントにのみインストールされる。子ミドルウェアビルダーは追加せず、
`_active` も `:subagent:`/`:swarm:` セッションを拒否する。

## ネットワーク状態ブリッジ

失敗がネットワーク由来の terminal コマンドはツールエラーになります: `curl` /
`wget` / `http` / `https` / `ssh` / `scp` / `sftp` / `ping` / `dig` / `nslookup`
の非ゼロ終了、または `git clone|fetch|pull|push|ls-remote|remote|submodule` の
非ゼロ終了は `ToolException` を raise します。`handle_tool_error` が
`ToolMessage(status="error")` に変えるため、exact-failure カウンタが認識します。
ローカルコマンド（pytest、コンパイラ、スクリプト）の非ゼロ終了は通常のテキスト
（`status="success"`）のまま返り、agent は失敗を読んで反復できます。`web_fetch`
の HTTP 4xx/5xx も同様に raise します。

eval harness は実行前に GitHub をプローブし、到達できない場合はシードされた
`AGENTS.md` に「ネットワークを探り続けない」directive を追加します。これで Sympy
の curl/GitHub 探査ループは解消しました。残る Sympy の F2P 失敗は
NumberSymbol vs Symbol の隠れたテスト意味論であり、ネットワーク探査ではありません。

## 結果

| Arm | Resolved | 時間 | メイン session tokens | 推定コスト |
| :--- | :--- | :--- | :--- | :--- |
| `DelegationPressureGate` 8/16（run 20261101_174750） | 2/2 | 1693.4s | 1,962,778 / 12,673 | $1.000 |
| `DelegationPressureGate` 7/14 助言型（run 20261103_010944） | 0/2 | 669.7s | 8,370,656 / 31,119 | $4.232 |
| Delegate-only（run 20261101_145717） | 2/2 | 3469.0s | 2,561,580 / 20,925 | $1.312 |

Django は製品コードとリリース文書だけを変更し、Sympy はライブラリ 1 ファイルだけを
変更した。どちらの成功 diff にもテストファイルはない。token/cost はメイン session
のみで、子の使用量は集計されていない。したがって 2 つの arm の比較は時間と
メイン文脈のシグナルであり、完全な請求額ではない。

## 再現

```bash
SHERRY_EVAL_DELEGATION_PRESSURE=1 uv run --no-sync python evals/swebench/suite.py \
  --hard --persona coding --keep-instances \
  --instance-cache-dir /tmp/swebench-instance-cache
```

## 注意

- 2 インスタンスはシグナルであり、ベンチ順位ではない。一般的な改善を主張する前に
  同じスライスを繰り返すこと。
- host harness は公式 Docker harness ではない。Python と依存のバージョンが
  ベンチマークイメージと異なる。
- クリーンな結果はテスト編集ガードを閉じていない。harness は `changed_paths` の
  `tests/` 配下を依然として invalid にすべきである。
- ブラウザツールは設計上除外した。この実行はブラウザ駆動の調査について何も
  証明しない。

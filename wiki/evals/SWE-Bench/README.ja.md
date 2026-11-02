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
- HARD スライス：`django__django-17051`、`sympy__sympy-20639`。
- インスタンスキャッシュ：`--instance-cache-dir /tmp/swebench-instance-cache`。
- Arm：中立プロンプト、`--persona coding`、delegation pressure
  （`SHERRY_EVAL_DELEGATION_PRESSURE=1`）。delegate-only arm とそのミドルウェアは
  2026-11-02 に削除され、古い結果行は履歴としてのみ残します。
- レポートは `evals/results/swebench/<run_id>/eval_report.json`。インスタンス diff は
  同じディレクトリにある。

## 失敗の履歴（2026-11-01）

| 実行 | Arm | 結果 | 何が起きたか |
| :--- | :--- | :--- | :--- |
| 2026-11-01 000025 | プロンプト + no-edit ゲート | 1/2 汚い | Django がテストファイルを追加；Sympy の 726B patch が P2P を破壊 |
| 2026-11-01 002305 | delegate-only | 2/2 クリーン | メインに直接ツールなし；子が実装と検証 |
| 2026-11-01 143952 | 最初のソフトゲート（10/20） | 1/2 | ゲートが spawn を強制した後、メインが patch を繰り返して P2P を破壊 |
| 2026-11-01 174750 | ソフトゲート（8/16、累積） | 2/2 クリーン | 16 回の読み取り後にメインの読み書きを停止し、実装を委譲 |

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
   通っても汚い成功になる。この穴はまだ開いており、今回の成功は偶然テストを
   編集しなかっただけである。
5. **書くことは検証ではない。** 最初の no-edit arm は patch を作ったが既存テストを
   実行せず、P2P 回帰は判定時に初めて分かった。
6. **子の結果と token が観測しにくい。** タイムアウトした子の checkpoint は消えて
   いることがあり、メインは状態確認のため再 spawn し、レポートはメイン session の
   token だけを数えていた。

### 現在の解決

1. **修正済み。** `IterationBudget` はモデル呼び出しごとに 1 単位。ツール結果は無料。
2. **緩和済み。** `DelegationPressureGate` は 7 回目の読み取りで `<system-reminder>` 警告、14 回目で escalated 版を注入し、`terminal`/読み取りツールは利用可能なままです。`ToolGuardrails` は非冪等 terminal をハッシュせず、読み取り専用コマンド分類は未実装。
3. **部分的に緩和。** escalated リマインダーは 14 回目で `sessions_spawn` を促しますが、ツールをブロックしなくなりました。対象テスト実行を要求する専用の検証ゲートは未実装。
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
2026-11-01 の実行はそうして収束した。現在の 7/14 助言型設計はブロックを
意図的に撤去している。

## 現在の設計：DelegationPressureGate（7/14 助言型）

2026-11-01 の実行は以前の 8/16 ハードブロック版を測定したもので、以下の
7/14 助言型動作は 2026-11-02 に競合解消して確定した設計です。

`agent/middlewares/delegation_pressure/core.py`：

- 1 ターン内の**読み取り/探索**呼び出しを累積する。
- **7** 回で、ゲート自身の mid-turn `abefore_model` 経由で `<system-reminder source="delegation_pressure">` 警告を 1 回注入する。
- **14** 回で、より強い escalated システムリマインダーを注入する。ツールは利用可能なままで、ゲートは助言し、ブロックしない。
- ブラウザのデバッグツールは計数対象外。
- accepted/pending の spawn が成功するとカウントをリセットし、
  メインエージェントは検査/検証でき、その後の 7/14 で再び作動する。失敗した
  spawn と書き込みだけではリセットされず、`after_agent` もターン境界でリセットする。

これはメインエージェントにのみインストールされる。子ミドルウェアビルダーは追加せず、
`_active` も `:subagent:`/`:swarm:` セッションを拒否する。

## 結果

| Arm | Resolved | 時間 | メイン session tokens | 推定コスト |
| :--- | :--- | :--- | :--- | :--- |
| `DelegationPressureGate` 8/16（run 20261101_174750） | 2/2 | 1693.4s | 1,962,778 / 12,673 | $1.000 |
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

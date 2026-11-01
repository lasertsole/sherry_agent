# 評価

このツリーは、エージェントを**外部**ベンチマークでどう測るか、そして各結果が何を
証明し、何を証明しないかを記録します。製品ドキュメントとは意図的に分けています。
ベンチマークは harness・モデル・エージェントを同時に測るため、スコアは能力の
パーセンテージではありません。

## スイート

| スイート | 測るもの | エントリ |
| :--- | :--- | :--- |
| [SWE-Bench](SWE-Bench/README.md) | 実際のリポジトリ issue を、そのインスタンス自身の FAIL_TO_PASS / PASS_TO_PASS で判定 | `evals/swebench/suite.py` |
| コーディング能力 | 自作のコーディング課題と同梱プロンプト arm | `evals/coding_capability/` |
| 外部ベンチ | ブリッジされたコーディングベンチと harness アダプタ | `evals/` |

## 測定ルール

- 選択したスライスで **N of M** を報告し、パーセンテージをリーダーボード順位として
  提示しない。
- **harness** の結果と**モデル**の結果を分ける。provider エラー、テストパッチの
  未適用、タイムアウトは「修正失敗」ではない。
- **arm**（中立プロンプト、コーディングテンプレート、delegate-only、
  delegation-pressure）、モデル、git revision、正確なコマンドを記録する。
- クリーンな diff も判定の一部：`tests/` 配下の変更や PASS_TO_PASS の破壊は、
  対象テストが通っても成功ではない。
- メイン session だけの token/cost はその旨を明記する。子 session の使用量は
  現在すべてのレポートで集計されているわけではない。

## 実行

```bash
# SWE-Bench HARD スライス、環境変数で現在の eval arm を選択：
SHERRY_EVAL_DELEGATION_PRESSURE=1 uv run --no-sync python evals/swebench/suite.py \
  --hard --persona coding --keep-instances \
  --instance-cache-dir /tmp/swebench-instance-cache
```

各実行は `evals/results/swebench/<run_id>/` に `eval_report.json`、インスタンスごとの
diff、`sandbox/` を書き出します。ディレクトリは gitignore され、この wiki には
安定した結論だけを残します。

## 結果

- [SWE-Bench](SWE-Bench/README.md) —— 失敗履歴、no-edit ゲートの事後分析、
  read-before-write ライセンスが最初のソフトゲートの patch ループを止めなかった理由、
  8/16 `DelegationPressureGate` の結果。

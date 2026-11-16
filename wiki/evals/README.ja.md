# 評価

このツリーは、エージェントを**外部**ベンチマークでどう測るか、そして各結果が何を
証明し、何を証明しないかを記録します。製品ドキュメントとは意図的に分けています。
ベンチマークは harness・モデル・エージェントを同時に測るため、スコアは能力の
パーセンテージではありません。

## スイート

| スイート | 測るもの | エントリ |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | 隠しテストパッチと benchmark 自身の grader を使う外部コーディング課題。`--local` は Docker 不要のホストモード | `evals/deepswe/suite.py` |
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
# DeepSWE の既定スライスを Docker なしのホストモードで実行：
uv run --no-sync python evals/deepswe/suite.py --local --timeout 1200
```

各実行は `evals/results/deepswe-local/<run_id>/` に `eval_report.json` と
タスクごとの diff を書き出します。ディレクトリは gitignore され、この wiki には
安定した結論だけを残します。Docker モードは benchmark のイメージを使うため、
ローカルモードとは別の経路です。

## 結果

- [DeepSWE](../../evals/deepswe/README.md) —— ローカルモードの結果と失敗分析は
  `evals/deepswe/RESULTS.md`。

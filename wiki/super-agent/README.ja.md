# スーパーエージェント

GitHub issue や Feishu トピックのメッセージを追跡可能な作業に変換し、Sherry の
worker サブエージェントに委任して、元のチャンネルへ結果を報告する、別プロセスの
オーケストレーターです。専用の SQLite 状態を持ち、狭いループバック接缝を通じて
のみ Sherry と通信します。

## 別プロセスである理由

Sherry は実行単位、スーパーエージェントは管理者です。分離することで、管理者の
SQLite 状態、tmux セッション、毎時のチェックは Sherry の再起動後も生存し、
Sherry がダウンしても「Trigger を入隊し続け、何も失わない」という縮退に留まり、
ワークフローは止まりません。スーパーエージェントは Sherry のコアコードを
決してインポートせず、`/internal/superagent/*` ループバックルートのみを呼びます。

## アーキテクチャ

```
GitHub / Feishu  →  Sources  →  Engine  →  Effects  →  Sherry workers
                                  ↓          ↓
                             SQLite Store   Outbox → channel replies
```

Engine は Trigger、Thread、Decision、Ticket の唯一の書き込み者です。各コマンドは
一つの SQLite トランザクションであり、アダプター（Sources、Executor、Outbox、
Assessor）は非同期で、狭いポートを通じて Engine と通信します。

- **Sources** は外部イベントを Trigger に翻訳し、返信を送ります。
- **Engine** は五つの部品を一方向に実行します：Intake → Evals → Tickets →
  Assignments → Replies。
- **Effects** は永続的で合体・リトライ可能なキューです——すべての副作用は
  再起動後も生存します。
- **Outbox** はチャンネル Receipt の唯一の書き込み者で、順序と冪等性を保証します。
- **Passes** は 2 秒ごとに期限切れ Effects、続いて期限切れ Evaluations を実行します。

## 認可と評価

Policy は admission 表と `off`、`observe`、`auto` の三つの accept mode により、
Trigger が明示的か推論的かを決定します。推論された作業は、auto モードとルールが
両方有効な場合のみ受け入れられます。評価は専用の Sherry セッションに委任され、
有界な thread ウィンドウを読んで Decision を返します。HITL の回答が拒否の場合は
fail closed で blocked Ticket になります。

## 設定とライフサイクル

`superagent.jsonc` は状態ディレクトリ、Sherry ループバック URL、パネルポート、
Source 設定を保持します。`scripts/superagent` は専用 tmux セッションで
`start`、`stop`、`restart`、`status` を提供し、終了コードは 0（健全）、
1（未実行／実行時エラー）、2（部品エラー）、3（設定未完了）です。毎時のチェックは
これらのコードを読み、1 のときのみ再起動します。完全な運用手順は
[deployment](deployment.md) を参照してください。

## パネル

Nuxt の `/superagent` ページはループバックのパネル API をポーリングし、六つの
タブを提供します：Overview、Sources、Triggers、Evals、Execution、Settings。
API はループバック保護（host + origin + リクエストヘッダー）されており、他の
オリジンのページからは駆動できません。設定にはリビジョンがあり、古い保存は拒否されます。

## テストとゲート

`tests/superagent/` はパッケージをミラーし、ドメインモデル、Engine、Policy、
Effects、Outbox、Sources、パネルの単体テストに加え、End-to-End チェーンを
持ちます：GitHub issue → Ticket → PR と Feishu mention → Ticket（いずれも CLI と
worker を mock）。静的ゲートは `ruff`、`basedpyright`、`lint-imports` の階層契約、
ドキュメントゲートは parity、links、language です。

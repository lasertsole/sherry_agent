# 🛡️ 高リスク操作の保護

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> 不可逆な操作をどう識別し、一度だけ承認し、再生時に拒否し、サブエージェントからメインエージェントの人間承認へバブルするか。

情報源: `agent/tools/pub_base/high_risk_guard_store.py`、`agent/tools/pub_base/high_risk_policy.py`、`agent/tools/high_risk_guard.py`、`agent/middlewares/tool_guardrails/core.py`、`agent/middlewares/humanInTheLoop/strategies.py`、`config/features/agent_side/high_risk_guard.py`、`server/trigger/http/high_risk_guard.py`、`client/app/pages/home/components/HighRiskGuardPanel.vue`。

## 🎯 脅威モデル

検証の再実行、ツールの再試行、再ディスパッチされた TaskFlow ステップは、同じ**不可逆**操作を二度実行し得ます。本番デプロイ、決済の課金、強制プッシュ。ソース編集の再実行は通常の作業なので、「非冪等」だけではトリガーになりません — ガードは**高リスク**を基準にします。

| 層 | 答えること | 仕組み |
| :---- | :------ | :-------- |
| L1 メタデータ | どのツールが不可逆な結果を起こし得るか | `metadata.high_risk`(MCP は既定で真) |
| L2 キーワード台帳 | どの操作を人間に見せるか | 組み込み + session/global キーワード、部分文字列スキャン |
| L3 実行済み操作台帳 | この正確な呼び出しは既に走ったか | `(ルートセッション, ツール, args_hash)` 行 + 初回結果 |
| L4 承認 | 初回実行を誰が承認するか | メインの HITL カード、サブエージェントの `[NEEDS_APPROVAL]` バブル |

各層は独立しています。人間が初回を承認しても台帳は再生を拒否し、キーワード一致は台帳に記録が無い段階でも先に確認します。

## ⚙️ キーワード台帳

1 つの SQLite テーブル(`src/data/high_risk_guard.db` の `high_risk_keywords`)が組み込みの上書きとカスタムキーワードを保持します。キーワードは**大文字小文字を無視した部分文字列**で一致し(単語境界なし: `password` は `user_password=…` に一致)、各キーワードは**3 文字以上**(Unicode 文字)でなければなりません — 2 文字の項目はほぼすべての日本語文に一致してしまいます。

| フィールド | 意味 |
| :---- | :------ |
| `session_id` | セッション id、`global`、または組み込み既定 |
| `keyword` | スキャン対象のテキスト |
| `policy` | `block`(スキャン/確認) または `allow`(スキャンしない) |
| `source` | `builtin`、`user`(パネル)、`agent`(ツール) |
| `executed` | 1 = このセッションで実行済み |
| `result_summary` | 初回結果。再生拒否時に表示 |

解決順は session → global → 組み込み既定です。`allow` 行は、別スコープが `block` でもキーワードを有効リストから外します。組み込みリストは資格情報、金融識別子、破壊的なシェル操作、破壊的な SQL を対象にします(`rm -rf`、`mkfs`、`git push --force`、`DROP TABLE`、`password`、`private_key` など)。

## 📒 実行済み操作台帳

台帳に入るのは**高リスクかつ非冪等**のツールだけです: PTC の `execute_code` とすべての MCP ツール(既定)。`terminal`、`python_repl`、`write_file`、`patch_file` は入りません — シェルコマンドや編集は通常の作業であり、特定のコマンドを確認させるのはキーワードスキャンです。

台帳行は**ルートのメインセッション**(サブエージェントは `APPROVAL_SCOPE_PARENT` を解決)、ツール名、安定した引数ダイジェスト(ソート済み JSON、生の引数は決して保存しない)をキーにします。再実行は実行前に拒否され、初回の `result_summary` が返ります。行は設定された保持期間(24 時間)で失効し、セッション削除で消えます。パネルの削除が明示的な「もう一度実行」です。

**実行済み**のキーワードも同じ一回限りの扱いです: 承認された操作が成功するとキーワードが実行済みになり、以降そのキーワードを含む呼び出しは初回結果付きで拒否され、パネル(または `high_risk_guard action=reset_keyword`)がマーカーを消すまで続きます。

### パネル API のトランスポートゲート

`args_preview` には機微な値が含まれ得るため、すべてのパネルルート（`GET/POST/PUT/DELETE /sessions/:sid/high-risk-keywords*`、`/high-risk-ops/:id`、`/high-risk-keywords/builtin`）は**安全なトランスポート**を要求します（`server/trigger/http/transport_guard.py`）。

* **同一ホスト** — loopback ピア、または `Host: localhost/127.0.0.1/[::1]` は通過（デスクトップ webview は `http://localhost:8080` に直接接続）。
* **暗号化** — `X-Forwarded-Proto: https` または `wss` は通過。Fleet Ingress が自身の TLS スキーム（`tls_cert`/`tls_key`）からこのヘッダーを再発行します。クライアント指定値は `SHERRY_INGRESS_TRUST_FORWARDED` 設定時のみ採用するため、平文リモートクライアントが `https` を偽装できません。
* **それ以外は縮退** — リモートの平文 HTTP/WS 呼び出しには **HTTP 200 + 空の `keywords`/`ops` + `degraded: true` + 日本語通知** を返し、実データは返しません。書き込みも同様に拒否されます。パネルは転送エラーではなく通知を表示し、機微データは不安全な経路を通りません。

これはトランスポート層のゲートであり、ゲートウェイの Origin/token、CSRF、ログインゲートを補完するもので、代替ではありません。

## 🔁 メインエージェントとサブエージェント

| | メインエージェント | サブエージェント |
| :--- | :--- | :--- |
| キーワード一致 | `HighRiskKeywordApprovalHandler` が承認/拒否カードを出す | `ToolGuardrails` が `[NEEDS_APPROVAL]` を返し、子は完了する |
| 台帳一致 | 初回結果付きで拒否(カードなし) | 同じ |
| 人間の承認後 | このセッションの `KEYWORD_APPROVALS` に承認 + 引数ダイジェストを記録 | 親が `approval_context` 付きでステップを再開し、新しい子がそれを受け取る |
| YOLO | 可逆ツールはカードを省略、PTC/MCP は依然確認 | `full_access` の子はキーワードスキャンを省略、台帳は有効 |
| confirm_all | 通常どおりカード | 子は拒否(`[BLOCKED: confirm_all mode]`) |

HITL カードは `interrupt()` の前にセッションのハートビートを凍結し(`HEARTBEAT_SKIP`)、再開パスが戻った後に解除します。遅い承認でもターンが「停滞」で殺されることはありません。

### バブルの経路

1. 子のガードレールが `[NEEDS_APPROVAL: keyword=…, tool=…]` を返す。
2. 子が完了し、完了メッセージ構築が `[SYSTEM: …]` ガイドを追記して、親に `question` の使い方と再開方法を示す。
3. 親が `question` でユーザーに尋ねる(通常の HITL カード)。
4. 承認後、親は `taskflow_step(action="resume", step_outcome="approved", step_id=…, approval_context={keyword, tool})` を呼ぶ — 直接 spawn なら `sessions_spawn(approval_context=…)`。
5. 承認は**新しい子自身の**レジスタ(`KEYWORD_APPROVALS`)に書かれ、その spawn だけがキーワードを通過できる。次に無関係な spawn は再び尋ねる。
6. 拒否時は `blocked` として再開し、依存はブロックされたまま。

`approval_context` を運ぶのは TaskFlow ステップだけです: `_OPTIONAL_STEP_FIELDS` → `step_spawn_kwargs` → `dispatch_child` → `SpawnRequest.approval_context`。承認済みステップの通常の再試行は承認を保ち(schema 再試行は同じ操作)、新しいステップは継承しません。

## 🧰 ツール、API、パネル

`high_risk_guard`(メインとサブの両方)が台帳と操作を管理します: `add_keyword`、`list_keywords`、`remove_keyword`、`set_policy`、`reset_keyword`、`register_op`、`list_ops`、`remove_op`。子の書き込みはルートセッションに解決されます。

| エンドポイント | 用途 |
| :------- | :------ |
| `GET /sessions/:sid/high-risk-keywords` | キーワード + 台帳(パネルの一括読み込み) |
| `POST /sessions/:sid/high-risk-keywords` | `{keyword, scope}` を追加 |
| `PUT /sessions/:sid/high-risk-keywords` | `{keyword, policy}` を設定 |
| `DELETE /sessions/:sid/high-risk-keywords/:id` | 行を削除 |
| `POST /sessions/:sid/high-risk-keywords/reset` | 実行済みマーカーを消す |
| `DELETE /sessions/:sid/high-risk-ops/:id` | 台帳行を削除 |
| `GET /high-risk-keywords/builtin` | 読み取り専用の組み込み一覧 |

ツールバーの盾ボタンが現在のセッションの `HighRiskGuardPanel` を開きます: キーワードの追加/削除、block↔allow の切替、マーカーのリセット、台帳行の削除(明示的な再実行)。

## 🧪 テスト

| スイート | 対象 |
| :---- | :----- |
| `tests/agent/tools/pub_base/test_high_risk_guard_store.py` | 検証、スキャン、スコープ、ポリシー、実行マーカー、台帳の往復、パージ、保持 |
| `tests/agent/middlewares/test_high_risk_guard.py` | 台帳の拒否/再生、キーワード NEEDS_APPROVAL、承認、YOLO、confirm_all、ルートスコープ |
| `tests/agent/middlewares/humanInTheLoop/test_high_risk_keyword_handler.py` | handler の一致、実行済み拒否、承認/拒否、ハートビート凍結、ヘッドレス拒否 |
| `tests/agent/tools/test_high_risk_guard_tool.py` | エージェント向けツールの全アクション + 子→ルートスコープ |
| `tests/agent/tools/taskflow/test_approval_resume.py` | 承認後の再ディスパッチ、再試行時の承認保持、検証拒否 |
| `tests/agent/tools/subagent/test_child_access_inheritance.py` | アクセスモード継承、承認スコープのトークンルート解決、承認スタンプ |
| `tests/server/trigger/http/test_high_risk_guard_api.py` | キーワード CRUD、ポリシー、リセット、台帳削除 |
| `client/app/pages/home/components/__tests__/HighRiskGuardPanel.integration.test.ts` | パネル表示、追加/ポリシー/リセット/削除、バックエンド拒否 |

## ⚠️ 正直な制限事項

- **スキャンはトリップワイヤであり、パーサではありません。** 無関係な文字列内のキーワード(`ドキュメント作成コマンドの `echo "password"`)でも確認し、登録語を含まない表現の操作は通過します。リストは運用者/エージェントが維持します。
- **キーワードの実行マーカーはセッション全体です。** 計画の「ステップごとに再確認」はここでは意図的に厳しく、最初に承認された操作がキーワードを実行済みにし、以降そのキーワードを含む呼び出しはマーカーをリセットするまで拒否されます。再生には安全ですが、正当な二回目にはパネル操作が必要です。
- **未注釈のツールは低リスク扱いです。** 明示的な `metadata.high_risk` が優先し、キーを持たない `mcp__` ツールは高リスク、それ以外の未注釈ツールは違います。危険な操作を普通の名前の背後に隠すプラグインは自分で注釈する必要があります。
- **MCP は接頭辞で台帳に入り、既定で非冪等です。** 本当に安全な MCP 読み取りは `high_risk: False` / `idempotent: True` を宣言できます。
- **台帳は引数プレビュー(≤300 文字)を保存します。** ローカルの `high_risk_guard.db`(追跡対象の workspace ではありません)に留まりますが、プレビューには呼び出しが含んだ値が残り得ます。パネル API は同一ホストまたは HTTPS/WSS の場合のみこれを返します(それ以外は縮退通知)。
- **トランスポート信頼はヘッダーベースです。** `X-Forwarded-Proto` は信頼できるプロキシからのみ採用されます。別の場所で TLS を終端する構成では、ingress に `SHERRY_INGRESS_TRUST_FORWARDED` を設定して実スキームを転送してください。
- **バブルは親がターンを取ることに依存します。** 完了メッセージがガイドを自動で追記しますが、実際の承認はメインエージェントが行う `question` 呼び出しです。完了メッセージを読まない親はステップを blocked のままにします。
- **保持は遅延実行です。** 期限切れの行は台帳アクセス時に最大 1 時間に 1 回掃除されるため、24 時間を超えて次の掃除まで残ることがあります。

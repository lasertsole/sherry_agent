# HITL(Human-In-The-Loop)ミドルウェア

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

hermes-agent パイプラインのための包括的なヒューマン・イン・ザ・ループ(HITL)ミドルウェアです。コマンド実行(ハードライン/危険)、ファイル書き込み、MCP ツール呼び出し、破壊的スラッシュコマンド、ピアペアリングに対する階層型承認ゲートを提供し、すべて単一のミドルウェアフックで管理されます。

---

## 目次

- [アーキテクチャ概要](#アーキテクチャ概要)
- [レイヤーリファレンス](#レイヤーリファレンス)
  - [1. ハードライン＆危険検出](#1-ハードライン危険検出)
  - [2. 書き込み承認ゲート](#2-書き込み承認ゲート)
  - [3. 割り込みマネージャー](#3-割り込みマネージャー)
  - [4. MCP 発動同意](#4-mcp-発動同意)
  - [5. カンバントリアージ](#5-カンバントリアージ)
  - [6. スマート承認](#6-スマート承認)
  - [7. ペアリングストア](#7-ペアリングストア)
  - [8. スラッシュ確認](#8-スラッシュ確認)
- [ミドルウェアフック](#ミドルウェアフック)
- [設定](#設定)
- [承認フックシステム](#承認フックシステム)
- [ファイル構成](#ファイル構成)

---

## アーキテクチャ概要

HITL ミドルウェアは、`HumanInTheLoop` ミドルウェアクラスによって調整される7つの独立したサブゲートで構成されています:

```
HumanInTheLoop
├── ApprovalPipeline      (approval.py — 階層型コマンド承認)
│   ├── detect_hardline_command()
│   ├── detect_dangerous_command()
│   └── smart_approve()
├── WriteApprovalGate     (gates.py — ファイル/メモリ書き込みゲーティング)
├── InterruptManager      (gates.py — セッション別割り込みフラグ)
├── MCPElicitationConsent (gates.py — MCP サーバー同意)
├── KanbanTriage          (gates.py — タスク失敗トリアージ)
├── PairingStore          (gates.py — プラットフォームユーザー承認)
└── SlashConfirm          (gates.py — 破壊的スラッシュ確認)
```

各ゲートは独立してインスタンス化・テスト可能です。`HumanInTheLoop` ミドルウェアがこれらを結線し、標準の `AgentMiddleware` ライフサイクルフック(`after_model`、`wrap_tool_call`、`awrap_tool_call`、`abefore_agent`)を通じて公開します。

---

## レイヤーリファレンス

### 1. ハードライン＆危険検出

**ファイル:** `detection.py`

副作用なしにコマンドを分類する2つの静的パターンマッチャー:

| 関数 | 目的 |
|---|---|
| `detect_hardline_command(cmd)` | `HARDLINE_PATTERNS` に対してチェック — 常にレビューが必要なコマンド(`rm -rf`、`format`、`dd` など) |
| `detect_dangerous_command(cmd)` | `DANGEROUS_PATTERNS` に対してチェック — 破壊可能性の高いコマンド(`DROP TABLE`、`shutdown`、`rm`、強制プッシュ) |

どちらも最初に一致したパターン(文字列)または `None` を返します。

### 2. 書き込み承認ゲート

**ファイル:** `gates.py` — クラス `WriteApprovalGate`

ファイルまたはメモリターゲットへの保留中の書き込み操作を管理します。各書き込みは一意の ID で追跡され、承認/拒否のために保存されます:

| メソッド | 説明 |
|---|---|
| `request_write(target, content, session_id)` | 承認のために書き込みを送信します。追跡された `write_id` を含む `ApprovalResult` を返します。 |
| `approve_write(session_id, write_id)` | 保留中の書き込みを承認します。 |
| `reject_write(session_id, write_id)` | 保留中の書き込みを拒否します。 |
| `get_pending_writes(session_id, target)` | 保留中の書き込みを一覧表示し、ターゲットタイプでフィルタリングできます。 |

### 3. 割り込みマネージャー

**ファイル:** `gates.py` — クラス `InterruptManager`

実行中のツール実行をゲーティングするセッション別のブールフラグ:

| メソッド | 説明 |
|---|---|
| `set_interrupt(session_id, active=True)` | 割り込みフラグを設定または解除します。 |
| `is_interrupted(session_id)` | セッションが割り込みされたかどうかを確認します。 |
| `clear_interrupt(session_id)` | 割り込みフラグを解除します(利便性エイリアス)。 |

割り込みが設定されると、`wrap_tool_call` / `awrap_tool_call` フックはステータス `"error"` の `ToolMessage` を返し、実行をブロックします。

### 4. MCP 発動同意

**ファイル:** `gates.py` — クラス `MCPElicitationConsent`

副作用を引き起こす可能性のある MCP(Model Context Protocol)サーバーの場合:

| メソッド | 説明 |
|---|---|
| `request_consent(server_name, session_id)` | MCP サーバーとのやり取りに対する明示的な同意を要求する割り込みをユーザーに表示します。 |

### 5. カンバントリアージ

**ファイル:** `gates.py` — クラス `KanbanTriage`

カンバンスタイルのトリアージエスカレーションのためのタスク失敗を追跡します:

| メソッド | 説明 |
|---|---|
| `report_task_failure(task_id, session_id)` | タスク失敗を登録します。`TriageStatus`(`NEW`、`ACKNOWLEDGED`、`RESOLVED` のいずれか)を返します。失敗回数が設定された `recurrence_limit` を超えると `RecurrenceLimitError` を発生させます。 |
| `resolve_triage(task_id, session_id)` | トリアージされたタスクを解決済みとしてマークします。 |

### 6. スマート承認

**ファイル:** `approval.py` — クラス `ApprovalPipeline`

複数のレイヤーを持つ構成可能な承認パイプライン:

| レベル | メカニズム |
|---|---|
| **レイヤー 1 — ハードライン検出** | 常にブロックされるコマンド(`rm -rf`、`format` など) |
| **レイヤー 2 — 危険検出** | フラグ付きコマンド(`DROP TABLE`、`shutdown` など) |
| **レイヤー 3 — ターミナルモード** | ターミナルコマンドの承認ポリシーに委任 |
| **レイヤー 4 — ツール承認** | プラグインエスカレーションによるツール承認(`request_tool_approval`) |
| **レイヤー 5 — セッションキャッシュ** | 繰り返しのプロンプトを避けるためにセッションごとに承認済みツールをキャッシュ |
| **レイヤー 6 — スマート承認** | `smart_approve()` — コマンド内容とコンテキストに基づくヒューリスティックな自動承認/自動拒否 |
| **レイヤー 7 — ヒューマン割り込み** | ユーザー決定のための `interrupt()` へのフォールバック |

パイプラインは外部呼び出し元に直接公開されます:

| メソッド | 説明 |
|---|---|
| `check_command(command, session_id)` | ハードライン + 危険検出を実行します。`ApprovalResult` を返します。 |
| `check_command_with_approval(command, session_id, prompt_fn)` | スマート承認 + ヒューマン割り込みを含む完全なパイプライン。 |
| `smart_approve(command)` | ヒューリスティックのみの承認(検出または割り込みなし)。 |
| `request_tool_approval(name, args, session_id)` | プラグインエスカレーションによるツール承認チェック。 |
| `approve_tool_for_session(name, args, session_id)` | セッションの残り期間、承認済みツールをキャッシュします。 |

### 7. ペアリングストア

**ファイル:** `gates.py` — クラス `PairingStore`

プラットフォームレベルのユーザー許可リスト:

| メソッド | 説明 |
|---|---|
| `is_user_allowed(platform, user_id)` | ユーザーが特定のプラットフォームで承認されているかどうかを確認します。 |
| `approve_user(platform, user_id)` | ユーザーを許可リストに追加します。 |
| `revoke_user(platform, user_id)` | ユーザーを許可リストから削除します。 |

### 8. スラッシュ確認

**ファイル:** `gates.py` — クラス `SlashConfirm`

破壊的スラッシュコマンドの確認ゲート(例: `/reset`、`/kill`):

| メソッド | 説明 |
|---|---|
| `confirm_destructive(action, session_id)` | 破壊的な操作の確認を求める割り込みを表示します。`ApprovalResult` を返します。 |

---

## ミドルウェアフック

`HumanInTheLoop` クラスは4つのフックを通じてエージェントライフサイクルに統合されます:

| フック | 目的 |
|---|---|
| `after_model` / `aafter_model` | LLM 出力を傍受します。各ツール呼び出しについて: コマンド承認、書き込みゲートチェック、`interrupt_on` 設定チェック、プラグインエスカレーション承認を実行します。ブロックされたとき、ツール呼び出しを人工的な `ToolMessage` 結果に置き換えます。 |
| `wrap_tool_call` | ツールを実行する前に割り込みフラグを確認します。セッションが割り込みされた場合、エラー `ToolMessage` を返します。 |
| `awrap_tool_call` | `wrap_tool_call` の非同期版。 |
| `abefore_agent` / `before_agent` | ターンごとの状態をリセットします(`turn_interrupted` フラグをクリア)。 |

### 割り込みフロー

```
LLM 出力 → after_model
  ├── ハードライン/危険チェック (レイヤー 1-2)
  ├── 書き込み承認ゲート (メモリ書き込みのみ)
  ├── interrupt_on 設定チェック
  ├── プラグインツール承認 (レイヤー 4)
  └── 改訂された tool_calls + 人工的な ToolMessages

各ツール呼び出し → wrap/awrap_tool_call
  └── 割り込みフラグチェック → ブロックまたは通過
```

---

## 設定

すべての設定は `HITLConfig` データクラス(`types.py` で定義)を通じて渡されます:

| フィールド | 型 | デフォルト | 説明 |
|---|---|---|---|
| `mode` | `ApprovalMode` | `STRICT` | `STRICT`、`SMART`、`DISABLED` のいずれか |
| `interrupted_tools` | `dict[str, bool \| dict]` | `{}` | `interrupt_on` 設定によってゲーティングされるツール名。各エントリはブール値(デフォルトの許可決定 `["approve", "edit", "reject"]`)または `allowed_decisions` とオプションの `description` コーラブルを持つ dict を指定できます。 |
| `interrupt_on` | deprecated | — | `interrupted_tools` に置き換えられました。 |
| `write_approval_memory` | `bool` | `False` | `WriteApprovalGate` を通じてメモリ書き込みをゲーティングします。 |
| `description_prefix` | `str` | `"Agent wants to"` | 人間が読めるアクション説明のプレフィックス。 |
| `kanban_recurrence_limit` | `int` | `5` | KanbanTriage で `RecurrenceLimitError` になるまでの最大失敗回数。 |

### 例

```python
from agent.middlewares.HumanInTheLoop import HumanInTheLoop, HITLConfig, ApprovalMode

middleware = HumanInTheLoop(HITLConfig(
    mode=ApprovalMode.SMART,
    interrupted_tools={
        "terminal": {"allowed_decisions": ["approve", "reject"]},
        "memory": True,
    },
    write_approval_memory=True,
    kanban_recurrence_limit=3,
))
```

---

## 承認フックシステム

すべての承認決定後に実行される外部コールバックを登録します:

```python
def log_approval(session_id: str, result: ApprovalResult):
    print(f"[{session_id}] {result.decision}: {result.reason}")

middleware.register_approval_hook(log_approval)
```

フックはセッション ID と完全な `ApprovalResult` を受け取ります。すべてのフックは try/except でラップされており、失敗したフックが承認フローをブロックすることはありません。

---

## ファイル構成

```
agent/middlewares/HumanInTheLoop/
├── __init__.py        # 公開エクスポート
├── types.py           # 列挙型、データクラス、設定、スタブ
├── detection.py       # ハードライン + 危険パターン検出
├── approval.py        # 階層型承認パイプライン
├── gates.py           # サブゲート (書き込み、割り込み、MCP、カンバン、ペアリング、スラッシュ)
├── core.py            # HumanInTheLoop ミドルウェアクラス
├── README.md          # このファイル (英語)
├── README.zh.md       # 中国語版
├── README.ko.md       # 韓国語版
└── README.ja.md       # 日本語版
```

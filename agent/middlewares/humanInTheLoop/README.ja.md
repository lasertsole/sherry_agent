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
- [ツール承認の永続化](#ツール承認の永続化)
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

### 外部パス割り込み(`external_file_access`)

すべての割り込みがこのミドルウェアから来るわけではありません。ファイルツール(`read_file`、`write_file`、`patch_file`、`search_files` など)が `agent/tools/pub_base/path_utils.py::resolve_external_path()` でパスを解決する際、`ROOT_DIR` の外側にあり YOLO 拒否リスト(セキュリティフロア)に命中しないパスのみが**6番目**のゲートに到達します: `interrupted_tools` には**よらず**、ツール層が直接起こす `interrupt()` で、独自の決定セットを持ちます:

- `approve` — このファイルのみ許可(セッション限定、サブエージェントに継承);
- `approve_dir` — ファイルが属するディレクトリ全体を許可(セッション限定のプレフィックス一致、サブエージェントにも継承);
- `yolo` — すべての外部パスを恒久的に許可(グローバル YOLO フラグを書き込む);
- `reject` — アクセスを拒否。

▶️ 詳細: [docs/sandbox/README.ja.md](../../docs/sandbox/README.ja.md#5-外部ファイルパスゲートファイルツール)。

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
from agent.middlewares.humanInTheLoop import HumanInTheLoop, HITLConfig, ApprovalMode

middleware = HumanInTheLoop(
    HITLConfig(
        mode=ApprovalMode.SMART,
        interrupted_tools={
            "terminal": {"allowed_decisions": ["approve", "reject"]},
            "memory": True,
        },
        write_approval_memory=True,
        kanban_recurrence_limit=3,
    )
)
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

## ツール承認の永続化

ツール呼び出しの承認決定は JSON ファイルに永続化され、プロセス再起動後も失われません——インメモリレジスタの決定は再起動で失われます。ストアはオペレーター単位で分離され、楽観的なバイト改訂 CAS で更新されます。

- **ストアと保存場所** — `ToolApprovalStore`(`approval_store.py`)は `SRC_DIR/data/approvals.json` を管理します(Sherry 所有のランタイムデータ。boulder ポインターや証跡レジャーと同じディレクトリツリー。`SHERRY_APPROVAL_STORE_PATH` で上書き可能)。ファイルが無い場合は「記録済みの決定なし」を意味し、破損した JSON は空として読み込まれ、次回の書き込み成功時に置き換えられます。書き込みはアトミック(一時ファイル + `os.replace`)で、ファイルは `0600` で作成されます。保存されるのはツール名、引数ハッシュ、判定(`allow`/`deny`)、理由、タイムスタンプのみ——生のツール引数やシークレットは保存しません。
- **バイト改訂 CAS** — 読み取りのたびに生バイトの sha256 改訂を取得し、ディスク上の改訂が一致する場合にのみ書き込みが成立します。競合した書き手は再読み取りして有限回(`max_cas_retries`、既定 3)リトライし、それでも失敗すれば失敗を報告します。プロセス内の変更はパス単位で直列化され、プロセス間の書き手は CAS で収束します。
- **オペレータースコープ** — 決定は `(オペレーター, セッション, ツール, 引数ハッシュ)` をキーとします。`operator_scope()` / `set_operator()` が `ContextVar` で現在のオペレーターを設定します。認証済みトランスポート身元が無い場合、セッション身元がオペレータースコープの等価物です。オペレーター A の承認は B には見えません。
- **オペレーター不在時は自動拒否** — スコープ内にオペレーターがいない場合(またはヘッドレスなシステム注入ターン: 最後の human メッセージの `metadata.internal` / `metadata.origin == "cron"`)、`ToolApprovalStore.evaluate()` は明示的な自動拒否を返し、本来 `interrupt()` するゲート(設定済み `interrupted_tools`、危険なターミナルコマンド、サンドボックスバイパス)は、応答できない人間のためにグラフを停止させる代わりにエラー `ToolMessage` を返します。
- **HITL 統合** — `ApprovalPipeline.request_tool_approval` / `approve_tool_for_session` / `deny_tool_for_session` がストアを読み書きします(従来のインメモリ `hitl:tool_approved:*` キャッシュも引き続き参照・更新されるため、既存セッションはそのまま動作します)。設定済み `interrupted_tools` 呼び出しの承認・拒否は決定として記録され、再起動後も同一セッションの同一呼び出しに再承認は不要です。`edit` と `yolo` の決定はそれぞれ一回限り / セッションフラグのスコープに留まります。

---

## ファイル構成

```
agent/middlewares/HumanInTheLoop/
├── __init__.py        # 公開エクスポート
├── types.py           # 列挙型、データクラス、設定、スタブ
├── approval_scope.py  # オペレーター ContextVar + ヘッドレスターン判定
├── approval_store.py  # 永続承認ストア (JSON + バイト改訂 CAS)
├── detection.py       # ハードライン + 危険パターン検出
├── approval.py        # 階層型承認パイプライン
├── gates.py           # サブゲート (書き込み、割り込み、MCP、カンバン、ペアリング、スラッシュ)
├── core.py            # HumanInTheLoop ミドルウェアクラス
├── README.md          # このファイル (英語)
├── README.zh.md       # 中国語版
├── README.ko.md       # 韓国語版
└── README.ja.md       # 日本語版
```

# EMA Agent Middleware システム

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangGraph 1.2+](https://img.shields.io/badge/LangGraph-1.2%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

LLMエージェント実行のための組み合わせ可能なミドルウェアパイプライン — メッセージ管理、ツール呼び出し検証、ガードレール、予算制御、マルチモーダル処理、ハートビート監視。すべてのミドルウェアは共有され永続的な状態システムを介して**LangGraphエージェントライフサイクル**にフックします。

---

## 目次

- [アーキテクチャ概要](#アーキテクチャ概要)
- [ミドルウェアチェーン](#ミドルウェアチェーン)
- [ミドルウェアリファレンス](#ミドルウェアリファレンス)
  - [Summarization](#summarization)
  - [ToolCallNormalize](#toolcallnormalize)
  - [ToolGuardrails](#toolguardrails)
  - [IterationBudget](#iterationbudget)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [MultimodalProcessor](#multimodalprocessor)
  - [ContextEngineHook](#contextenginehook)
- [共有状態システム](#共有状態システム)
- [設定](#設定)
- [ライフサイクルとデータフロー](#ライフサイクルとデータフロー)
- [カスタムミドルウェアの作成](#カスタムミドルウェアの作成)

---

## アーキテクチャ概要

すべてのミドルウェアは専用の**基本クラス**(例: `SummarizationMiddleware`、`AgentMiddleware`、または `ContextEngineHook`)を継承し、それぞれが以下の**ライフサイクルフック**の1つ以上を実装します:

| フック | 呼び出しタイミング | 目的 |
|---|---|---|
| `awrap_before_agent(state)` | すべてのLLM呼び出しの前 | 状態の準備、システムプロンプトの注入、履歴の整理 |
| `awrap_after_agent(state)` | すべてのLLM呼び出しの後 | アシスタント応答の後処理、副作用の実行 |
| `awrap_tool_call(state, tool_call)` | 個々のツール実行の前 | ツール呼び出しの検証、保護、強化 |
| `awrap_after_tool(state)` | ツールが返された後 | ツール結果の処理、予算の確認、計算フィールドの追加 |

ミドルウェアインスタンスは**メインエージェントビルダー**に登録され、エージェントノードを包むチェーンとして**宣言順に実行**されます。

### 状態の永続化

ミドルウェアは `runtime` から提供される**シングルトンインスタンス**である2つのクロスフック状態辞書を介して通信します:

- **`state_register_mem`** (`StateRegisterMeM`) — メモリ内、セッションごとの状態ストア。カウンタ、予算、ガードレール追跡、ハートビート進行に使用されます。
- **`state_register_db`** (`StateRegisterDB`) — SQLite対応、セッションごとの状態ストア。プロセス再起動後も持続が必要な構造化レコードに使用されます。

両方とも `runtime.state_register` からインポートされるシングルトンです。同じ `Register` インターフェース(`set_state`、`get_state`、`delete_state`、`clear_session` など)を共有します。

---

## ミドルウェアチェーン

**メインエージェント**の完全なパイプラインは次の順序で実行されます(それぞれが内側のレイヤーを包みます):

```
┌─────────────────────────────────────────────────────────┐
│  Summarization                (最外側 — 最初に整理)      │
│  ToolCallNormalize            (壊れたツール呼び出しを修復│
│  ToolGuardrails               (ループ検出、停止)         │
│  IterationBudget              (ハード反復上限)          │
│  HeartbeatStaleness           (ハートビートタイムアウト) │
│  MultimodalProcessor          (メディア処理)             │
│  ContextEngineHook            (メモリとナッジ、最内側)   │
│    ┌─────────────────────────────────────┐              │
│    │         LLM (Agent Node)            │              │
│    └─────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────┘
```

> **注:** `HeartbeatStaleness` は**ワーカーエージェントのみ**で使用されます(メインエージェントではありません)。サブエージェントの進行状況を監視し、アイドル/応答不能になったエージェントを終了します。

**データフロー (単一ターン):**

1. `awrap_before_agent` — 外側から内側へ実行 (summarizationが最初、context engineが最後)
2. LLMが応答を生成 (ツール呼び出しを含む場合あり)
3. `awrap_after_agent` — 内側から外側へ実行 (context engineが最初、summarizationが最後)
4. 各ツール呼び出しについて: 各ミドルウェアの `awrap_tool_call` が順に発火
5. 各ツールが返された後: 各ミドルウェアの `awrap_after_tool` が順に発火
6. LLMが最終回答を生成するか予算が尽きるまでステップ1から繰り返す

---

## ミドルウェアリファレンス

### Summarization

**ファイル:** `summarization.py`
**クラス:** `Summarization` (`SummarizationMiddleware` を継承)
**フック:** `awrap_before_agent`, `awrap_after_agent`

トークン制限を超えたときに対話履歴を整理し、最近のターンを保存して、古いメッセージの圧縮要約を生成します。

**動作:**

- `awrap_before_agent`: メッセージ履歴の総トークンを数えます。設定された `max_tokens` を超えている場合:
  1. 最近の対話の最後のNターンをそのまま保持
  2. それ以前のすべてを要約プロンプトに圧縮
  3. 要約を `SystemMessage` としてメッセージリストの先頭に注入
- `awwrap_after_agent`: 要約結果を `state_register_mem` に保存

**設定:**

```json
{
  "summarization": {
    "max_tokens": 64000,
    "recent_turns": 10
  }
}
```

---

### ToolCallNormalize

**ファイル:** `tool_call_normalize.py`
**クラス:** `ToolCallNormalize` (`AgentMiddleware` を継承)
**フック:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`, `awrap_after_tool`

不正なツール呼び出しを修復します — 主にLLMが誤ったまたは不一致の `id`/`name` フィールドを持つツール呼び出しを生成する**ペアの一致しないID/名前パターン**を修正します。

**動作:**

- **ペア修復:** `tool_call` の複数のエントリで `id` 値が期待パターンと一致しない場合、ミドルウェアは名前→期待IDのマッピングを構築し、再割り当てします。
- **重複排除:** すでに処理されたツール呼び出しをスキップします(状態で追跡)。
- **ノイズ削減:** 検証に失敗したツール呼び出しエントリを除去します。

**なぜ必要なのか:** LLM(特に小規模または量子化モデル)は、ぶら下がった、入れ替わった、または重複した `id` フィールドを持つツール呼び出しを頻繁に生成します。このミドルウェアは、それらがランタイムに到達する前に静かに解決します。

---

### ToolGuardrails

**ファイル:** `tool_guardrails.py`
**クラス:** `ToolGuardrails` (`AgentMiddleware` を継承)
**フック:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`, `awrap_after_tool`

**無限ツール呼び出しループ**、**繰り返される失敗パターン**、**同一の再試行**を検出して防止します。3段階のエスカレーションシステムを使用します。

**段階:**

| 段階 | 条件 | 動作 |
|---|---|---|
| `warn` | 最近の呼び出しでツール名がN+回繰り返される(同じツール、同じ名前、任意の引数) | 次のLLM呼び出しの前に対話に警告 `SystemMessage` を注入 |
| `block` | 同じツール + 同じ引数がN+回繰り返される | ツール呼び出しの実行を防止 — 代わりにエラー `ToolMessage` を返す |
| `halt` | ブロックされた呼び出しが連続N+回再生成される | **ハードストップ**を強制: エージェント実行を終了する `AgentHalt` を発生 |

**検出データ:**

- `state_register_mem` でツール呼び出し名とシリアル化された引数を追跡
- スライディングウィンドウ方式を使用 — 最も最近の呼び出しのみを考慮(ウィンドウサイズは設定可能)

**設定:**

```json
{
  "tool_guardrails": {
    "call_window": 15,
    "warn_threshold": 4,
    "block_threshold": 3,
    "halt_threshold": 3
  }
}
```

---

### IterationBudget

**ファイル:** `iteration_budget.py`
**クラス:** `IterationBudget` (`AgentMiddleware` を継承)
**フック:** `awrap_before_agent`, `awrap_after_tool`

対話ターンあたりの**LLM-ツール反復回数のハード上限**を適用します。制限に達すると、エージェントはそれ以上のツール呼び出しなしで最終回答を生成するよう強制されます。

**動作:**

- `awrap_before_agent`: 現在の反復回数を `max_iterations` 制限と比較します。超えている場合、利用可能な情報を使用して即座に回答するようLLMに指示する `SystemMessage` を追加します。
- `awrap_after_tool`: `state_register_mem` の反復カウンタを増加させます。
- **反復カウンタのリセット**は、制限到達後の次の `awrap_before_agent` 呼び出しで発生します("resetting" フラグで検出)。

**設定:**

```json
{
  "iteration_budget": {
    "max_iterations": 10
  }
}
```

---

### HeartbeatStaleness

**ファイル:** `heartbeat_staleness.py`
**クラス:** `HeartbeatStaleness` (`AgentMiddleware` を継承)
**エクスポート名:** `HeartbeatStaleness`
**使用箇所:** ワーカーエージェントのみ(メインエージェントではありません)
**フック:** `awrap_before_agent`, `awrap_after_agent`, `awrap_model_call`, `awrap_tool_call`

ワーカーエージェントが長時間**アイドルまたは応答不能**であることを検出して終了します。エージェントが進行したか(反復回数の増加または現在のツールの変更)を確認する定期的なハートビートタイマーを使用します。

**二重しきい値システム:**

| 状態 | しきい値 | 根拠 |
|---|---|---|
| **アイドル** (実行中ツールなし) | `stale_cycles_idle` (デフォルト7サイクル ≈ 7分) | より厳格 — エージェントがハングした呼び出しで停止している可能性が高い |
| **ツール処理中** (ツール実行中) | `stale_cycles_in_tool` (デフォルト20サイクル ≈ 20分) | より緩やか — ツールが正当に長時間実行されている可能性がある |

**進行検出:**

`heartbeat_interval_minutes`(デフォルト1分)ごとに、バックグラウンドタイマーがエージェントの現在の `(iteration_count, current_tool)` ペアを以前に観察された値と比較します。**どちらか**が進行していればstaleカウンタはゼロにリセットされ、そうでなければ1ずつ増加します。

**終了:**

staleカウンタが設定されたしきい値に達すると、セッションは `killed` とマークされます。以降の `awrap_model_call` または `awrap_tool_call` 呼び出しは**`HeartbeatTimeoutError`**を発生させ、エージェントを正常に終了します。

**状態ストレージ:**

すべてのセッションごとの状態は `state_register_mem` に保持され、同じターン内のミドルウェアフック間で存続します:

| キー | 目的 |
|---|---|
| `heartbeat_iter` | 現在の反復回数 |
| `heartbeat_tool` | 現在実行中のツール名 (または `None`) |
| `heartbeat_stale` | 連続するstaleサイクルカウンタ |
| `heartbeat_killed` | セッションが終了したかどうか |

**設定:**

```json
{
  "heartbeat_staleness": {
    "heartbeat_interval_minutes": 1,
    "stale_cycles_idle": 7,
    "stale_cycles_in_tool": 20
  }
}
```

---

### MultimodalProcessor

**ファイル:** `multimodal_processor.py`
**クラス:** `MultimodalProcessor` (`AgentMiddleware` を継承)
**フック:** `awrap_before_agent`, `awrap_after_agent`, `awrap_after_tool`

対話内の**マルチモーダルコンテンツ**(画像、ファイル)を処理します — さまざまなソース(ローカルファイル、S3、HTTP)からのメディアURIをLLMが消費できる形式に正規化します。

**動作:**

- ユーザーメッセージのメディア参照を検出 (ファイルパス、S3 URI、HTTP URL)
- LLM消費用にメディアをbase64データURIに解決してエンコード
- 履歴をクリーンに保つため、LLM呼び出し後に対話から解決済みURIを削除
- 対応: ローカルファイルシステム、S3互換ストレージ、HTTP/HTTPS URL

**設定:**

```json
{
  "multimodal_processor": {
    "enabled": true,
    "max_image_size_mb": 20,
    "allowed_mime_types": ["image/jpeg", "image/png", "image/webp", "image/gif"]
  }
}
```

---

### ContextEngineHook

**ファイル:** `context_engine/core.py`, `context_engine/nudge.py`
**クラス:** `ContextEngineHook` (`AgentMiddleware` を継承)
**フック:** `awrap_before_agent`, `awrap_after_agent`, `awrap_tool_call`

**最内側のミドルウェア** — LLMに最も近い位置。システムプロンプト、対話の永続化、定期的な**ナッジ**介入(メモリレビュー、スキルレビュー)、および**知識グラフの保守**を管理します。

#### コア動作 (`core.py`)

- **`awrap_before_agent`:**
  1. キャッシュ(`MemoryCache`)から**システムプロンプト**をロード (デフォルトへのフォールバック付き)
  2. **スレッドセーフ変数を設定** (`conversation_id`, `user_id`) — 下流フック用
  3. `MesMemory` から保存されたメッセージをロードして対話状態にマージ
  4. (オプションでカスタマイズされた)システムプロンプトを最初の `SystemMessage` として注入
- **`awrap_after_agent`:**
  1. `add_messages()` でアシスタント応答を `MesMemory` に永続化
  2. エージェント後の副作用として**ナッジシステム**を実行(下記参照)
  3. 定期的なタスクのため**知識グラフ保守(`after_turn`)を呼び出し**(例: 古いノードの整理、エッジ重みの更新)。try/exceptでラップされており、失敗は致命的ではなく、デバッグレベルで記録されます。
- **`awrap_tool_call` (ナッジ副作用):** ツールが呼び出されるたびに `MesMemory` のスキルレビューカウンタを増加

#### ナッジシステム (`nudge.py`)

メモリ/スキルシステムへの関与を促す介入メッセージを定期的に注入します。

| ナッジタイプ | トリガー | 内容 |
|---|---|---|
| **メモリナッジ** | 最後のメモリ操作から10ターンごと | より良い結果のためにユーザーにメモリ/システムプロンプトの更新を促す |
| **スキルレビューナッジ** | 最後のレビューから10回のツール呼び出しごと | 最後のツール実行結果の評価をユーザーに促す |
| **結合ナッジ** | 両方の条件が同時に満たされる | メモリとスキルレビューの両方を扱うマージされたメッセージ |

**ロックメカニズム:** 各ナッジタイプは `MesMemory` で**クールダウンロック**(`nudge_lock_memory`, `nudge_lock_skill`)を持ち、10ターンのウィンドウ内での繰り返しナッジを防ぎます。ユーザーが実際にアクションを実行した場合(例: メモリの更新やスキルの評価)、ロックはリセットされます。

ナッジメッセージは、通常のアシスタント応答の後に追加される**個別の `AIMessage` チャンク**として送信され、UIで自然なフォローアップ提案として表示されます。

#### 知識グラフ統合

ナッジロジックの後、`aafter_agent` は定期的な保守のために知識グラフの `after_turn(session_id)` を呼び出します。これには古いノードの整理やエッジ重みの更新などのタスクが含まれます。呼び出しはtry/exceptブロックでラップされており、失敗してもエラーはデバッグレベルで記録され、エージェントのフローを中断しません。

**設定 (`system/config.tool.md` 内):**

```json
{
  "context_engine": {
    "enabled": true,
    "nudge_memory_interval": 10,
    "nudge_skill_interval": 10
  }
}
```

#### 依存関係

- **`MemoryCache`** — システムプロンプトとメタデータ用のスレッドセーフなメモリ内キャッシュ
- **`MesMemory`** — 永続的な対話メモリバックエンド (メッセージ、ナッジロック、スキルレビューカウンタを保存)
- **経験グラフ** — 各ターン後の定期的な保守用知識グラフモジュール
- **`system/config.tool.md`** — システムプロンプトとナッジ間隔の設定ファイル

---

## 共有状態システム

すべてのミドルウェアは `runtime.state_register` の2つの共有シングルトンインスタンスにアクセスします:

| インスタンス | クラス | 永続性 | 目的 |
|---|---|---|---|
| `state_register_mem` | `StateRegisterMeM` | メモリ内、セッションごと | カウンタ、フラグ、現在の要約、ウィンドウバッファ、ハートビート状態 |
| `state_register_db` | `StateRegisterDB` | SQLite対応、セッションごと | プロセス再起動後も存続する構造化レコード |

両方のクラスは `Register` を継承し、同じインターフェースを公開します:

| メソッド | 説明 |
|---|---|
| `set_state(session_id, key, value)` | セッションにキーと値のペアを設定 |
| `get_state(session_id, key, default)` | デフォルトのフォールバック付きでキーの値を取得 |
| `get_all_states(session_id)` | セッションのすべてのキーと値のペアを取得 |
| `delete_state(session_id, key)` | 特定のキーを削除 |
| `clear_session(session_id)` | セッションのすべての状態を削除 |
| `has_session(session_id)` | セッションが存在するかどうかを確認 |
| `has_key(session_id, key)` | キーがセッションに存在するかどうかを確認 |
| `update_states(session_id, states)` | 複数のキーを一括更新 |

### 初期化ガード

`StateRegisterMeM` と `StateRegisterDB` の両方が `__init__` で `_initialized` ガードを使用して再初期化を防ぎます:

```python
class StateRegisterMeM(Register):
    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._states = {}
        self._initialized = True
```

これは `Register.clear_all_register_sessions` が `__init__` をトリガーして `_states` をリセットし、すべてのメモリ内状態を消去できるバグを修正します。

### 名前空間規則

各ミドルウェアは独自の最上位キー名前空間を使用します:

```
state_register_mem (session "abc123") = {
    "summarization": { "current_summary": "..." },
    "tool_call_normalize": { "last_names": [...] },
    "tool_guardrails": { "tool_calls": [...], "block_count": 3 },
    "iteration_budget": { "count": 5 },
    "heartbeat_iter": 3,
    "heartbeat_tool": "web_search",
    "heartbeat_stale": 0,
    "heartbeat_killed": False,
    "multimodal_processor": { "resolved_uris": [...] },
}
```

---

## 設定

ミドルウェアはメインエージェントビルダーで設定されます。チェーンの順序とパラメータは、別の設定ファイルではなくエージェントの構築中に設定されます。

### ビルダー設定の例

```python
from agent.middlewares import (
    Summarization,
    ToolCallNormalize,
    ToolGuardrails,
    IterationBudget,
    HeartbeatStaleness,
    MultimodalProcessor,
    ContextEngineHook,
)

middlewares = [
    Summarization(session_id="session_001"),
    ToolCallNormalize(session_id="session_001"),
    ToolGuardrails(config=ToolCallGuardrailConfig(warn_threshold=4, block_threshold=3, halt_threshold=3)),
    IterationBudget(session_id="session_001", max_iterations=10),
    # HeartbeatStaleness — worker agents only
    MultimodalProcessor(session_id="session_001"),
    ContextEngineHook(session_id="session_001"),
]
```

### ミドルウェアごとのパラメータ

```yaml
middleware:
  summarization:
    max_tokens: 64000
    recent_turns: 10
  tool_guardrails:
    call_window: 15
    warn_threshold: 4
    block_threshold: 3
    halt_threshold: 3
  iteration_budget:
    max_iterations: 10
  heartbeat_staleness:
    heartbeat_interval_minutes: 1
    stale_cycles_idle: 7
    stale_cycles_in_tool: 20
  multimodal_processor:
    enabled: true
    max_image_size_mb: 20
  context_engine:
    enabled: true
    nudge_memory_interval: 10
    nudge_skill_interval: 10
```

---

## ライフサイクルとデータフロー

### 単一ターン (詳細)

```
[ユーザーがメッセージを送信]
    │
    ▼
Summarization.awrap_before_agent(state)
    │  トークン予算を超えていれば履歴を整理
    ▼
ToolCallNormalize.awrap_before_agent(state)
    │  (通常はno-op)
    ▼
ToolGuardrails.awrap_before_agent(state)
    │  ループ検出時は警告を注入
    ▼
IterationBudget.awrap_before_agent(state)
    │  予算超過時は"即答せよ"を注入
    ▼
HeartbeatStaleness.awrap_before_agent(state)  [ワーカーエージェントのみ]
    │  カウンタをリセット、ハートビートタイマーを開始
    ▼
MultimodalProcessor.awrap_before_agent(state)
    │  メディアURIを解決 → base64
    ▼
ContextEngineHook.awrap_before_agent(state)
    │  システムプロンプトをロード、対話を復元、スレッド変数を設定
    ▼
┌──────────────────────────────────────────────┐
│              LLM CALL (Agent Node)            │
│  → アシスタントメッセージ (text + tool_calls)   │
└──────────────────────────────────────────────┘
    │
    ▼
ContextEngineHook.awrap_after_agent(state)
    │  MesMemoryに永続化、ナッジ実行、知識グラフ保守
    ▼
MultimodalProcessor.awrap_after_agent(state)
    │  履歴から解決済みURIをクリーンアップ
    ▼
HeartbeatStaleness.awrap_after_agent(state)  [ワーカーエージェントのみ]
    │  ハートビートタイマーを停止
    ▼
IterationBudget.awrap_after_agent(state)
    │  (通常はno-op)
    ▼
ToolGuardrails.awrap_after_agent(state)
    │  ツール呼び出し履歴ウィンドウを更新
    ▼
ToolCallNormalize.awrap_after_agent(state)
    │  last_names追跡を更新
    ▼
Summarization.awrap_after_agent(state)
    │  要約結果を保存
    │
    ▼
[アシスタントメッセージの各tool_callについて:]
    │
    ├─ ToolCallNormalize.awrap_tool_call(state, tc)
    │     不正なid/nameペアを修復
    ├─ ToolGuardrails.awrap_tool_call(state, tc)
    │     しきい値超過時にブロックまたは停止
    ├─ IterationBudget.awrap_tool_call(state, tc)
    │     (通常はno-op)
    ├─ HeartbeatStaleness.awrap_tool_call(state, tc)  [ワーカーエージェントのみ]
    │     現在のツールを追跡、killedならHeartbeatTimeoutErrorを発生
    ├─ MultimodalProcessor.awrap_tool_call(state, tc)
    │     (通常はno-op)
    └─ ContextEngineHook.awrap_tool_call(state, tc)
           スキルレビューカウンタを増加 (ナッジ副作用)
    │
    ▼
    [ツールが実行される]
    │
    ▼
    [各ツール結果について:]
    │
    ├─ IterationBudget.awrap_after_tool(state)
    │     反復カウンタを増加
    ├─ ToolGuardrails.awrap_after_tool(state)
    │     将来の検出のために結果を登録
    ├─ ToolCallNormalize.awrap_after_tool(state)
    │     (通常はno-op)
    ├─ Summarization.awrap_after_tool(state)
    │     (通常はno-op)
    ├─ MultimodalProcessor.awrap_after_tool(state)
    │     (通常はno-op)
    └─ ContextEngineHook.awrap_after_tool(state)
           (per-toolではなくawrap_after_agent経由で実行)
    │
    ▼
[最終回答まで次の反復のためにbefore_agentにループバック]
```

---

## カスタムミドルウェアの作成

```python
from agent.middlewares.base import AgentMiddleware

class MyCustomMiddleware(AgentMiddleware):
    """Custom middleware example."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.my_param = config.get("my_param", "default")

    async def awrap_before_agent(self, state: AgentState) -> AgentState:
        # Runs before each LLM call
        state.state_register_mem["my_middleware"] = {"started": True}
        return state

    async def awrap_after_agent(self, state: AgentState) -> AgentState:
        # Runs after each LLM call
        return state

    async def awrap_tool_call(
        self, state: AgentState, tool_call: ToolCall
    ) -> AgentState:
        # Runs before each individual tool execution
        if tool_call["name"] == "sensitive_tool":
            # Add guard logic here
            pass
        return state

    async def awrap_after_tool(
        self, state: AgentState
    ) -> AgentState:
        # Runs after each tool returns
        return state
```

それをエージェントビルダーに登録します:

```python
middlewares = [
    # ...existing middlewares...
    MyCustomMiddleware(config={"my_param": "value"}),
]
```

---

## 付録

### ファイル構成

```
agent/middlewares/
├── __init__.py                   # Public exports
├── summarization.py              # Summarization
├── tool_call_normalize.py        # ToolCallNormalize
├── tool_guardrails.py            # ToolGuardrails
├── iteration_budget.py           # IterationBudget
├── heartbeat_staleness.py        # HeartbeatStaleness
├── multimodal_processor.py       # MultimodalProcessor
├── context_engine/
│   ├── __init__.py
│   ├── core.py                   # ContextEngineHook (main)
│   └── nudge.py                  # Nudge logic (memory/skill review)
├── README.md                     # This file
├── README.zh.md                  # Chinese version
├── README.ko.md                  # Korean version
└── README.ja.md                  # Japanese version
```

### エクスポート (`__init__.py`)

```python
from .summarization import Summarization
from .tool_guardrails import ToolGuardrails
from .iteration_budget import IterationBudget
from .context_engine import ContextEngineHook
from .tool_call_normalize import ToolCallNormalize
from .heartbeat_staleness import HeartbeatStaleness
from .multimodal_processor import MultimodalProcessor
```

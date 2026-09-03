# MesMemory — セッションメッセージメモリシステム

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **MesMemory** は EMA AI Agent の短期会話メモリエンジン（`context_engine` パッケージ）です。SQLite によるセッションメッセージの永続化、履歴取得、FTS5 全文検索を担います。このパッケージには、バックグラウンドでスキル保守を行う **Curator** サブパッケージも含まれています — [Curator（スキル保守サブパッケージ）](#curatorスキル保守サブパッケージ) を参照してください。

---

## 目次

- [概要](#概要)
- [パッケージ構成](#パッケージ構成)
- [データモデル](#データモデル)
- [コア機能](#コア機能)
- [統合ポイント](#統合ポイント)
- [Curator（スキル保守サブパッケージ）](#curatorスキル保守サブパッケージ)
- [API リファレンス](#api-リファレンス)
- [FAQ](#faq)
- [技術スタック](#技術スタック)

---

## 概要

### 設計上の位置づけ

MesMemory は**セッション単位の短期メッセージストア**であり、意図的にシンプルに設計されています。すべての保存・取得は SQL/FTS5 ベースで行われ、このパッケージにはベクトル埋め込み、グラフアルゴリズム、リランカー（reranker）は一切含まれません。

| | MesMemory |
|---|-----------|
| 対象 | 各セッションの生の `human` / `ai` / `tool` メッセージ |
| ストレージ | 共有の単一 SQLite データベース（`src/store/mes_memory/mes_memory.db`） |
| 取得 | 直近 N ターン、ターン範囲クエリ、ページング履歴、FTS5 全文検索 |
| 書き込み | `await add_messages(...)` — 1 回の呼び出しで 1 ターンを永続化 |

Agent が生成したスキルの長期保守（ライフサイクル遷移・統合・整理）は、`context_engine/` 内の独立した [Curator](#curatorスキル保守サブパッケージ) サブパッケージが担当します。Curator はメッセージデータには**一切触れません**。

### コア機能

1. **メッセージ永続化** — 各対話ターンの `human`/`ai`/`tool` メッセージを SQLite に書き込む
2. **履歴取得** — 直近 N ターン、ターン範囲、ページングで履歴を取得
3. **全文検索** — FTS5 ベースの対話検索。CJK クエリ用の trigram 経路、LIKE フォールバック、コンテキストプレビュー付き
4. **セッション管理** — トップレベルセッションの一覧（派生タイトル付き）。セッションの全メッセージ削除

---

## パッケージ構成

```
context_engine/
├── __init__.py          # パッケージのエクスポート（store と core の API を再エクスポート）
├── core.py              # ビジネス層：履歴フォーマット、FTS5 検索
├── store/
│   ├── __init__.py      # ストア層のエクスポート
│   ├── db.py            # SQLite 接続、WAL モード、バージョン管理されたマイグレーション（テーブル・インデックス・FTS5 トリガー）
│   └── core.py          # メッセージ CRUD：追加/照会/削除 + セッション一覧
└── curator/             # バックグラウンドのスキル保守オーケストレーター（専用 README あり）
```

```
┌──────────────────────────────────────────────────────┐
│                    context_engine                    │
├──────────────────────┬───────────────────────────────┤
│  store/ （データ層）  │     core.py （ビジネス層）     │
├──────────────────────┼───────────────────────────────┤
│ • db.py              │ • retrieve_history_by_last_   │
│   - SQLite 接続      │   n_prompt() → 整形済み対話    │
│   - WAL + 移行       │ • search_messages() → FTS5 /  │
│ • core.py            │   trigram / LIKE ルーティング  │
│   - add_messages     │ • _sanitize_fts5_query()      │
│   - ターン範囲クエリ  │   クエリのサニタイズ           │
│   - ページング履歴    │ • _decode_content()           │
│   - セッション一覧    │   JSON コンテンツのデコード    │
└──────────────────────┴───────────────────────────────┘
```

### パッケージのエクスポート（`__init__.py`）

```python
# context_engine/__init__.py
from .store import *   # get_db, add_messages, get_messages_by_lastest_n_turns,
                       # get_turns_by_turn_num_scope, get_history_by_turn_page,
                       # get_session_ids, delete_messages_by_session
from .core import retrieve_history_by_last_n_prompt, search_messages
```

---

## データモデル

### データベーススキーマ

```sql
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_num      INTEGER NOT NULL,   -- ターン番号（add_messages 1 回の呼び出し = 1 ターン）
    session_id    TEXT NOT NULL,      -- セッション ID
    role          TEXT NOT NULL,      -- human / ai / tool
    content       TEXT,               -- メッセージ内容（json.dumps, ensure_ascii=False）
    tool_call_id  TEXT,               -- ツール呼び出し ID（tool メッセージ）
    tool_calls    TEXT,               -- ツール呼び出し詳細（JSON、AI メッセージ）
    tool_status   TEXT,               -- ツール実行ステータス（デフォルト "success"）
    tool_name     TEXT,               -- ツール名
    timestamp     TEXT NOT NULL,      -- タイムスタンプ YYYYMMDDHHmmss（同バッチで共有）
    finish_reason TEXT,               -- AI レスポンスの終了理由
    reasoning     TEXT,               -- 思考連鎖（additional_kwargs["reasoning_content"]）
    reasoning_content TEXT,           -- 推論過程
    images        TEXT,               -- 画像パスの JSON リスト（human のマルチモーダル入力）
    audios        TEXT,               -- 音声パス/参照の JSON リスト
    videos        TEXT,               -- 動画パス/参照の JSON リスト
    model_name    TEXT,               -- AI メッセージ：レスポンスを生成したモデル
    input_tokens  INTEGER,            -- AI メッセージ：usage_metadata の入力トークン
    output_tokens INTEGER,            -- AI メッセージ：usage_metadata の出力トークン
    origin        TEXT                -- メッセージの送信元タグ（完了キャリアは "subagent_completion"、それ以外は NULL）
);
```

**インデックス：**

- `idx_messages_timestamp` — `(session_id, timestamp)`
- `idx_messages_turn_num` — `(session_id, turn_num)`

**FTS5 テーブル**（どちらも `content`、`tool_name`、`tool_calls` を連結したテキストを索引化）：

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
```

**FTS5 トリガー：** 各 FTS テーブルには `messages` テーブルに対する `AFTER INSERT` / `AFTER UPDATE` / `AFTER DELETE` トリガーがあり、インデックスを自動的に同期します。したがって、行を削除しても（例：`delete_messages_by_session`）、FTS の別途クリーンアップは不要です。

**マイグレーション：** スキーマ作成は `_migrations` テーブルでバージョン管理されています。手順は次の順序です：
`build_messages_tb` → `build_messages_fts_tb` → `build_messages_fts_trigram_tb` → `add_images_column` → `add_audio_video_columns` → `add_model_token_columns` → `add_origin_column`。

---

## コア機能

### 1. メッセージの永続化

```python
from context_engine.store import add_messages

# 1 対話ターンを永続化（turn_num は呼び出しごとに自動増分）
await add_messages("session_001", [user_msg, ai_msg])
```

- `add_messages` 1 回の呼び出し = 1 ターン：同バッチのメッセージは同じ `turn_num` と同じ `YYYYMMDDHHmmss` タイムスタンプを共有します
- 要約圧縮によって生成された `human` メッセージ（`additional_kwargs["lc_source"] == "summarization"` で識別）は除外されます
- `ai` メッセージは `tool_calls`（JSON）、`additional_kwargs["reasoning_content"]` からの思考連鎖（`reasoning` 列に保存）、レスポンス/使用量メタデータの `model_name` / `input_tokens` / `output_tokens` を永続化します（いずれも省略可能で、欠落時は `None`）
- `human` メッセージは `additional_kwargs` 内のマルチモーダルファイル参照を `images` / `audios` / `videos` 列に永続化します（JSON リスト、空の場合は `None`）
- `tool` メッセージは `tool_call_id`、`tool_name`、`tool_status`（デフォルト `"success"`）を永続化します
- メタデータが `internal: true` かつ `provenance: "subagent_completion"` である `human` メッセージ（ステアリングキューの完了キャリア）は `origin = 'subagent_completion'` として永続化されます。それ以外の行の `origin` はすべて `NULL` です（空文字列にも JSON にもなりません）

### 2. 履歴取得

```python
from context_engine import retrieve_history_by_last_n_prompt

# 直近 5 ターンを取得し、prompt 文字列として整形
history = retrieve_history_by_last_n_prompt("session_001", n=5)
```

**出力フォーマット**（`core.py` からの逐語。ターン本文にタイムスタンプは含まれません）：

```
===== The following is the content of the last 5 turns (from oldest to newest, timestamp format: YYYYMMDDHHmmss) =====

<turn>
user: User message

agent: AI response
</turn>

...

===== The above is the content of the last 5 turns =====

```

`human` メッセージの内容がマルチモーダルリストの場合、最初の `{"type": "text"}` 部分のみが使用されます。

ターン範囲クエリにも対応しています：

```python
from context_engine.store import get_turns_by_turn_num_scope

# target_turn_num の前後 5 ターンずつを取得
rows = get_turns_by_turn_num_scope("session_001", target_turn_num=10, half_scope=5)
```

ページングされた履歴取得（ページ 1 が最新ページ）：

```python
from context_engine.store import get_history_by_turn_page

# ページ 1 を 1 ページ 10 ターンで取得
rows = get_history_by_turn_page("session_001", min_turn_num=1, turn_page_size=10, turn_page_num=1)
```

ターン範囲クエリとページングクエリはいずれも、ターンの新しい順に行を返し、JSON エンコードされた `content`、`tool_calls`、`images`、`audios`、`videos` 列は Python オブジェクトへデコードされます。

### 3. 全文検索

```python
from context_engine import search_messages

# "Docker" を含むメッセージを検索（コンテキストプレビュー付き）
results = search_messages(
    query="Docker",
    session_id="session_001",
    role_filter=["human", "ai"],
    limit=20,
    offset=0,
)

for r in results:
    print(r["snippet"])        # ハイライト済みスニペット（マーカー：>>> match <<<）
    print(r["context"])        # 最大 3 件：前のメッセージ、一致メッセージ、次のメッセージ
```

**検索の特徴：**

- **FTS5 テーブル 2 種**：`messages_fts`（デフォルトの unicode61 トークナイザー）と `messages_fts_trigram`（trigram トークナイザー、CJK 部分一致に対応）
- **自動ルーティング**：非 CJK クエリは `messages_fts` へ。CJK 文字の合計が 3 以上で、3 文字未満の CJK トークンを含まない CJK クエリは trigram テーブルへ。それ以外は LIKE にフォールバック
- **トークン単位の CJK チェック**：`广西 OR 桂林 OR 漓江` のような複数語クエリはトークン単位で判定 — いずれかの CJK トークンが 3 文字未満の CJK 文字しか持たない場合、クエリ全体が LIKE に回ります（trigram はトークンごとに 3 文字以上の CJK 文字を要求）
- **LIKE フォールバック**：演算子以外の各トークンについて、`content`・`tool_name`・`tool_calls` に対する LIKE 条件を 1 つずつ生成（`ESCAPE '\'` 付き）。`timestamp DESC` 順でソート。スニペットは最初のトークン出現位置を中心とした 120 文字のウィンドウ
- **クエリのサニタイズ**（`_sanitize_fts5_query`）：対になった引用句を保持し、対になっていない FTS5 特殊文字を除去し、連続する `*` を 1 つにまとめ、宙ぶらりんの `AND`/`OR`/`NOT` を削除し、ハイフン・ドット・アンダースコアを含む語（例：`my-app.config.ts`）は引用符で囲んで FTS5 に句として扱わせます
- **Trigram トークンの引用符付け**：trigram 経路では、演算子以外の各トークンを二重引用符で囲み、ブール演算子（`AND`、`OR`、`NOT`）は保持します
- **コンテキスト拡張**：各一致には最大 3 エントリのコンテキストが付きます — 前のメッセージ、一致メッセージ自身、次のメッセージ（`timestamp`、次いで `id` 順）。各エントリは `{"role": ..., "content": preview}` としてレンダリングされ、preview は 200 文字に切り詰められます。マルチモーダルリストの内容はテキスト部分を連結して表示し、テキストがなければ `[multimodal content]` を表示します
- **結果の絞り込み**：完全な `content` フィールドは結果から取り除かれます（snippet と context のみ）。トークン消費を抑えるためです
- **エラー耐性**：空のクエリ／サニタイズ後に空になるクエリは `[]` を返します。MATCH による FTS5 `sqlite3.OperationalError` は握りつぶされて `[]` を返します
- **スレッドセーフ**：すべての DB アクセスはモジュールレベルの `threading.Lock` で保護されます
- **並び順**：FTS5 経路は関連度順（`ORDER BY rank`）、LIKE 経路は `timestamp DESC` 順

---

## 統合ポイント

確認済みの `context_engine` パッケージの利用箇所：

| エントリポイント | インポート | 用途 |
|------------------|-----------|------|
| `agent/middlewares/context_engine/core.py` → `ContextEngineHook` | `add_messages` | Agent ミドルウェア（`agent/core.py` のメイン Agent に登録）。`aafter_agent` で最終ターンを切り出し（`slice_last_turn`）、サニタイズ（`sanitize_tool_use_result_pairing`）した上で `add_messages()` により永続化。またシステムプロンプトを注入し（`wrap_model_call`/`awrap_model_call`）、メモリ/スキル nudge カウンタ（しきい値 10）と nudge サブエージェントを実行。詳細は `agent/middlewares/README.md` を参照。 |
| `agent/tools/message_search.py` → `message_search` ツール | `get_db`、`search_messages`、`get_turns_by_turn_num_scope` | セッション横断の想起ツール：FTS5 検索（limit 50）→ 一致ごとにターン範囲取得 → LLM によるセッション要約。query がない場合は直近セッションのメタデータを返す |
| `server/service/messages.py` | `get_session_ids`、`get_history_by_turn_page`、および（`context_engine.curator` からの）`reset_idle_for_seconds` | クライアント向けセッション一覧（トップレベルセッション + 派生タイトル）、ページング履歴、ユーザーターンごとの curator アイドルタイマーリセット |
| `server/DAO/messages.py` | `delete_messages_by_session` | 「セッションをクリア」操作 |
| `server/trigger/http/stats.py` | `get_db`（`context_engine.store.db` から） | messages テーブルに基づく利用統計 |
| `server/__main__.py` | `context_engine.curator.init()` | curator のバックグラウンドデーモンスレッドを明示的に開始する（パッケージのインポートに副作用はない） |

---

## Curator（スキル保守サブパッケージ）

`context_engine/curator/` は**バックグラウンドのスキル保守オーケストレーター**であり、メッセージストレージとは無関係です。確認済みの動作の概要：

- **対象**：`skills/auto/` 配下の Agent 生成スキルのみ。組み込みスキルには一切触れません
- **トリガー**：サービスのエントリポイントが `context_engine.curator.init()` を呼び出すと、デーモンスレッド（`curator-timer`）が起動し、3600 秒ごとに `maybe_run_curator()` を呼び出します。実行は `should_run_now()` が真（有効・一時停止でない・`interval_hours` 経過）で、Agent が十分アイドル（`min_idle_hours`）の場合にのみ行われます。ユーザーターンごとに `reset_idle_for_seconds()` が呼ばれます（`server/service/messages.py`）
- **ライフサイクル**：`active → stale`（`stale_after_days`、デフォルト 30 日間無活動）。`archive_after_days`（デフォルト 90 日）を超えたスキルはディスクから削除されます。stale ウィンドウ内で一度も使われていないスキルは再アクティブ化されます。pinned スキルはすべての遷移をバイパスします
- **LLM 統合**（`curator.yaml` でオプトイン、デフォルト `consolidate: false`）：重複する狭いスキルを LLM が生成した umbrella スキルへ統合します
- **状態とレポート**：実行状態は `skills/.curator_state` に保存。レポートは `logs/curator/{timestamp}/` 配下（`run.json` + `REPORT.md`）

公開 API には `run_curator_review(on_summary=None, dry_run=False, consolidate=None)`、`maybe_run_curator(*, idle_for_seconds=None, on_summary=None)`、`reset_idle_for_seconds()`、`pin_skill(name)`、`unpin_skill(name)`、`delete_skill(name, absorbed_into="")`、`apply_automatic_transitions(now=None)`、`should_run_now(now=None)` が含まれます。

▶️ 詳細：[curator/README.md](curator/README.md) · [中文](curator/README.zh.md) · [한국어](curator/README.ko.md) · [日本語](curator/README.ja.md)

---

## API リファレンス

以下のシグネチャはソースからそのままコピーしたもので、層ごとのインポートパスを併記しています。

### ビジネス層（`context_engine.core`、パッケージレベルで再エクスポート）

#### `retrieve_history_by_last_n_prompt(session_id: str, n: int = 5) -> str`
直近 `n` ターンを prompt 文字列として整形します（出力フォーマットは上記参照）。

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `session_id` | `str` | セッション ID |
| `n` | `int` | ターン数（デフォルト 5） |

**戻り値：** `str` — 整形済みの対話履歴

---

#### `search_messages(query: str, session_id: str, role_filter: list[str] = None, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]`
メッセージを全文検索します。

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `query` | `str` | 検索クエリ（空 → `[]`） |
| `session_id` | `str` | セッション ID |
| `role_filter` | `list[str]` | ロールフィルタ（例：`["human", "ai"]`、デフォルト `None`） |
| `limit` | `int` | 最大結果数（デフォルト 20） |
| `offset` | `int` | オフセット（デフォルト 0） |

**戻り値：** `list[dict[str, Any]]` — 各結果は `id`、`session_id`、`turn_num`、`role`、`snippet`、`timestamp`、`tool_name`、`context` を含みます（完全な `content` フィールドは取り除かれています）

---

#### `_sanitize_fts5_query(query: str) -> str`（内部）
ユーザー入力を安全な FTS5 MATCH クエリ用にサニタイズします。

#### `_decode_content(content: Any) -> Any`（内部）
`\x00json:` 接頭辞を持つメッセージ内容文字列をデコードします。その他の値はそのまま返します。

---

### ストア層（`context_engine.store`）

#### `async add_messages(session_id: str, messages: list[BaseMessage]) -> None`
LangChain メッセージのバッチを新しい 1 ターンとして永続化します。

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `session_id` | `str` | セッション ID |
| `messages` | `list[BaseMessage]` | LangChain `BaseMessage` のリスト（`human` / `ai` / `tool`） |

---

#### `get_messages_by_lastest_n_turns(session_id: str, last_n: int = 5) -> list[dict]`
直近 `last_n` ターンのメッセージ行を取得します（内部的に `get_history_by_turn_page` のページ 1 に委譲）。

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `session_id` | `str` | セッション ID |
| `last_n` | `int` | ターン数（デフォルト 5） |

**戻り値：** `list[dict]` — メッセージ行。ターンの新しい順、JSON 列はデコード済み

---

#### `get_turns_by_turn_num_scope(session_id: str, target_turn_num: int, half_scope: int = 5) -> list[dict]`
対象ターン番号の前後一定範囲のメッセージを取得します（範囲は `[1, max_turn_num]` にクランプされます）。

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `session_id` | `str` | セッション ID |
| `target_turn_num` | `int` | 対象ターン番号 |
| `half_scope` | `int` | 前後それぞれのターン数（デフォルト 5） |

**戻り値：** `list[dict]` — メッセージ行。ターンの新しい順、JSON 列はデコード済み

---

#### `get_history_by_turn_page(session_id: str, min_turn_num: Annotated[int, Field(ge=1)] = 1, turn_page_size: Annotated[int, Field(ge=1)] = 10, turn_page_num: Annotated[int, Field(ge=1)] = 1) -> list[dict]`
最新ターンから遡ってターン番号単位でページングした履歴を取得します（`@validate_call` で装飾）。

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `session_id` | `str` | セッション ID |
| `min_turn_num` | `int` | `turn_num` の下限（含む、≥1、デフォルト 1） |
| `turn_page_size` | `int` | 1 ページあたりのターン数（≥1、デフォルト 10） |
| `turn_page_num` | `int` | 最新ターンから遡る 1 起点のページ番号（≥1、デフォルト 1） |

**戻り値：** `list[dict]` — メッセージ行。ターンの新しい順、JSON 列はデコード済み

---

#### `get_max_turn_num(session_id: str) -> int`
セッションの最大 `turn_num`。メッセージがない場合は `0`。`context_engine/store/core.py` に定義されています（`context_engine.store` からは再エクスポートされません）。

---

#### `delete_messages_by_session(session_id: str) -> int`
セッションの全メッセージを削除します。FTS5 インデックスはトリガーにより自動的にクリーンアップされます。

**戻り値：** `int` — 削除された行数

---

#### `get_session_ids() -> list[dict]`
異なるトップレベルセッションをすべて列挙します（`:subagent:` を含むサブエージェントセッションは除外）。最近のアクティビティ順。

**戻り値：** `list[dict]` — 各項目は `{"session_id": str, "last_time": str, "title": str}`。`last_time` は最新の `YYYYMMDDHHmmss` タイムスタンプ、`title` は最新の `human` メッセージから派生（`""` の場合あり）

タイトルクエリは `origin IS NULL` の行のみを対象とします。`human` 行がすべて `subagent_completion` キャリアであるセッションのタイトルは空文字列になり、クライアントがプレースホルダーを表示します。

---

#### `get_db()`（`context_engine.store.db`）
共有の `sqlite3.Connection` を返します（初回呼び出し時に作成。`check_same_thread=False`、`timeout=1.0`、`isolation_level=None`、`row_factory=sqlite3.Row`、`PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`）。

---

## FAQ

### Q1: MesMemory と Curator の関係は？

同じパッケージに存在しますが、実行時には無関係です：MesMemory は生のセッションメッセージの保存・取得（短期メモリ）を担い、Curator は `skills/auto/` 配下の Agent 生成スキルの保守（ライフサイクル遷移・統合・整理）を担います。Curator が `messages` テーブルを読み書きすることはありません。

---

### Q2: FTS5 テーブルが 2 つあるのはなぜ？

`messages_fts` はデフォルトの unicode61 トークナイザーを使い、英語的なトークンマッチングに適しています。`messages_fts_trigram` は trigram トークナイザーを使い、テキストを 3 文字の n-gram 部分文字列に分割するため、CJK の部分一致が可能になります（unicode61 は CJK テキストを 1 文字単位に分割して誤検出を招きます）。ルーターはクエリの CJK 内容とトークン長に基づいてテーブルを選択します。

---

### Q3: 検索結果の `snippet` と `content` の違いは？

FTS5 経路では、`snippet` は一致部分を `>>>` / `<<<` マーカーで囲んだ FTS5 の抜粋（40 トークンのウィンドウ）です。LIKE 経路では、`snippet` は最初のトークン出現位置を中心とした `content` の 120 文字スライス（マーカーなし）。トークン節約のため、完全な `content` フィールドはすべての結果から取り除かれます。完全な内容が必要な場合は `get_messages_by_lastest_n_turns` / `get_history_by_turn_page` を使用してください。

---

### Q4: トークン単位の CJK ルーティングはどう機能する？

CJK クエリでは、演算子以外の各トークンが個別に判定されます。いずれかの CJK トークンが 3 文字未満の CJK 文字しか持たない場合、trigram FTS5 はマッチできず（トークンごとに 3 文字以上の CJK 文字を要求）、クエリ全体が LIKE 検索にフォールバックします。これにより、`"广西 OR 桂林 OR 漓江"` のように各語が 2 文字の CJK しかないケース（CJK 文字の合計は 6）も正しく処理されます。

---

## 技術スタック

| コンポーネント | 技術 |
|----------------|------|
| **データベース** | SQLite 3 — WAL モード、`foreign_keys=ON`、単一の共有接続（`check_same_thread=False`、`timeout=1.0`） |
| **全文検索** | FTS5（unicode61）+ FTS5（trigram トークナイザー） |
| **メッセージモデル** | LangChain `BaseMessage` |
| **バリデーション** | Pydantic `@validate_call`（`get_history_by_turn_page` で使用） |
| **並行制御** | すべての DB アクセスを `threading.Lock` で保護 |
| **ストレージパス** | `src/store/mes_memory/mes_memory.db`（`config.path.SRC_DIR / "store/mes_memory/mes_memory.db"`） |

---

## ライセンス

本プロジェクトは EMA AI Agent のオープンソースライセンスに従います。

---

**最終更新：** 2026-09-02

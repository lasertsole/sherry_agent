# MesMemory — セッションメッセージメモリシステム

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **MesMemory** は EMA AI Agent の短期的な会話メモリエンジンで、メッセージの永続化、履歴の取得、全文検索を担当します。

---

## 目次

- [概要](#概要)
- [アーキテクチャ](#アーキテクチャ)
- [データモデル](#データモデル)
- [コア機能](#コア機能)
- [API リファレンス](#api-リファレンス)
- [FAQ](#faq)

---

## 概要

### 設計上の位置づけ

MesMemory は [Skill Memory](../skill_memory/README.md) を補完します:

| Skill Memory | MesMemory |
|-------------|-----------|
| 長期的な知識グラフ (TASK/SKILL/EVENT) | 短期的なセッションメッセージの保存 |
| 構造化されたトリプル、セッション間で再利用 | 生のメッセージシーケンス、セッションごとに分離 |
| グラフコミュニティ + PageRank 再呼び出し | FTS5 全文検索 + ターン範囲クエリ |
| 非同期バックグラウンド抽出 | 同期書き込み、即時永続化 |

### コア機能

1. **メッセージの永続化** — 各対話ターンの human/ai/tool メッセージを SQLite に書き込む
2. **履歴の取得** — 直近 N ターン、ページネーションされた履歴、または特定のターン範囲をフォーマット済みのコンテキストとして取得
3. **全文検索** — FTS5 ベースの対話検索、中国語対応 (trigram) とコンテキストプレビュー

---

## アーキテクチャ

```
┌────────────────────────────────────────────────────┐
│                   context_engine                     │
├───────────────────┬────────────────────────────────┤
│    store/         │          core.py                │
│   (データ層)      │      (ビジネスロジック)        │
├───────────────────┼────────────────────────────────┤
│ • db.py           │ • retrieve_history_by_last_n   │
│   - SQLite 接続    │   _prompt() → フォーマット     │
│   - マイグレーション│   された会話文字列              │
│ • core.py         │ • search_messages() → FTS5     │
│   - CRUD 操作      │   検索 + コンテキスト           │
│   - メッセージ書き込み│ • _sanitize_fts5_query()     │
│   - ターンクエリ   │   クエリのサニタイズ            │
│   - ページネーション│ • _decode_content()          │
│     履歴           │   JSON コンテンツのデコード     │
└───────────────────┴────────────────────────────────┘
```

### ストア層 (`store/`)

| ファイル | 責務 |
|------|---------------|
| `store/db.py` | SQLite 接続管理、WAL モード、自動マイグレーション (テーブル、インデックス、FTS5 トリガー) |
| `store/core.py` | メッセージ CRUD: `add_messages`、`get_messages_by_lastest_n_turns`、`get_turns_by_turn_num_scope`、`get_history_by_page`、`get_max_turn_num` |

### ビジネス層 (`core.py`)

| 関数 | 責務 |
|----------|---------------|
| `retrieve_history_by_last_n_prompt(session_id, n)` | 直近 N ターンを取得し、プロンプトコンテキストとしてフォーマット |
| `search_messages(query, session_id, ...)` | FTS5 全文検索、中国語 trigram サポートとコンテキスト拡張 |
| `_sanitize_fts5_query(query)` | 安全な FTS5 MATCH クエリのためのユーザー入力のサニタイズ (内部) |
| `_decode_content(content)` | JSON エンコードされたメッセージコンテンツの逆変換 (内部) |

### パッケージエクスポート (`__init__.py`)

```python
# context_engine/__init__.py
from .store import *                                              # get_db, add_messages, get_messages_by_lastest_n_turns, get_turns_by_turn_num_scope, get_history_by_page
from .core import retrieve_history_by_last_n_prompt, search_messages
```

---

## データモデル

### データベーススキーマ

```sql
-- Messages テーブル
CREATE TABLE messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_num      INTEGER NOT NULL,       -- ターンシーケンス番号
    session_id    TEXT NOT NULL,           -- セッション ID
    role          TEXT NOT NULL,           -- human / ai / tool
    content       TEXT,                   -- メッセージコンテンツ (JSON エンコード)
    tool_call_id  TEXT,                   -- ツール呼び出し ID
    tool_calls    TEXT,                   -- ツール呼び出しの詳細 (JSON)
    tool_status   TEXT,                   -- ツール実行ステータス
    tool_name     TEXT,                   -- ツール名
    timestamp     TEXT NOT NULL,          -- タイムスタンプ (YYYYMMDDHHmmss)
    finish_reason TEXT,                   -- AI 応答の終了理由
    reasoning     TEXT,                   -- 推論コンテンツ
    reasoning_content TEXT                -- 推論プロセス
);

-- FTS5 全文検索 (英語優先)
CREATE VIRTUAL TABLE messages_fts USING fts5(content);

-- FTS5 中国語 trigram 検索
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
```

**インデックス:**
- `idx_messages_timestamp` — `(session_id, timestamp)` 高速な時間範囲クエリ用
- `idx_messages_turn_num` — `(session_id, turn_num)` ターンベースのクエリ用

**FTS5 トリガー:** `messages` テーブルの INSERT/UPDATE/DELETE 時に FTS5 インデックスを自動同期します。インデックス化されるフィールドには `content`、`tool_name`、`tool_calls` が含まれます。

---

## コア機能

### 1. メッセージの永続化

```python
from context_engine.store import add_messages

# 対話ターンを書き込む (turn_num 自動インクリメント)
await add_messages("session_001", [user_msg, ai_msg])
```

- 3 つの役割 (human/ai/tool) すべてのメッセージが永続化されます
- 圧縮による human メッセージ (`lc_source == "summarization"` で識別) は除外されます
- 各メッセージは `YYYYMMDDHHmmss` タイムスタンプを持ちます
- コンテンツは構造化データ用に `\x00json:` プレフィックス付きで JSON エンコードされます

---

### 2. 履歴の取得

```python
from context_engine import retrieve_history_by_last_n_prompt

# 直近 5 ターンを取得し、プロンプト文字列としてフォーマット
history = retrieve_history_by_last_n_prompt("session_001", n=5)
```

**出力フォーマット:**

```
===== The following is the content of the last 5 turns (from oldest to newest, timestamp format: YYYYMMDDHHmmss) =====

<turn>
User: User message

Assistant: AI response
</turn>

...

===== The above is the content of the last 5 turns =====
```

ターン範囲クエリもサポートされています:

```python
from context_engine.store import get_turns_by_turn_num_scope

# target_turn_num の前後それぞれ 5 ターンを取得
rows = get_turns_by_turn_num_scope("session_001", target_turn_num=10, half_scope=5)
```

ページネーションされた履歴の取得:

```python
from context_engine.store import get_history_by_page

# 1 ページあたり 10 ターン、1 ページ目を取得
rows = get_history_by_page("session_001", min_turn_num=1, turn_page_size=10, turn_page_num=1)
```

---

### 3. 全文検索

```python
from context_engine import search_messages

# "Docker" を含むメッセージを検索、コンテキストプレビュー付き
results = search_messages(
    query="Docker",
    session_id="session_001",
    role_filter=["human", "ai"],
    limit=20,
    offset=0,
)

for r in results:
    print(r["snippet"])        # ハイライトされたスニペット
    print(r["context"])        # 前後のコンテキスト 1 メッセージ
```

**検索機能:**

- **デュアル FTS5 テーブル**: `messages_fts` (デフォルトの unicode61 トークナイザー) と `messages_fts_trigram` (trigram トークナイザー、中国語対応)
- **自動ルーティング**: 中国語クエリの検出 (トークンあたり CJK 3 文字以上) → trigram パス; それ以外 → デフォルト FTS5
- **グレースフルデグラデーション**: 短い中国語クエリ (トークンあたり CJK 3 文字未満) は LIKE 検索にフォールバック
- **トークンごとの CJK チェック**: "广西 OR 桂林 OR 漓江" のような複数語クエリはトークンごとにチェック — CJK トークンが 3 文字未満ならクエリ全体が LIKE にルーティング
- **クエリのサニタイズ**: FTS5 の特殊文字、引用符のバランス、ブール演算子のクリーンアップ、ハイフン/ドット用語の引用を自動処理
- **コンテキスト拡張**: 各結果は前後のコンテキスト 1 メッセージを含む
- **マルチモーダル対応**: 非テキストコンテンツ (画像など) は `[multimodal content]` として表示
- **トークン効率**: 結果は完全な `content` フィールドを省略 (スニペット + コンテキストのみ)
- **スレッド安全性**: すべての DB 操作はスレッディングロックで保護

---

## API リファレンス

### `retrieve_history_by_last_n_prompt(session_id, n=5)`
直近 N ターンを取得し、プロンプト文字列としてフォーマットします。

| パラメータ | 型 | 説明 |
|-----------|------|-------------|
| `session_id` | `str` | セッション ID |
| `n` | `int` | ターン数 (デフォルト: 5) |

**戻り値:** `str` — フォーマットされた会話履歴

---

### `search_messages(query, session_id, role_filter=None, limit=20, offset=0)`
メッセージの全文検索。

| パラメータ | 型 | 説明 |
|-----------|------|-------------|
| `query` | `str` | 検索クエリ |
| `session_id` | `str` | セッション ID |
| `role_filter` | `list[str]` | 役割フィルター (例: `["human", "ai"]`) |
| `limit` | `int` | 最大結果数 (デフォルト: 20) |
| `offset` | `int` | オフセット (デフォルト: 0) |

**戻り値:** `list[dict]` — 各結果は `id`、`session_id`、`turn_num`、`role`、`snippet`、`timestamp`、`tool_name`、`context` を含む

---

### `add_messages(session_id, messages)`
(ストア層) データベースにメッセージを書き込みます。

| パラメータ | 型 | 説明 |
|-----------|------|-------------|
| `session_id` | `str` | セッション ID |
| `messages` | `list[BaseMessage]` | LangChain BaseMessage リスト |

---

### `get_messages_by_lastest_n_turns(session_id, last_n=5)`
ストア層から直近 N ターンの生のメッセージレコードを取得します。

| パラメータ | 型 | 説明 |
|-----------|------|-------------|
| `session_id` | `str` | セッション ID |
| `last_n` | `int` | ターン数 (デフォルト: 5) |

**戻り値:** `list[dict]` — 各レコードはすべてのメッセージフィールドを含む

---

### `get_turns_by_turn_num_scope(session_id, target_turn_num, half_scope=5)`
対象ターン番号の周囲のターン範囲内のメッセージを取得します。

| パラメータ | 型 | 説明 |
|-----------|------|-------------|
| `session_id` | `str` | セッション ID |
| `target_turn_num` | `int` | 対象ターン番号 |
| `half_scope` | `int` | 両側のターン数 (デフォルト: 5) |

**戻り値:** `list[dict]` — 各レコードはデコードされた JSON を含むすべてのメッセージフィールドを含む

---

### `get_history_by_page(session_id, min_turn_num=1, turn_page_size=10, turn_page_num=1)`
ページネーションされた履歴メッセージを取得します。

| パラメータ | 型 | 説明 |
|-----------|------|-------------|
| `session_id` | `str` | セッション ID |
| `min_turn_num` | `int` | 最小ターン番号 (≥1、デフォルト: 1) |
| `turn_page_size` | `int` | 1 ページあたりのターン数 (≥1、デフォルト: 10) |
| `turn_page_num` | `int` | ページ番号 (≥1、デフォルト: 1) |

**戻り値:** `list[dict]` — 各レコードはデコードされた JSON を含むすべてのメッセージフィールドを含む

---

### `get_max_turn_num(session_id)`
セッションの最大ターン番号を取得します。

| パラメータ | 型 | 説明 |
|-----------|------|-------------|
| `session_id` | `str` | セッション ID |

**戻り値:** `int` — 最大ターン番号、メッセージが存在しない場合は 0

---

## FAQ

### Q1: MesMemory と Skill Memory の関係は何ですか？

MesMemory は**生のメッセージの保存と取得** (短期記憶) を処理します。Skill Memory は**知識の抽出とグラフ構築** (長期記憶) を処理します。MesMemory は「言われたこと」を保存し、Skill Memory は言われたことから抽出された構造化された知識を保存します。

---

### Q2: なぜ FTS5 テーブルが 2 つあるのですか？

`messages_fts` はデフォルトの unicode61 トークナイザーを使用し、英語とピンイン検索に適しています。`messages_fts_trigram` は trigram トークナイザーを使用し、テキストを 3-gram の部分文字列に分割するため、中国語の曖昧検索と部分文字列検索を自然にサポートします。システムはクエリ言語に基づいて自動選択します。

---

### Q3: 検索結果の `snippet` と `content` の違いは何ですか？

`snippet` は FTS5 が提供するハイライトマーカー付きの短い抜粋 (両側約 40 文字) で、一致位置のクイックプレビューに使用されます。`content` は完全なメッセージ本文ですが、トークンを節約するため検索結果からは省略されます。完全なコンテンツが必要な場合は、代わりに `get_messages_by_lastest_n_turns` を使用してください。

---

### Q4: トークンごとの CJK ルーティングはどのように機能しますか？

CJK クエリの場合、システムは各非演算子トークンを個別にチェックします。いずれかの CJK トークンが CJK 文字 3 文字未満の場合、trigram FTS5 はそれに一致できません (トークンあたり CJK 3 文字以上が必要) ので、クエリ全体が LIKE 検索にフォールバックします。これにより、各用語が CJK 文字 2 文字のみの `"广西 OR 桂林 OR 漓江"` のようなケースを処理します。

---

## 技術スタック

| コンポーネント | 技術 |
|-----------|-----------|
| **データベース** | SQLite 3 + WAL モード |
| **全文検索** | FTS5 + Trigram トークナイザー |
| **フレームワーク** | LangChain BaseMessage |
| **バリデーション** | Pydantic `@validate_call` |
| **保存パス** | `store/mes_memory/mes_memory.db` |

---

## ライセンス

このプロジェクトは EMA AI Agent のオープンソースライセンスに従います。

---

**最終更新:** 2026-07-09

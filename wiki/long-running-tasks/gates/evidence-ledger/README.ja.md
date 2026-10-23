# 📋 エビデンス台帳 — 検証監査トレイル

[English](README.md) · [中文](README.zh.md) · [한국어](README.ko.md) · **日本語**

> SQLite の監査トレイル。エージェントが実行したすべての検証コマンド（テスト・リント・ビルド）を記録し、読み取り時に各結果がまだ有効かを導出します — これにより、検証なしで「完了」を主張するサブエージェントや TaskFlow を完了ゲートが拒否できます。

エビデンス台帳はすべての完了境界で一つの問いに答えます：**「エージェントは自分の作業を検証したか、その検証はまだ有効か？」** 古さのフラグを保存せずにこれを実現します — イベント行は不変で、鮮度は読み取り時に検証行とファイル編集イベントの順序から計算されます（保持期間は古い行を削除するだけで履歴を書き換えません）。

**ソースコード:** `agent/tools/pub_base/evidence_ledger.py`（ストレージ）、`agent/tools/pub_base/evidence_recorder.py`（プロデューサー）、`agent/tools/taskflow/evidence_collector.py`（リーダー/コンシューマー）、`config/features/agent_side/evidence_ledger.py`（分類）。

---

## 仕組み

```
terminal / python_repl がコマンドを実行
  │
  ├─ 分類："pytest" にマッチ → kind=test、"ruff" → kind=lint など
  │        （分類は EVIDENCE_LEDGER["verify_commands"] に定義）
  │        マッチしないコマンドは黙って無視
  │
  ├─ record_verification_evidence()
  │    └─ SQLite の行を 1 つ挿入: {kind, command, exit_code, status, output_snippet, sandboxed, session_id, timestamp}
  │
  └─ その後: write_file / patch_file がソースファイルを編集
       └─ mark_evidence_stale(file_path, session_id)
            └─ SQLite の行を 1 つ挿入: {event:"stale", file_path, session_id, timestamp}
                 │
                 └─ 読み取り時: collect_evidence_summary()
                      ├─ evidence 行が [stale] になるのは、より後の stale イベントの file_path が
                      │   その行の command の部分文字列である場合
                      ├─ 方向が重要: evidence より前の stale = 新鮮（再検証済み）
                      └─ 返す: "  [pass] test: `pytest foo.py`\n  [FAIL] lint: `ruff .` [stale]"
```

---

## ストレージ

**ファイル:** `src/data/evidence-ledger.db`（`config.path.resolve_evidence_ledger_path` で解決）

| 項目 | 値 |
|------|----|
| 形式 | SQLite テーブル `evidence_events`；WAL モード + `busy_timeout=5000` |
| 範囲 | リポジトリ全体（全セッションで共有） |
| 可変性 | 行は INSERT のみで UPDATE されない。保持期間が古い行を削除する（age TTL / セッション上限 / 全体上限） |
| セッション分離 | インデックス付き `session_id` に対する `WHERE session_id = ?`。行は SESSION key（メインセッション id、または子の `agent:{agent}:subagent:{uuid}`）を持ち、plan / todo / flow id は持たない |
| 複数キー読み取り | `read_for_keys(keys)` は 1 回のインデックス付き `IN` クエリで取得し、各キーの挿入順を保つ（古さは 1 セッションの行順序内で導出） |
| 並行性 | WAL により読み書きが重なり、書き込みは `busy_timeout` で再試行 |
| 旧 JSONL | 初回オープン時に 1 回だけインポート（行ハッシュで重複排除）し、`.migrated` にリネーム |
| ファイル不在 | `read_all()` は `[]` を返す。ディレクトリは初回アクセス時に作成 |

DB は意図的にリポジトリ全体に置かれ、セッション単位ではない。同じ計画を検証する複数セッションで共有される監査トレイルである。

### 保持期間とパージ

`EVIDENCE_LEDGER` がポリシーを持つ：`pruning_enabled`（既定 true）、`max_age_days`（30）、`max_events_per_session`（100）、`max_total_events`（10 000）。プルーニングは遅延実行で、各 `append` の INSERT 後に 3 層の DELETE を走らせる。失敗しても直前に記録した行は失われない。セッション削除は `purge_session`（`DELETE WHERE session_id = ?` 1 回）を呼び、boot の孤児 sweep は `purge_orphans(known_sessions)` を呼ぶため、通常のクリーンアップ経路を迂回して削除されたセッションの evidence も消える。`purge_on_session_delete` / `purge_on_boot_sweep` が明示削除を制御し、`pruning_enabled=false` は遅延保持期間だけを無効化する。

---

## プロデューサー（何が記録されるか）

### 1. Terminal & Python REPL — 自動記録（常時オン）

すべての `terminal` と `python_repl` 呼び出しは、**すべての**終了パス（成功・非ゼロ終了・タイムアウト・キャンセル・例外）で `record_verification_evidence()` を経由します。レコーダーは:

1. `classify_verification_command()` でコマンドを分類 — `EVIDENCE_LEDGER["verify_commands"]` に対する小文字部分文字列マッチ:
   ```python
   "verify_commands": {
       "test": ["pytest", "jest", "vitest", "cargo test", "go test", "npm test"],
       "lint": ["ruff", "eslint", "flake8", "pylint", "clippy"],
       "build": ["cargo build", "npm run build", "make", "cmake"],
       "typecheck": ["basedpyright", "mypy", "tsc", "pyright"],
       "format": ["ruff format", "prettier", "black"],
   }
   ```
   どのパターンにもマッチしないコマンドは黙って無視されます（行は書かれない）。

2. 結果文字列から `exit_code` を導出（"Exit code N" / 失敗プレフィックスの正規表現マッチ）。
3. `status` = exit_code が 0 のとき `"passed"`、それ以外は `"failed"`。
4. `output_snippet`（先頭 500 文字）と `sandboxed` フラグを含む行を追記。

**Fail-open:** すべてのレコーダーは自身のエラーを飲み込む — 台帳の書き込みがツールを壊すことはない。

### 2. ファイル編集 — stale イベント

`write_file` と `patch_file` は `@invalidates_evidence(path_arg="file_path", session_arg="session_id")` でデコレートされています。デコレータは `finally` ブロックで実行されるため、**すべての**結果（成功・エラー文字列・例外送出）が `{"event": "stale", "file_path": "..."}` 行を追記します。

### 3. サブエージェントワークスペースマージ — stale イベント

子の隔離ワークスペースが親セッションにマージバックされるとき、`workspace_merge._mark_merged_paths_stale()` は適用/作成/削除されたすべてのパスに対して**親**セッション id で `mark_evidence_stale_for_paths()` を呼び出します。親のそれらのファイルに対する以前の検証は無効になります。

---

## 行スキーマ

### 検証エビデンス行

```json
{
  "kind": "test",
  "command": "pytest tests/test_foo.py",
  "exit_code": 0,
  "status": "passed",
  "output_snippet": "===== 3 passed in 0.42s =====",
  "sandboxed": true,
  "session_id": "agent:main:session:abc-123",
  "timestamp": "2026-10-15T08:30:00.123456+00:00"
}
```

### Stale イベント行

```json
{
  "event": "stale",
  "file_path": "src/foo.py",
  "session_id": "agent:main:session:abc-123",
  "timestamp": "2026-10-15T08:31:00.654321+00:00"
}
```

---

## 古さの導出

古さは**読み取り側で導出**され、フラグとして保存されません。ルール:

> evidence 行が stale になるのは、**より後の** stale イベント（追記順序でインデックスが大きい）の `file_path` がその行の `command` 文字列の**部分文字列**として現れる場合。

```python
def _is_stale(index, row, stale_events):
    command = str(row.get("command") or "")
    return any(
        stale_index > index
        and str(event.get("file_path") or "") in command
        for stale_index, event in stale_events
    )
```

重要なポイント:

- **方向が重要:** **より後の** stale イベントだけが**より前の** evidence 行を無効化します。evidence より前の stale イベントは、ファイルが編集された後に再検証されたことを意味し、検証は新鮮です。
- **部分文字列マッチ:** `file_path in command`。意図的に緩い — `src/foo.py` の stale イベントは `pytest src/foo.py tests/` を無効化します。
- **セッションスコープ:** `SessionEvidenceLedger.list_records()` が先に `session_id` でフィルタリングするため、他のセッションの stale イベントは交差汚染しません。

---

## エビデンスサマリー形式

`collect_evidence_summary(session_key=None, flow_id=None)` は複数行の文字列を返します（エビデンス行がない場合は `None`）:

```
  [pass] test: `pytest tests/test_foo.py`
  [FAIL] lint: `ruff check .`
  [pass] build: `npm run build` [stale]
  [?] test: `pytest bar.py`
```

1行の形式: `  [{icon}] {kind}: \`{command}\`{stale_tag}`

| 構成要素 | ソース |
|---------|--------|
| Icon | status が passed のとき `"pass"`、failed のとき `"FAIL"`、不明のとき `"?"` |
| Kind | 検証カテゴリ: test / lint / build / typecheck / format |
| Command | 実行コマンド文字列、バッククォートで囲む |
| Stale tag | `_is_stale` が True のとき `" [stale]"` を追加 |

この文字列をゲートとジャッジが解析します — リテラル部分文字列 `"FAIL"`、`"[stale]"`、`"[pass]"` を検索します。

---

## コンシューマー — 3つのゲーティングレベル

### レベル 1: ステップごと（TaskFlow step judge）

`taskflow_resume` が acceptance criteria を持つステップをディスパッチするとき、`judge_step_result()` に `evidence_summary=collect_evidence_summary(session_key=step.child_session_key)` を渡します。step judge のプロンプトには `## Verification Evidence` セクションが含まれ、サマリー（または `"(none recorded)"`）が表示されます。judge はこれを使って PASS / RETRY / BLOCK を決定します。

### レベル 2: サブエージェントごと（completion judge + drain gate）

- **Completion judge スキップ:** goal-loop の最初のターンで、`_first_turn_self_evident(result_text, evidence_summary)` がチェック: result が非空 + 少なくとも1つの `[pass]` + `FAIL` なし + `[stale]` なし → **judge 呼び出しをスキップ**、auxiliary LLM 呼び出しを1回節約。
- **Completion judge プロンプト:** スキップされない場合、`judge_completion()` がプロンプト内でエビデンスサマリーを受け取ります。
- **Drain gate:** `SubagentCompletionDrainMiddleware`（親側 `before_model`）がサブエージェント完了メッセージのドレイン時にエビデンスをチェック。エビデンスなしまたは `FAIL` → `[GATE] Completion blocked: verification evidence missing or failing` ブロッキングメッセージを注入。

### レベル 3: フローごと（taskflow_finish Gate C）

TaskFlow が DONE に遷移する前、Gate C はそのフローの**集約**エビデンスをチェックします——ディスパッチされた各ステップの `child_session_key` とフローの `creator_session_key` の和集合です（行はセッションキーで保存され、フローは複数セッションにまたがるため。一度もステップをディスパッチしていないフローはキーが無く、エビデンスも無いので通過します）:

| 条件 | 結果 |
|------|------|
| サマリーに `"FAIL"` が含まれる | 拒否: "failing verification evidence" |
| サマリーに `"[stale]"` が含まれる | 拒否: "stale evidence (code changed since last verification), re-run verification commands before finishing" |
| それ以外（またはエビデンスなし / collector エラー） | 通過 |

ゲート順序: A（DAG 完全性）→ B（未解決ステップなし）→ C（エビデンス）→ E（ゴール judge）。

---

## 設計原則

1. **いたるところで fail-open。** すべてのレコーダーは自身のエラーを飲み込む。すべてのゲートは collector エラー時に「通過/非違反」を返す。壊れた台帳は完了をブロックしたりサブエージェントを閉じ込めることはない。
2. **不変イベント行 + 読み取り時導出。** 記録済みの行は UPDATE されない。古さは読み取り時に行情報から計算される。保持期間は古い行を DELETE することがある（age/セッション/全体上限）が、履歴を書き換えない。reader は残存する行順序から鮮度を計算する。
3. **パス参照、コンテンツではない。** 台帳はコマンド文字列と出力スニペット（500文字）を記録し、ファイル内容は記録しない。*何を検証したか*と*通過したか*を記録し、*ファイルに何が含まれていたか*は記録しない。
4. **常時オン、トグルなし。** 自動記録パスにはオン/オフスイッチがない。分類にマッチするすべての terminal/python_repl 呼び出しが行を書く。すべてのファイル編集が stale イベントを書く。モデルはバイパスできない。

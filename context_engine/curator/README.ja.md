# Curator — バックグラウンドスキル保守オーケストレーター

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **Curator** は EMA AI エージェントのバックグラウンドスキル保守システムで、エージェントが作成したスキルのライフサイクル管理、統合、整理を担当します。

---

## 目次

- [概要](#概要)
- [主要な役割](#主要な役割)
- [アーキテクチャ](#アーキテクチャ)
- [トリガーメカニズム](#トリガーメカニズム)
- [ライフサイクル状態機械](#ライフサイクル状態機械)
- [実行フロー](#実行フロー)
- [自動遷移ルール](#自動遷移ルール)
- [LLM 統合](#llm-統合)
- [アンブレラスキル生成](#アンブレラスキル生成)
- [分類と調整](#分類と調整)
- [使用記録システム](#使用記録システム)
- [孤立レコードのクリーンアップ](#孤立レコードのクリーンアップ)
- [ピンメカニズム](#ピンメカニズム)
- [レポートシステム](#レポートシステム)
- [設定リファレンス](#設定リファレンス)
- [Curator 状態ファイル](#curator-状態ファイル)
- [不変条件](#不変条件)
- [ファイル構造](#ファイル構造)

---

## 概要

Curator は**非アクティブトリガー**のバックグラウンドタスクです。エージェントがアイドル状態で、最後の Curator 実行から `interval_hours` 以上経過すると、`maybe_run_curator()` がバックグラウンドレビューを開始します。

エージェントが作成したスキル（`skills/auto/` 配下）のみを操作し、**組み込みスキル**（`skills/builtin/`）には決して触れません。古くて未使用のスキルは**削除**（ディスクから削除）され、必要に応じて LLM 統合により重複スキルをアンブレラスキルへマージしてから整理します。

---

## 主要な役割

1. **自動ライフサイクル遷移** — スキル活動タイムスタンプに基づいて `active → stale` へ遷移; アーカイブ基準を超えたスキルを削除
2. **統合**（オプションの LLM パス）— 重複する狭いスキルをクラスレベルのアンブレラスキルにマージし、コンテンツ生成とファイル移行を自動化
3. **永続状態** — `.curator_state` ファイルに実行履歴を保存

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────┐
│  maybe_run_curator()                                            │
│    │                                                            │
│    ├── should_run_now()? ── No ──► return None                  │
│    │                                                            │
│    └── Yes ──► run_curator_review()                             │
│                  │                                              │
│                  ├── 1. Auto-transitions (apply_automatic_...)  │
│                  │     ├── Iterate agent_created_report()       │
│                  │     ├── Skip pinned                          │
│                  │     └── Mark stale / delete by cutoff times  │
│                  │                                              │
│                  ├── 2. LLM Consolidation (optional)            │
│                  │     ├── _render_candidate_list()             │
│                  │     ├── _run_llm_review(prompt)              │
│                  │     ├── _apply_consolidation()               │
│                  │     │     ├── _generate_umbrella_skill()     │
│                  │     │     └── Migrate support files          │
│                  │     └── Parse structured YAML output         │
│                  │                                              │
│                  └── 3. Report & Persist                        │
│                        ├── _build_rename_summary()              │
│                        ├── _write_run_report() → logs/curator/  │
│                        └── save_state() → .curator_state        │
└─────────────────────────────────────────────────────────────────┘
```

---

## トリガーメカニズム

Curator はスケジュール cron の代わりに**非アクティブトリガー**パターンを使用します:

```
maybe_run_curator(idle_for_seconds=..., on_summary=...)
  │
  ├── should_run_now() checks:
  │     ├── is_enabled() == False  → skip
  │     ├── is_paused() == True    → skip
  │     ├── last_run_at is None    → eligible (first run executes immediately)
  │     └── now - last_run_at >= interval_hours → eligible
  │
  └── idle_for_seconds < min_idle_hours * 3600 → skip
```

| パラメータ | デフォルト | 説明 |
|-----------|---------|-------------|
| `interval_hours` | 168（7日） | Curator 実行間の最小間隔 |
| `min_idle_hours` | 2 | エージェントは最低 N 時間アイドル状態である必要がある |

`last_run_at` が一度も設定されていない場合、最初の `should_run_now()` 呼び出しは `True` を返し、レビューは即座に進行します（遅延された初回実行シードはありません）。

---

## ライフサイクル状態機械

```
    active ──────(stale_after_days no activity)──────► stale
      ▲                                                 │
      │             (new activity / reactivation)        │
      └─────────────────────────────────────────────────┘
      │                                                 │
      │         (archive_after_days no activity)         │
      └──────────────────► deleted ◄─────────────────────┘
```

| 状態 | 意味 |
|-------|---------|
| `active` | スキルが通常利用可能 |
| `stale` | `stale_after_days` 間アクティビティがなく、古いとマークされた |

スキルが `archive_after_days` の非アクティブ期間を超えると**削除**されます（ディレクトリと使用記録がディスクから削除）。中間の `archived` 状態はなく、削除は不可逆です。

**主要な制約**:
- ピン留めされたスキルは**決して**自動遷移または削除されません
- stale 基準より後に作成された `use_count == 0` のスキルは、現在 stale 状態であれば**再アクティブ化**されます

---

## 実行フロー

### run_curator_review()

```
run_curator_review(on_summary=None, synchronous=True, dry_run=False, consolidate=None)
  │
  ├── 1. Auto-transition phase
  │     ├── dry_run=True → count only, no mutations
  │     └── dry_run=False → apply_automatic_transitions()
  │           ├── Mark stale
  │           ├── Delete (remove from disk)
  │           └── Reactivate
  │
  ├── 2. Save intermediate state
  │     └── last_run_at, run_count, last_run_summary
  │
  ├── 3. LLM consolidation (_llm_pass)
  │     ├── consolidate=False → skip, write report
  │     └── consolidate=True:
  │           ├── Snapshot before_report (skill list)
  │           ├── _render_candidate_list() → candidate list
  │           ├── _run_llm_review(prompt) → LLM invocation
  │           ├── _apply_consolidation(llm_final):
  │           │     ├── Parse structured YAML (consolidations + prunings)
  │           │     ├── For each umbrella: _generate_umbrella_skill()
  │           │     ├── Migrate support files (references/, templates/, scripts/, assets/)
  │           │     ├── Delete consolidated source skills
  │           │     └── Delete pruned skills
  │           ├── Snapshot after_report
  │           ├── _build_rename_summary() → classify changes
  │           └── _write_run_report() → logs/curator/{timestamp}/
  │
  ├── 4. Execution mode
  │     ├── synchronous=True → run on current thread
  │     └── synchronous=False → run on new daemon thread
  │
  └── 5. Return
        └── { started_at, auto_transitions, summary_so_far }
```

### _run_llm_review()

```
_run_llm_review(prompt)
  │
  ├── Build LLM (build_main_llm, temperature=0.3)
  ├── Assemble messages (system prompt + user prompt)
  ├── llm.invoke(messages)
  │
  └── Return { final, summary, model, provider, tool_calls, error }
```

LLM は `skill_manage` ツールを呼び出してスキルを作成/変更/削除できます。これらの tool_calls は記録され、分類調整に使用されます。

---

## 自動遷移ルール

`apply_automatic_transitions()` は各エージェント作成スキルを評価します:

```
For each agent-created skill:
  │
  ├── pinned? → skip
  ├── no persisted usage record? → seed_record_if_missing(), skip
  │
  ├── never used (use_count==0) and anchor > stale_cutoff?
  │     └── if currently stale → reactivate to active
  │
  ├── anchor <= archive_cutoff and not archived?
  │     └── _remove_skill() → delete from disk
  │
  ├── anchor <= stale_cutoff and currently active?
  │     └── mark as stale
  │
  └── anchor > stale_cutoff and currently stale?
        └── reactivate to active
```

ここで `anchor` = `last_activity_at`（一度もアクティブでなければ `created_at`、フォールバックとして `now`）。

時間基準:
- `stale_cutoff = now - stale_after_days`（デフォルト 30 日）
- `archive_cutoff = now - archive_after_days`（デフォルト 90 日）

---

## LLM 統合

LLM パスは `CURATOR_REVIEW_PROMPT` を受け取り、狭いスキルをクラスレベルのアンブレラスキルへマージするよう指示します:

**統合戦略**:
- **a. 既存のアンブレラにマージ** — ラベル付きセクションを追加し、兄弟スキルをアーカイブ
- **b. 新しいアンブレラを作成** — クラスレベルのスキルを作成し、兄弟スキルをアーカイブ
- **c. 参照に格下げ** — 狭いコンテンツをアンブレラのサポートディレクトリへ移動し、古いスキルをアーカイブ

**LLM 出力形式**（YAML 構造化サマリー）:
```yaml
consolidations:
  - from: old-skill-name
    into: umbrella-skill-name
    reason: why merged
prunings:
  - name: skill-name
    reason: why archived
```

**Dry-run モード**: LLM は実際にスキルライブラリを変更せず、「取ろうとするアクション」のみを出力します。`CURATOR_DRY_RUN_BANNER` プレフィックスがプロンプトに追加されます。

---

## アンブレラスキル生成

統合が新しいアンブレラスキルを生成するとき、`_apply_consolidation()` が完全なマージを調整します:

### _generate_umbrella_skill()

各新しいアンブレラスキルに対して、LLM を介して統合された SKILL.md を作成します:

```
_generate_umbrella_skill(umbrella, reasons, source_content, file_inventory)
  │
  ├── Build LLM (build_main_llm, temperature=0.3)
  ├── System prompt: skill librarian creating umbrella skill
  │     - Output ONLY SKILL.md content (YAML frontmatter + markdown body)
  │     - frontmatter: name, description, created_by: curator
  │     - Synthesize & deduplicate overlapping instructions
  │     - Organize with ## headings per concern area
  │     - Include "## When to use" section
  │     - Reference migrated support files with relative links
  │
  ├── User prompt: umbrella name + merge reasons + source skill content + file inventory
  │
  ├── On success → return generated SKILL.md content
  └── On failure → return fallback skeleton with concatenated source content
```

### ファイル移行

アンブレラスキルを作成した後、ソーススキルのサポートファイルを移行します:

```
For each consolidation entry (from → into umbrella):
  │
  ├── For each support subdirectory (references/, templates/, scripts/, assets/):
  │     └── Copy each file into umbrella's corresponding subdirectory
  │
  └── Delete source skill (delete_skill with absorbed_into=into)
```

### プルーニング

`prunings` ブロックにリストされていて統合の一部でないスキルは、単純に削除されます。

---

## 分類と調整

LLM パスが実行された後、一部のスキルが削除されている可能性があります。`classify.py` は、各削除されたスキルが**統合**（アンブレラにマージ）されたのか**プルーニング**（単純に削除）されたのかを決定します:

### 3 ソース調整

```
_reconcile_classification(removed, heuristic, model_block, destinations, absorbed_declarations)
  │
  ├── For each removed skill:
  │
  │   1. absorbed_into declaration (attached at LLM delete time)
  │      ├── target exists in destinations → consolidated
  │      └── declaration is empty → pruned
  │
  │   2. Model structured block (consolidations in YAML output)
  │      ├── target exists → consolidated
  │      └── target missing → fall back to heuristic or mark as pruned
  │
  │   3. Heuristic audit (old skill name referenced in tool_call content)
  │      ├── evidence found → consolidated
  │      └── no evidence → pruned
  │
  │   4. No evidence at all → mark as pruned (no-evidence fallback)
  │
  └── Output: { consolidated: [...], pruned: [...] }
```

**ヒューリスティック監査**（`_classify_removed_skills`）は LLM の `skill_manage` tool_calls を検査します:
- tool_call 引数（file_path、content、new_string など）を反復
- 削除されたスキル名（`-`/`_` バリアントを含む）への参照を検索
- `file_path` フィールドでパス認識マッチングのために `_needle_in_path_component()` を使用
- content フィールドには単語境界正規表現を使用
- 見つかった場合 → スキルがターゲットアンブレラに統合された証拠

---

## 使用記録システム

各エージェント作成スキルには、`skills/auto/.usage/` 配下に対応する JSON レコードファイルがあります:

```json
{
  "name": "my-skill",
  "state": "active",
  "pinned": false,
  "use_count": 3,
  "view_count": 5,
  "patch_count": 1,
  "activity_count": 9,
  "created_at": "2026-07-15T10:00:00+00:00",
  "last_activity_at": "2026-07-15T12:30:00+00:00"
}
```

| フィールド | 説明 |
|-------|-------------|
| `use_count` | スキルが呼び出された回数 |
| `view_count` | スキルが表示された回数 |
| `patch_count` | スキルが変更された回数 |
| `activity_count` | 上記すべての回数の合計 |
| `last_activity_at` | 最後のアクティビティのタイムスタンプ（使用されたことがなければ null） |
| `created_at` | レコードが作成されたタイムスタンプ |
| `_persisted` | 内部フラグ — `seed_record_if_missing()` がレコードを書き込んだ後に `True` |

`_default_record()` は `use_count=0`、`activity_count=0`、`last_activity_at=None` の新しいレコードを作成します。

---

## 孤立レコードのクリーンアップ

`agent_created_report()` は、対応するスキルディレクトリを持たない `.usage/` JSON ファイルを削除するために `_cleanup_orphan_records()` を自動的に呼び出します。これにより、使用ストアがディスク上の実際のスキルディレクトリと整合性を保ちます。

---

## ピンメカニズム

ピン留めされたスキルは最高レベルの保護を受けます:

- **二重判定**: 使用レコードの `pinned=True` **または** スキルディレクトリに `.pinned` マーカーファイルが存在
- **保護効果**: すべての自動遷移をバイパス（古い/削除が決してトリガーされない）; `_pinned_guard()` は任意の削除または状態変更をブロック
- **ガード動作**: `set_state()`、`delete_skill()`、`_remove_skill()` はすべて進行前に `_pinned_guard()` をチェック — ピン留めされている場合は警告とともに操作が拒否されます

現在の実装には公開の `pin_skill()` / `unpin_skill()` 関数はありません。ピン留めは外部で管理されます（使用レコードの `pinned` フィールドを設定するか、`.pinned` マーカーファイルを作成する）。

---

## レポートシステム

各実行は `logs/curator/{timestamp}/` 配下に詳細なレポートを生成します:

| ファイル | 内容 |
|------|---------|
| `run.json` | 完全な構造化データ（遷移数、分類結果、tool_calls、LLM 出力など） |
| `REPORT.md` | 人間が読める Markdown レポート |

**REPORT.md には以下が含まれる**:
- 実行メタデータ（モデル、プロバイダー、期間、スキル数の変化）
- 自動遷移統計
- LLM 統合統計（統合 / プルーニング）
- 具体的な統合とプルーニングのリスト（それぞれ最大 50 エントリ）
- 名前別のツール呼び出し数
- 自動サマリーテキスト
- LLM 最終サマリーテキスト
- リカバリーノート

**リカバリー**:
> **注記**: スキルは削除（アーカイブではない）されるため、リカバリーはバージョン管理またはバックアップによってのみ可能です。現在の実装には `restore_skill()` 関数はありません。

---

## 設定リファレンス

設定ファイルパス: `curator.yaml`（プロジェクトルート、`ROOT_DIR` の横）

| 設定 | デフォルト | 説明 |
|---------|---------|-------------|
| `enabled` | `true` | Curator が有効かどうか |
| `interval_hours` | `168`（7日） | 実行間隔 |
| `min_idle_hours` | `2` | 最小アイドル時間 |
| `stale_after_days` | `30` | 古いとマークするまでの日数 |
| `archive_after_days` | `90` | 削除までの日数 |
| `consolidate` | `false` | LLM 統合を有効にするかどうか |

設定は PyYAML を使用して `curator.yaml` を読み取る `_load_config()` を介してロードされます。各 getter 関数（`is_enabled`、`get_interval_hours` など）は、解析エラー時に定数デフォルトにフォールバックします。

---

## Curator 状態ファイル

パス: `skills/.curator_state`

```json
{
  "last_run_at": "2026-07-28T10:00:00+00:00",
  "last_run_duration_seconds": 12.34,
  "last_run_summary": "auto: 2 marked stale; llm: skipped",
  "last_run_summary_shown_at": null,
  "last_report_path": "/path/to/logs/curator/20260728-100000",
  "paused": false,
  "run_count": 5
}
```

| フィールド | 説明 |
|-------|-------------|
| `last_run_at` | 最後の実行の ISO タイムスタンプ |
| `last_run_duration_seconds` | 最後の実行の所要時間（秒） |
| `last_run_summary` | 最後の実行の人間が読めるサマリー |
| `last_run_summary_shown_at` | サマリーが最後に表示されたとき |
| `last_report_path` | 最後の実行のレポートディレクトリへのパス |
| `paused` | `True` の場合、Curator は実行されない |
| `run_count` | 完了した実行の合計数 |

状態は `load_state()`（`_default_state()` とマージし、`_` で始まる未知のキーを保持）を介してロードされ、`save_state()`（アトミック JSON 書き込み）を介して保存されます。

---

## 不変条件

Curator は、決して違反されてはならない以下の厳格な不変条件に従います:

1. **エージェント作成スキルのみを操作**（`skills/auto/`）、組み込み（`skills/builtin/`）は決して不可
2. **ピン留めされたスキルはすべての自動遷移をバイパス** — 古いとマークされたり削除されることはない
3. **`_pinned_guard()` は強制レイヤー** — すべての破壊的操作がそれをチェック

---

## ファイル構造

```
curator/
├── __init__.py           # Public API exports
├── constants.py          # Constants (paths, state names, defaults)
├── config.py             # Config loading (curator.yaml + env vars)
├── state.py              # Curator run state persistence (.curator_state)
├── usage.py              # Skill usage record CRUD (.usage/{name}.json) + agent_created_report + orphan cleanup
├── transitions.py        # Auto state transitions + should_run_now logic
├── orchestrator.py       # Main orchestrator (run_curator_review / maybe_run_curator / _apply_consolidation / _generate_umbrella_skill)
├── classify.py           # Removed skill classification (consolidated vs pruned) + reconciliation
├── helpers.py            # Utilities (ISO parsing, atomic writes, skill description reader, path needle matching)
└── report.py             # Run report generation (run.json + REPORT.md + _build_rename_summary)
```

**実行時ファイル**:
```
skills/
├── .curator_state              # Curator run state
└── auto/
    └── .usage/
        └── {skill-name}.json   # Skill usage record

logs/curator/
└── {timestamp}/
    ├── run.json                # Structured run data
    └── REPORT.md               # Human-readable report
```

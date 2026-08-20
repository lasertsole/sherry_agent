# Curator — 백그라운드 스킬 유지보수 오케스트레이터

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **Curator**는 EMA AI 에이전트의 백그라운드 스킬 유지보수 시스템으로, 에이전트가 생성한 스킬의 수명 주기 관리, 통합 및 정리를 담당합니다.

---

## 목차

- [개요](#개요)
- [핵심 책임](#핵심-책임)
- [아키텍처](#아키텍처)
- [트리거 메커니즘](#트리거-메커니즘)
- [수명 주기 상태 머신](#수명-주기-상태-머신)
- [실행 흐름](#실행-흐름)
- [자동 전환 규칙](#자동-전환-규칙)
- [LLM 통합](#llm-통합)
- [우산 스킬 생성](#우산-스킬-생성)
- [분류 및 조정](#분류-및-조정)
- [사용 기록 시스템](#사용-기록-시스템)
- [고아 레코드 정리](#고아-레코드-정리)
- [핀 메커니즘](#핀-메커니즘)
- [보고 시스템](#보고-시스템)
- [구성 참조](#구성-참조)
- [Curator 상태 파일](#curator-상태-파일)
- [불변 조건](#불변-조건)
- [파일 구조](#파일-구조)

---

## 개요

Curator는 **비활성 트리거** 기반 백그라운드 작업입니다. 에이전트가 유휴 상태이고 마지막 Curator 실행이 `interval_hours` 이전이라면, `maybe_run_curator()`가 백그라운드 검토를 시작합니다.

에이전트가 생성한 스킬(`skills/auto/` 아래)에만 작동하며, **내장 스킬**(`skills/builtin/`)은 절대 건드리지 않습니다. 오래되고 사용되지 않는 스킬은 **삭제**(디스크에서 제거)되며, 선택적으로 LLM 통합을 통해 겹치는 스킬을 우산 스킬로 병합한 후 정리할 수 있습니다.

---

## 핵심 책임

1. **자동 수명 주기 전환** — 스킬 활동 타임스탬프를 기준으로 `active → stale`로 전환; 보관 기준을 초과한 스킬 삭제
2. **통합**(선택적 LLM 패스) — 겹치는 좁은 스킬을 클래스 수준의 우산 스킬로 병합, 자동 콘텐츠 생성 및 파일 마이그레이션
3. **영구 상태** — `.curator_state` 파일에 실행 기록 저장

---

## 아키텍처

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

## 트리거 메커니즘

Curator는 예약된 cron 대신 **비활성 트리거** 패턴을 사용합니다:

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

| 매개변수 | 기본값 | 설명 |
|-----------|---------|-------------|
| `interval_hours` | 168 (7일) | Curator 실행 사이의 최소 간격 |
| `min_idle_hours` | 2 | 에이전트는 최소 N시간 동안 유휴 상태여야 함 |

`last_run_at`이 한 번도 설정되지 않은 경우, `should_run_now()`에 대한 첫 호출은 `True`를 반환하고 검토가 즉시 진행됩니다(지연된 첫 실행 시딩 없음).

---

## 수명 주기 상태 머신

```
    active ──────(stale_after_days no activity)──────► stale
      ▲                                                 │
      │             (new activity / reactivation)        │
      └─────────────────────────────────────────────────┘
      │                                                 │
      │         (archive_after_days no activity)         │
      └──────────────────► deleted ◄─────────────────────┘
```

| 상태 | 의미 |
|-------|---------|
| `active` | 스킬이 정상적으로 사용 가능한 상태 |
| `stale` | `stale_after_days` 동안 활동이 없어 오래된 것으로 표시 |

스킬이 `archive_after_days`의 무활동 기간을 초과하면 **삭제**됩니다(디렉터리와 사용 기록이 디스크에서 제거됨). 중간 `archived` 상태는 없으며 삭제는 되돌릴 수 없습니다.

**핵심 제약 조건**:
- 고정(pinned)된 스킬은 **절대** 자동 전환되거나 삭제되지 않습니다
- 오래된 기준 이후 생성된 `use_count == 0` 스킬은 현재 stale 상태라면 **재활성화**됩니다

---

## 실행 흐름

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

LLM은 `skill_manage` 도구를 호출하여 스킬을 생성/수정/삭제할 수 있습니다. 이러한 tool_calls는 기록되어 분류 조정에 사용됩니다.

---

## 자동 전환 규칙

`apply_automatic_transitions()`는 각 에이전트 생성 스킬을 평가합니다:

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

여기서 `anchor` = `last_activity_at`(활동이 없었으면 `created_at`, 또는 대체로 `now`).

시간 기준:
- `stale_cutoff = now - stale_after_days` (기본 30일)
- `archive_cutoff = now - archive_after_days` (기본 90일)

---

## LLM 통합

LLM 패스는 `CURATOR_REVIEW_PROMPT`를 받아 좁은 스킬을 클래스 수준의 우산 스킬로 병합하도록 지시합니다:

**통합 전략**:
- **a. 기존 우산에 병합** — 레이블이 있는 섹션 추가, 형제 스킬 보관
- **b. 새 우산 생성** — 클래스 수준 스킬 작성, 형제 스킬 보관
- **c. 참조로 격하** — 좁은 콘텐츠를 우산의 지원 디렉터리로 이동, 기존 스킬 보관

**LLM 출력 형식** (YAML 구조화 요약):
```yaml
consolidations:
  - from: old-skill-name
    into: umbrella-skill-name
    reason: why merged
prunings:
  - name: skill-name
    reason: why archived
```

**Dry-run 모드**: LLM은 실제로 스킬 라이브러리를 수정하지 않고 "취할 조치"만 출력합니다. `CURATOR_DRY_RUN_BANNER` 접두사가 프롬프트에 추가됩니다.

---

## 우산 스킬 생성

통합이 새 우산 스킬을 생성하면 `_apply_consolidation()`가 전체 병합을 조정합니다:

### _generate_umbrella_skill()

각 새 우산 스킬에 대해 통합된 우산 스킬을 생성합니다. 이 함수는 **주** `SKILL.md` 콘텐츠와 LLM이 분리한 선택적 **지원 파일**들의 매핑을 **모두** 반환하여, 주 문서를 간결하게 유지합니다:

```
_generate_umbrella_skill(umbrella, reasons, source_content, file_inventory)
  → (main_content: str, supporting_files: dict[path, content])
  │
  ├── Build LLM (build_main_llm, temperature=0.3)
  ├── System prompt: skill librarian creating umbrella skill
  │     - Output one or more file blocks, each starting with a '<<<PATH>>>' header
  │     - FIRST block MUST be '<<<SKILL.md>>>' (YAML frontmatter + markdown body)
  │     - Extra blocks permitted UNDER allowed subdirectories:
  │         references/, templates/, scripts/, assets/, examples/, resources/
  │     - Keep SKILL.md under ~15k characters (it is loaded on every skill use)
  │     - Offload bulky material into supporting blocks instead of inflating SKILL.md
  │       (long API docs → references/, runnable demos → examples/, helper logic → scripts/)
  │     - Reference each supporting file with a relative link + one-line description
  │     - frontmatter: name, description, created_by: curator
  │     - Synthesize & deduplicate overlapping instructions
  │
  ├── User prompt: umbrella name + merge reasons + source skill content + file inventory
  │
  ├── Parse response via _parse_multifile_umbrella():
  │     - Splits blocks on '<<<PATH>>>' headers deterministically
  │     - SKILL.md block → main content; other allowed-subdir blocks → supporting_files
  │     - Invalid paths / empty bodies are dropped with a warning
  │     - No valid headers → whole response treated as SKILL.md (historical fallback)
  │
  ├── On success → return (main_content, supporting_files)
  └── On failure/fallback → return (fallback skeleton SKILL.md, empty dict)
```

**파일 영속화**(`_apply_consolidation`):

**하드 길이 제한** (`skill_manage`와 공유):

LLM이 반환한 후, 우산 `SKILL.md` 메인 콘텐츠는
`skill_manage.split_oversized_skill(main_content, _UMBRELLA_SKILL_CHAR_TARGET, supporting_files)`
를 통과합니다. 이는 `skill_manage`를 통해 일반 스킬을 생성/편집할 때 사용되는 것과 동일한
결정적 보호 장치입니다. 메인 콘텐츠가 15,000자 목표를 초과하면 함수가 `## ` 제목을 따라 이를
`references/partNN.md` 참조 파일로 분할하고 각 섹션을 간결한 스텁 링크로 대체합니다 — 그 결과
`SKILL.md`는 항상 예산 내에 유지되고 콘텐츠 손실은 없습니다. `## ` 제목이 없는 본문이나 이미
예산 내인 콘텐츠는 그대로 반환됩니다.

1. `_create_skill(umbrella, main_content)`가 주 SKILL.md를 작성합니다.
2. `supporting_files`의 각 항목은 `_write_file(umbrella, path, content)`로 작성됩니다
   (경로는 허용된 우산 하위 디렉토리에 대해 검증됩니다).
3. 그 후 소스 하위 디렉토리 파일이 마이그레이션됩니다 (LLM이 이미 작성한 파일은
   합성 콘텐츠를 덮어쓰지 않도록 건너뜀).

### 파일 마이그레이션

우산 스킬을 생성한 후 소스 스킬의 지원 파일을 우산의 해당 하위 디렉토리로 마이그레이션합니다:

```
For each consolidation entry (from → into umbrella):
  │
  ├── For each support subdirectory (references/, templates/, scripts/, assets/,
  │     examples/, resources/):
  │     └── Copy each file into umbrella's corresponding subdirectory
  │         (skip any path already written from supporting_files)
  │
  └── Delete source skill (delete_skill with absorbed_into=into)
```

### 정리

`prunings` 블록에 나열된 스킬 중 이미 통합의 일부가 아닌 것은 단순히 삭제됩니다.

---

## 분류 및 조정

LLM 패스가 실행된 후 일부 스킬이 제거될 수 있습니다. `classify.py`는 각 제거된 스킬이 **통합**(우산으로 병합)되었는지 **정리**(단순 삭제)되었는지 결정합니다:

### 3-소스 조정

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

**휴리스틱 감사**(`_classify_removed_skills`)는 LLM의 `skill_manage` tool_calls를 검사합니다:
- tool_call 인수(file_path, content, new_string 등)를 반복
- 제거된 스킬 이름(`-`/`_` 변형 포함)에 대한 참조 검색
- `file_path` 필드에서 경로 인식 매칭을 위해 `_needle_in_path_component()` 사용
- content 필드에 대해 단어 경계 정규식 사용
- 발견되면 → 스킬이 대상 우산으로 통합되었음을 나타내는 증거

---

## 사용 기록 시스템

각 에이전트 생성 스킬은 `skills/auto/.usage/` 아래에 해당 JSON 기록 파일이 있습니다:

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

| 필드 | 설명 |
|-------|-------------|
| `use_count` | 스킬이 호출된 횟수 |
| `view_count` | 스킬이 조회된 횟수 |
| `patch_count` | 스킬이 수정된 횟수 |
| `activity_count` | 위 모든 카운트의 합계 |
| `last_activity_at` | 마지막 활동 타임스탬프(사용된 적 없으면 null) |
| `created_at` | 레코드가 생성된 시점의 타임스탬프 |
| `_persisted` | 내부 플래그 — `seed_record_if_missing()`가 레코드를 기록한 후 `True` |

`_default_record()`는 `use_count=0`, `activity_count=0`, `last_activity_at=None`인 새 레코드를 생성합니다.

---

## 고아 레코드 정리

`agent_created_report()`는 대응하는 스킬 디렉터리가 없는 `.usage/` JSON 파일을 제거하기 위해 자동으로 `_cleanup_orphan_records()`를 호출합니다. 이렇게 하면 사용 저장소가 디스크의 실제 스킬 디렉터리와 일관성을 유지합니다.

---

## 핀 메커니즘

고정된 스킬은 가장 높은 수준의 보호를 받습니다:

- **이중 판정**: 사용 기록의 `pinned=True` **또는** 스킬 디렉터리에 `.pinned` 마커 파일 존재
- **보호 효과**: 모든 자동 전환(오래됨/삭제가 절대 트리거되지 않음)을 우회; `_pinned_guard()`는 모든 삭제 또는 상태 변경을 차단
- **가드 동작**: `set_state()`, `delete_skill()`, `_remove_skill()` 모두 진행 전에 `_pinned_guard()`를 확인 — 고정된 경우 경고와 함께 작업이 거부됨

현재 구현에는 공개 `pin_skill()` / `unpin_skill()` 함수가 없습니다. 고정은 외부에서 관리됩니다(사용 기록의 `pinned` 필드 설정 또는 `.pinned` 마커 파일 생성).

---

## 보고 시스템

각 실행은 `logs/curator/{timestamp}/` 아래에 상세 보고서를 생성합니다:

| 파일 | 내용 |
|------|---------|
| `run.json` | 전체 구조화 데이터(전환 수, 분류 결과, tool_calls, LLM 출력 등) |
| `REPORT.md` | 사람이 읽을 수 있는 Markdown 보고서 |

**REPORT.md에는 다음이 포함**:
- 실행 메타데이터(모델, 공급자, 기간, 스킬 수 변경)
- 자동 전환 통계
- LLM 통합 통계(통합됨 / 정리됨)
- 특정 통합 및 정리 목록(각각 최대 50개 항목)
- 이름별 도구 호출 수
- 자동 요약 텍스트
- LLM 최종 요약 텍스트
- 복구 참고 사항

**복구**:
> **참고**: 스킬이 삭제(아카이브 아님)되므로 복구는 버전 관리 또는 백업을 통해서만 가능합니다. 현재 구현에는 `restore_skill()` 함수가 없습니다.

---

## 구성 참조

구성 파일 경로: `curator.yaml` (프로젝트 루트, `ROOT_DIR` 옆)

| 설정 | 기본값 | 설명 |
|---------|---------|-------------|
| `enabled` | `true` | Curator 활성화 여부 |
| `interval_hours` | `168` (7일) | 실행 간격 |
| `min_idle_hours` | `2` | 최소 유휴 시간 |
| `stale_after_days` | `30` | 오래된 것으로 표시하기 전 일수 |
| `archive_after_days` | `90` | 삭제 전 일수 |
| `consolidate` | `false` | LLM 통합 활성화 여부 |

구성은 PyYAML을 사용하여 `curator.yaml`을 읽는 `_load_config()`를 통해 로드됩니다. 각 getter 함수(`is_enabled`, `get_interval_hours` 등)는 파싱 오류 시 상수 기본값으로 대체됩니다.

---

## Curator 상태 파일

경로: `skills/.curator_state`

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

| 필드 | 설명 |
|-------|-------------|
| `last_run_at` | 마지막 실행의 ISO 타임스탬프 |
| `last_run_duration_seconds` | 마지막 실행 기간(초) |
| `last_run_summary` | 마지막 실행의 사람이 읽을 수 있는 요약 |
| `last_run_summary_shown_at` | 요약이 마지막으로 표시된 시점 |
| `last_report_path` | 마지막 실행의 보고 디렉터리 경로 |
| `paused` | `True`이면 Curator가 실행되지 않음 |
| `run_count` | 완료된 총 실행 횟수 |

상태는 `load_state()`(`_default_state()`와 병합, `_`로 시작하는 알 수 없는 키 보존)를 통해 로드되고 `save_state()`(원자적 JSON 쓰기)를 통해 저장됩니다.

---

## 불변 조건

Curator는 절대 위반해서는 안 되는 다음의 엄격한 불변 조건을 준수합니다:

1. **에이전트 생성 스킬만 처리**(`skills/auto/`), 내장(`skills/builtin/`)은 절대 아님
2. **고정된 스킬은 모든 자동 전환을 우회** — 오래된 것으로 표시되거나 삭제되지 않음
3. **`_pinned_guard()`는 집행 계층** — 모든 파괴적인 작업이 이를 확인

---

## 파일 구조

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

**런타임 파일**:
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

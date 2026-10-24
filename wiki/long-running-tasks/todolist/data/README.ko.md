# 📦 TodoList 데이터 레이어 — 계획, Boulder, 원장, todos.db

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> [TodoList](../README.ko.md)의 일부: 계획 레이어를 뒷받침하는 세션 스코프 데이터 저장소 — 계획 파일, Boulder 상태, 증거 원장, todos.db 스키마.

---

## 데이터 레이어

### 계획 파일 (workspace/sessions/<session_id>/plans/*.md)

계획 파일은 세션 스코프입니다: **세션을 삭제하면 해당 세션의 plans도 삭제됩니다** (`workspace/sessions/<session_id>/` 트리 전체 삭제). 계획 참조는 `config.path.resolve_plan_path`로 세션 트리 또는 명시적 리포지토리 상대 경로로 해석되며, 외부 오케스트레이션 디렉터리는 관여하지 않습니다.

체크박스 형식의 Markdown으로, 완전한 HTN 분해를 정의합니다:

```markdown
# <Plan Name>

## Goal

<상세 목표: 계획명, 경로, 종단 상태, 딜리버리 모드, 검증 방법>

## Context

<프로젝트 배경, 제약, 알려진 정보>

## TODOs

### Wave 0: <Wave description>

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Recommended task executor category: quick
  - Verification: <exact command + assertion>
  - Files in scope: <path1, path2>

### Wave 1: <Wave description> (depends on Wave 0)

- [ ] [WHERE] [HOW] to [WHY] - expect [RESULT]
  - Depends on: Wave 0
  - Verification: <exact command + assertion>

## Final Verification Wave

- [ ] Run full test suite + typecheck + lint
- [ ] Manual QA: <exact command, exact observable, exact assertion>
- [ ] Cleanup receipts: <list of resources to tear down>
```

### Boulder 상태 (src/data/boulder.json)

영구 작업 상태. `session_id`에 `sherry:` 접두사 사용:

```json
{
  "schema_version": 2,
  "active_work_id": "<work-id>",
  "works": {
    "<work-id>": {
      "work_id": "<work-id>",
      "active_plan": "workspace/sessions/<session_id>/plans/<plan-name>.md",
      "session_ids": ["sherry:<session_id>"],
      "status": "active"
    }
  }
}
```

### 증거 원장 (src/data/evidence-ledger.db)

SQLite 테이블 `evidence_events`(WAL): 공유 검증 감사 트레일입니다. 행은 INSERT만 되고 UPDATE되지 않으며 보존 기간이 오래된 행을 삭제합니다. 쓰는 쪽은 `agent/tools/pub_base/evidence_recorder.py`, 읽는 쪽은 `agent/tools/taskflow/evidence_collector.py`이며, 프로듀서·행 스키마·파생 스테일 규칙·요약 형식·보존 기간·이를 소비하는 게이트는 [증거 원장](../../gates/evidence-ledger/README.ko.md)에 정리되어 있습니다.

### todos.db — 세션 수준 TODO 저장

```sql
CREATE TABLE IF NOT EXISTS todos (
    session_id   TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    priority     TEXT    NOT NULL DEFAULT 'medium',
    position     INTEGER NOT NULL,
    category     TEXT    NOT NULL DEFAULT 'quick',
    delegation   TEXT    NOT NULL DEFAULT 'self',
    subagent_id  TEXT    DEFAULT NULL,
    plan_ref     TEXT    DEFAULT NULL,
    flow_id      TEXT    DEFAULT NULL,
    step_id      TEXT    DEFAULT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, position)
);
```

| 필드       | 설명                                                              |
| ---------- | ----------------------------------------------------------------- |
| `plan_ref` | 연결된 계획 파일 경로 — 세션 스코프 `workspace/sessions/<session_id>/plans/*.md` |
| `flow_id`  | 연결된 TaskFlow flow id (DAG는 TaskFlow가 소유)                    |
| `step_id`  | 연결된 TaskFlow step id (예: `step-2`), DAG 상태 재읽기용          |

> **설계 경계**: `todos.db`는 `depends_on` / 웨이브 / step 상태를 보유하지 않습니다. 의존성 그래프, `blocked/ready/dispatched/done`과 언록 로직은 모두 TaskFlow가 제공합니다. todo는 `flow_id`/`step_id`로 해당 flow step을 가리킬 뿐이며, DAG 상태는 `taskflow_inspect(action="summary", flow_id)`로 재읽기합니다.

CRUD 인터페이스:

```python
async def replace_all(session_id: str, todos: list[dict]) -> None:
    """전량 교체: DELETE + INSERT (트랜잭션)"""

async def get_todos(session_id: str) -> list[dict]:
    """position순 읽기"""

def get_todos_sync(session_id: str) -> list[dict]:
    """시스템 프롬프트 주입용 동기 경로 (이벤트 루프 없음)"""

async def get_todos_by_flow(session_id: str, flow_id: str) -> list[dict]:
    """TaskFlow flow에 연결된 todo 읽기 (DAG 상태를 UI로 매핑)"""
```

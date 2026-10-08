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

### 증거 원장 (src/data/evidence-ledger.jsonl)

한 줄에 하나의 JSON 객체로, 각 체크박스의 실행 증거를 기록합니다.

```json
{"event": "task-started", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "tier": "LIGHT", "timestamp": "..."}
{"event": "task-completed", "plan": "xxx", "task": "Wave 0 Checkbox 0", "session_id": "sherry:xxx", "commands": ["pytest -xvs"], "artifact": "src/data/evidence/xxx.txt", "adversarial_classes": {"stale_state": "not-applicable", "dirty_worktree": "probed: git status clean"}, "cleanup": ["killed tmux session"], "timestamp": "..."}
{"event": "stale", "file_path": "agent/tools/taskflow/step_judge.py", "session_id": "sherry:xxx", "timestamp": "..."}
```

- **자동 기록**: `agent/tools/todolist/evidence_recorder.py`가 `terminal` / `python_repl` 결과에서 인식한 검증 명령의 행(`kind` + `status`; 분류표는 `EVIDENCE_LEDGER["verify_commands"]`)을 항상 덧붙이고, `write_file` / `patch_file` 편집 후 `stale` 이벤트를 덧붙입니다. 모든 진입점은 페일오픈 — 원장 쓰기가 도구를 깨뜨리지 않습니다.
- **staleness는 파생값**: 이후의 `{"event": "stale"}` 행이 어떤 evidence 행의 `command`에 포함된 경로를 지명할 때만 그 행이 stale입니다(`agent/tools/taskflow/evidence_collector.py`). 과거 행은 결코 재작성되지 않습니다.
- **세션 뷰**: `EvidenceLedger.for_session(session_key)` / `read_for_session()`이 공유 파일을 `session_id`로 필터링합니다; 파일 자체는 저장소 전역으로 공유됩니다.

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

> **설계 경계**: `todos.db`는 `depends_on` / 웨이브 / step 상태를 보유하지 않습니다. 의존성 그래프, `blocked/ready/dispatched/done`과 언록 로직은 모두 TaskFlow가 제공합니다. todo는 `flow_id`/`step_id`로 해당 flow step을 가리킬 뿐이며, DAG 상태는 `taskflow_summary(flow_id)`로 재읽기합니다.

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

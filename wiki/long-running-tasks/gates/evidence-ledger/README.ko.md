# 📋 에비던스 원장 — 검증 감사 트레일

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> SQLite 감사 트레일로, 에이전트가 실행한 모든 검증 명령(테스트, 린트, 빌드)을 기록하고 읽기 시점에 각 결과가 여전히 유효한지를 도출합니다 — 이를 통해 검증 없이 "완료"를 주장하는 서브에이전트나 TaskFlow를 완료 게이트가 거부할 수 있습니다.

에비던스 원장은 모든 완료 경계에서 하나의 질문에 답합니다: **"에이전트가 자신의 작업을 검증했는가, 그 검증이 여전히 유효한가?"** 이를 위해 유효하지 않음 플래그를 저장하지 않습니다 — 이벤트 행은 불변이며, 신선도는 읽기 시점에 검증 행과 파일 편집 이벤트의 순서로부터 계산됩니다(보존 기간은 오래된 행을 삭제할 뿐 역사를 다시 쓰지 않습니다).

**소스 코드:** `agent/tools/pub_base/evidence_ledger.py`(스토리지), `agent/tools/pub_base/evidence_recorder.py`(프로듀서), `agent/tools/taskflow/evidence_collector.py`(리더/컨슈머), `config/features/agent_side/evidence_ledger.py`(분류).

---

## 작동 방식

```
terminal / python_repl 이 명령을 실행
  │
  ├─ 분류: "pytest" 매치 → kind=test, "ruff" → kind=lint 등
  │        (분류는 EVIDENCE_LEDGER["verify_commands"]에 정의)
  │        매치되지 않는 명령은 조용히 무시
  │
  ├─ record_verification_evidence()
  │    └─ SQLite 행 하나 삽입: {kind, command, exit_code, status, output_snippet, sandboxed, session_id, timestamp}
  │
  └─ 이후: write_file / patch_file 가 소스 파일을 편집
       └─ mark_evidence_stale(file_path, session_id)
            └─ SQLite 행 하나 삽입: {event:"stale", file_path, session_id, timestamp}
                 │
                 └─ 읽기 시점: collect_evidence_summary()
                      ├─ evidence 행이 [stale]이 되는 것은, 더 나중의 stale 이벤트의 file_path 가
                      │   해당 행의 command 의 부분문자열인 경우
                      ├─ 방향이 중요: evidence 이전의 stale = 신선(재검증됨)
                      └─ 반환: "  [pass] test: `pytest foo.py`\n  [FAIL] lint: `ruff .` [stale]"
```

---

## 스토리지

**파일:** `src/data/evidence-ledger.db`(`config.path.resolve_evidence_ledger_path`로 해결)

| 속성 | 값 |
|------|----|
| 형식 | SQLite 테이블 `evidence_events`; WAL 모드 + `busy_timeout=5000` |
| 범위 | 저장소 전체(모든 세션 공유) |
| 변경성 | 행은 INSERT만 되고 UPDATE되지 않음. 보존 기간이 오래된 행을 삭제함(age TTL / 세션별 상한 / 전체 상한) |
| 세션 격리 | 인덱스된 `session_id`에 `WHERE session_id = ?`; 행은 SESSION key(메인 세션 id 또는 자식의 `agent:{agent}:subagent:{uuid}`)를 가지며 plan / todo / flow id는 없음 |
| 다중 키 읽기 | `read_for_keys(keys)`는 인덱스된 `IN` 쿼리 한 번으로 가져오고 각 키의 삽입 순서를 유지함(스테일은 한 세션의 행 순서 안에서 파생) |
| 동시성 | WAL이 읽기/쓰기를 겹치게 하고 쓰기는 `busy_timeout`으로 재시도 |
| 레거시 JSONL | 첫 오픈 시 한 번 가져오고(줄 해시로 중복 제거) `.migrated`로 이름 변경 |
| 파일 없음 | `read_all()`은 `[]` 반환; 디렉터리는 첫 접근 시 생성 |

DB는 의도적으로 저장소 전체 범위이며 세션 범위가 아니다. 같은 계획을 검증하는 여러 세션이 공유하는 감사 추적이다.

### 보존 기간과 정리

`EVIDENCE_LEDGER`가 정책을 가진다: `pruning_enabled`(기본 true), `max_age_days`(30), `max_events_per_session`(100), `max_total_events`(10 000). 정리는 지연 실행으로, 각 `append`의 INSERT 뒤에 세 단계 DELETE를 수행하며 실패해도 방금 기록한 행은 잃지 않는다. 세션 삭제는 `purge_session`(`DELETE WHERE session_id = ?` 한 번)을 호출하고, boot 고아 sweep은 `purge_orphans(known_sessions)`를 호출하므로 정상 정리 경로를 우회한 세션의 evidence도 제거된다. `purge_on_session_delete` / `purge_on_boot_sweep`가 명시 삭제를 제어하며, `pruning_enabled=false`는 지연 보존 기간만 끈다.

---

## 프로듀서(무엇이 기록되는가)

### 1. Terminal & Python REPL — 자동 기록(항상 켜짐)

모든 `terminal`과 `python_repl` 호출은 **모든** 종료 경로(성공, 비영 종료, 타임아웃, 취소, 예외)에서 `record_verification_evidence()`를 거칩니다. 레코더는:

1. `classify_verification_command()`로 명령을 분류 — `EVIDENCE_LEDGER["verify_commands"]`에 대한 소문자 부분문자열 매치:
   ```python
   "verify_commands": {
       "test": ["pytest", "jest", "vitest", "cargo test", "go test", "npm test"],
       "lint": ["ruff", "eslint", "flake8", "pylint", "clippy"],
       "build": ["cargo build", "npm run build", "make", "cmake"],
       "typecheck": ["basedpyright", "mypy", "tsc", "pyright"],
       "format": ["ruff format", "prettier", "black"],
   }
   ```
   어떤 패턴에도 매치되지 않는 명령은 조용히 무시됩니다(행이 기록되지 않음).

2. 결과 문자열에서 `exit_code`를 도출("Exit code N" / 실패 접두사 정규식 매치).
3. `status` = exit_code가 0이면 `"passed"`, 그렇지 않으면 `"failed"`.
4. `output_snippet`(처음 500자)과 `sandboxed` 플래그를 포함한 행을追加.

**Fail-open:** 모든 레코더는 자신의 에러를 삼킵니다 — 원장 쓰기가 도구를 망가뜨리지 않습니다.

### 2. 파일 편집 — stale 이벤트

`write_file`과 `patch_file`은 `@invalidates_evidence(path_arg="file_path", session_arg="session_id")`로 데코레이트됩니다. 데코레이터는 `finally` 블록에서 실행되므로, **모든** 결과(성공, 에러 문자열, 예외 발생)가 `{"event": "stale", "file_path": "..."}` 행을追加합니다.

### 3. 서브에이전트 워크스페이스 머지 — stale 이벤트

자식의 격리 워크스페이스가 부모 세션으로 머지백될 때, `workspace_merge._mark_merged_paths_stale()`이 적용/생성/삭제된 모든 경로에 대해 **부모** 세션 id로 `mark_evidence_stale_for_paths()`를 호출합니다. 부모의 해당 파일에 대한 이전 검증이 이제 무효가 됩니다.

---

## 행 스키마

### 검증 에비던스 행

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

### Stale 이벤트 행

```json
{
  "event": "stale",
  "file_path": "src/foo.py",
  "session_id": "agent:main:session:abc-123",
  "timestamp": "2026-10-15T08:31:00.654321+00:00"
}
```

---

## 유효하지 않음(staleness) 도출

유효하지 않음은**읽기 측에서 도출**되며, 플래그로 저장되지 않습니다. 규칙:

> evidence 행이 stale인 것은, **더 나중의** stale 이벤트(추가 순서에서 인덱스가 더 큼)의 `file_path`가 해당 행의 `command` 문자열의**부분문자열**로 나타나는 경우.

```python
def _is_stale(index, row, stale_events):
    command = str(row.get("command") or "")
    return any(
        stale_index > index
        and str(event.get("file_path") or "") in command
        for stale_index, event in stale_events
    )
```

핵심 세부사항:

- **방향이 중요:** **더 나중의** stale 이벤트만이 **더 이전의** evidence 행을 무효화합니다. evidence 이전의 stale 이벤트는 파일이 편집된 후 재검증되었음을 의미합니다 — 검증은 신선합니다.
- **부분문자열 매치:** `file_path in command`. 의도적으로 느슨함 — `src/foo.py`의 stale 이벤트는 `pytest src/foo.py tests/`를 무효화합니다.
- **세션 범위:** `SessionEvidenceLedger.list_records()`가 먼저 `session_id`로 필터링하므로, 다른 세션의 stale 이벤트가 교차 오염되지 않습니다.

---

## 에비던스 요약 형식

`collect_evidence_summary(session_key=None, flow_id=None)`는 여러 줄의 문자열을 반환합니다(evidence 행이 없으면 `None`):

```
  [pass] test: `pytest tests/test_foo.py`
  [FAIL] lint: `ruff check .`
  [pass] build: `npm run build` [stale]
  [?] test: `pytest bar.py`
```

줄별 형식: `  [{icon}] {kind}: \`{command}\`{stale_tag}`

| 구성요소 | 소스 |
|---------|------|
| Icon | status가 passed이면 `"pass"`, failed이면 `"FAIL"`, 알 수 없으면 `"?"` |
| Kind | 검증 카테고리: test / lint / build / typecheck / format |
| Command | 실행 명령 문자열, 백틱으로 감싸짐 |
| Stale tag | `_is_stale`이 True이면 `" [stale]"`追加 |

이 문자열을 게이트와 판정자가 파싱합니다 — 리터럴 부분문자열 `"FAIL"`, `"[stale]"`, `"[pass]"`를 검색합니다.

---

## 컨슈머 — 3개의 게이팅 레벨

### 레벨 1: 스텝별(TaskFlow step judge)

`taskflow_resume`이 acceptance criteria를 가진 스텝을 디스패치할 때, `judge_step_result()`에 `evidence_summary=collect_evidence_summary(session_key=step.child_session_key)`를 전달합니다. step judge의 프롬프트에는 `## Verification Evidence` 섹션이 포함되어 요약(또는 `"(none recorded)"`)이 표시됩니다. judge는 이를 사용하여 PASS / RETRY / BLOCK을 결정합니다.

### 레벨 2: 서브에이전트별(completion judge + drain gate)

- **Completion judge 스킵:** goal-loop의 첫 턴에서, `_first_turn_self_evident(result_text, evidence_summary)`가 확인: result가 비어있지 않음 + 적어도 하나의 `[pass]` + `FAIL` 없음 + `[stale]` 없음 → **judge 호출을 스킵**, auxiliary LLM 호출 1회 절약.
- **Completion judge 프롬프트:** 스킵되지 않으면, `judge_completion()`이 프롬프트에서 에비던스 요약을 받습니다.
- **Drain gate:** `SubagentCompletionDrainMiddleware`(부모 측 `before_model`)가 서브에이전트 완료 메시지를 드레인할 때 에비던스를 확인. 에비던스 없음 또는 `FAIL` → `[GATE] Completion blocked: verification evidence missing or failing` 차단 메시지를 주입.

### 레벨 3: 플로별(taskflow_finish Gate C)

TaskFlow가 DONE으로 전환되기 전, Gate C는 그 플로의 **집계** 에비던스를 확인합니다——디스패치된 각 단계의 `child_session_key`와 플로의 `creator_session_key`의 합집합입니다(행은 세션 키로 저장되고 플로는 여러 세션에 걸치기 때문입니다. 단계를 한 번도 디스패치하지 않은 플로는 키가 없어 에비던스도 없고 따라서 통과합니다):

| 조건 | 결과 |
|------|------|
| 요약에 `"FAIL"` 포함 | 거부: "failing verification evidence" |
| 요약에 `"[stale]"` 포함 | 거부: "stale evidence (code changed since last verification), re-run verification commands before finishing" |
| 그 외(또는 에비던스 없음 / collector 에러) | 통과 |

게이트 순서: A(DAG 완전성) → B(미해결 스텝 없음) → C(에비던스) → E(골 judge).

---

## 설계 원칙

1. **어디서나 fail-open.** 모든 레코더는 자신의 에러를 삼킵니다. 모든 게이트는 collector 에러 시 "통과/위반 아님"을 반환합니다. 망가진 원장은 완료를 차단하거나 서브에이전트를 가두지 않습니다.
2. **불변 이벤트 행 + 읽기 시점 도출.** 기록된 행은 UPDATE되지 않습니다. 유효하지 않음은 읽기 시점에 행의 순서로부터 계산됩니다. 보존 기간은 오래된 행을 DELETE할 수 있지만(age/세션/전체 상한) 역사를 다시 쓰지 않습니다. reader는 남은 행 순서에서 신선도를 계산합니다.
3. **경로 참조, 콘텐츠가 아님.** 원장은 명령 문자열과 출력 스니펫(500자)을 기록하고, 파일 콘텐츠는 기록하지 않습니다. *무엇을 검증했는지*와 *통과했는지*를 기록하며, *파일에 무엇이 있었는지*는 기록하지 않습니다.
4. **항상 켜짐, 토글 없음.** 자동 기록 경로에는 on/off 스위치가 없습니다. 분류에 매치되는 모든 terminal/python_repl 호출이 행을 씁니다. 모든 파일 편집이 stale 이벤트를 씁니다. 모델은 우회할 수 없습니다.

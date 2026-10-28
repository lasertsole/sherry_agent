# 📋 에비던스 원장 — 검증 감사 트레일

[English](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> SQLite 감사 트레일로, 에이전트가 실행한 모든 검증 명령(테스트, 린트, 빌드)을 기록하고 읽기 시점에 각 결과가 여전히 유효한지를 도출합니다 — 이를 통해 검증 없이 "완료"를 주장하는 서브에이전트나 TaskFlow를 완료 게이트가 거부할 수 있습니다.

에비던스 원장은 모든 완료 경계에서 하나의 질문에 답합니다: **"에이전트가 자신의 작업을 검증했는가, 그 검증이 여전히 유효한가?"** 이를 위해 유효하지 않음 플래그를 저장하지 않습니다 — 이벤트 행은 불변이며, 신선도는 읽기 시점에 검증 행과 파일 편집 이벤트의 순서로부터 계산됩니다(보존 기간은 오래된 행을 삭제할 뿐 역사를 다시 쓰지 않습니다).

**소스 코드:** `agent/tools/pub_base/evidence_ledger.py`(스토리지 + 도출), `agent/tools/pub_base/evidence_recorder.py`(프로듀서), `agent/tools/pub_base/regression_gate.py`(finish 시 재검증), `agent/tools/taskflow/evidence_collector.py`(리더/컨슈머), `config/features/agent_side/evidence_ledger.py`(분류), `config/features/agent_side/evidence_cross_session.py`(크로스 세션 + 회귀 게이트 스위치).

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
  │    └─ SQLite 행 하나 삽입: {kind, command, exit_code, status, output_snippet, sandboxed, modified_files, session_id, timestamp}
  │
  └─ 이후: write_file / patch_file 가 소스 파일을 편집
       └─ mark_evidence_stale(file_path, session_id) + record_file_edit()
            └─ SQLite 행 하나 삽입: {event:"stale", file_path, session_id, timestamp}
                 │
                 └─ 읽기 시점: collect_evidence_summary()
                      ├─ evidence 행이 [stale]이 되는 것은, 더 나중의 stale 이벤트의 file_path 가
                      │   해당 행의 command 의 부분문자열인 경우
                      ├─ 방향이 중요: evidence 이전의 stale = 신선(재검증됨)
                      └─ 반환: "  [pass] test: `pytest foo.py`\n  [FAIL] lint: `ruff .` [stale]"
```

**Changed-path 스냅샷(B').** `write_file` / `patch_file`은 `EvidenceLedger.record_file_edit()`도 호출해 해당 세션에서 편집한 경로를 누적합니다(중복 제거, 상한 `EVIDENCE_LEDGER["changed_paths_limit"]` = 200. **검증마다 리셋하지 않으므로** 같은 편집 뒤의 lint와 test가 같은 스냅샷을 공유합니다). 기록 시 세션의 project root 기준으로 정규화합니다: `./a.py`, `a/../a.py`, root 내부 절대 경로는 모두 `a.py`가 되고, root 외부 절대 경로는 절대 경로로 남습니다. 검증 명령을 기록할 때 현재 목록이 evidence 행의 `modified_files`로 복사됩니다. 이후 그중 어떤 파일에 쓰더라도 명령이 그 파일을 지목하지 않았어도 evidence가 무효화됩니다.

**크로스 세션 전파.** 파일 쓰기는 먼저 writer 자신의 세션을 마크합니다. `EVIDENCE_CROSS_SESSION_STALENESS["enabled"]`가 true이면 `EvidenceLedger.mark_stale_for_path_all_sessions()`가 다른 세션의 evidence 행 중 `modified_files`에 편집 경로가 포함된 것, command가 실제 경로 토큰으로 지목한 것, 또는 레거시 행의 command가 포함한 것을 찾아, 영향을 받는 각 세션 아래에 `scope="cross_session"` stale 행을 하나씩 추가합니다. `source_session`은 writer를 기록하고, `max_sessions_scan`은 스캔 수를 제한하며, `exclude_self`는 이미 개별 마크된 writer를 건너뜁니다. 읽기 측 도출은 변경할 필요가 없습니다 — 브로드캐스트 행이 영향을 받는 세션 자신의 행 순서에 들어가기 때문입니다.

**Finish 시 재검증.** `taskflow_flow(action="finish")`의 Gate C는 먼저 `regression_gate.reverify_flow_evidence()`를 호출합니다: flow 각 세션의 passed 명령을 중복 제거해 한 번씩 재실행하며(allowlist 실행 파일, 셸 없음, `max_reverify_commands` 및 명령별 타임아웃), 작업 디렉터리는 flow의 프로젝트 루트입니다. 성공하면 stale/broken이었던 명령에 `unmark`를 추가하고, 실패하면 `broken`을 추가합니다. 이 경로는 변경된 소스 파일을 stale 행이 지목하지 않아도 A의 테스트 명령을 다시 실행하므로 크로스 파일 import 파괴를 잡을 수 있는 유일한 경로입니다. 단계는 `file_deps`를 선언할 수 있으며, 다른 단계의 쓰기가 그중 하나에 닿으면 `prioritized_sessions()`가 해당 단계의 명령을 `max_reverify_commands` 상한 앞으로 옮깁니다(`file_deps_mode="prioritize"`, 기본). 선택형 `file_deps_mode="filter"`는 선언이 적중한 세션만 재실행합니다 — 더 저렴하지만 선언되지 않은 단계의 파괴를 놓칠 수 있습니다.

**검증기 허용 목록은 설정 가능합니다.** 재실행되는 것은 첫 프로세스가 알려진 테스트/lint/빌드/타입 검사 도구인 명령뿐이며, 그 집합은 코드 기본값 + `sherry.jsonc`의 `regression_gate_allowed_executables`입니다(`yolo_deny_paths`와 같은 list-setting 패턴: 기본값은 소비자가 소유하고 사용자 항목은 호출 시 병합되므로, 편집은 재시작 없이 다음 재실행부터 적용됩니다). gradle / maven / dotnet / rspec / swift 사용자는 **실행 파일 이름**만 추가하면 되고 코드는 건드리지 않습니다. 셸 메타 문자 거부, 명령 상한, 명령별 타임아웃은 그대로이며 이 목록을 읽는 것은 이 재검증 게이트뿐입니다——서브에이전트가 **기록**할 수 있는 명령은 별개의 면입니다. 시스템 설정 → 앱 설정 패널이 `GET|PUT /sherry-config/list/regression_gate_allowed_executables`로 편집하며, 이 엔드포인트는 등록된 list 키만 허용하고 각 항목을 검증합니다(소문자, 셸 메타 문자 없음, 길이 상한). 내장 기본값은 여기서 삭제할 수 없습니다.

---

## 스토리지

**파일:** `src/data/evidence-ledger.db`(`config.path.resolve_evidence_ledger_path`로 해결)

| 속성 | 값 |
|------|----|
| 형식 | SQLite 테이블 `evidence_events`; WAL 모드 + `busy_timeout=5000` |
| 범위 | 저장소 전체(모든 세션 공유) |
| 변경성 | 행은 INSERT만 되고 UPDATE되지 않음. 보존 기간이 오래된 행을 삭제함(age TTL / 세션별 상한 / 전체 상한) |
| 세션 격리 | 인덱스된 `session_id`에 `WHERE session_id = ?`; 행은 SESSION key(메인 세션 id 또는 자식의 `agent:{agent}:subagent:{uuid}`)를 가짐 |
| flow 스탬프 | `evidence_events.flow_id`(`idx_evidence_flow` 인덱스). TaskFlow 스텝 자식은 spawn 시 스탬프하고, finish 호출 중에는 creator에 임시 스탬프한 뒤 복원한다. stale/broken/unmark 행은 대상 세션의 활성 flow를 상속한다. `NULL` = TaskFlow 범위 아님 |
| 파일 편집 기억 | 같은 DB의 `verification_state(session_id, changed_paths_json, last_edit_at)`; 세션별 상한, session/고아 정리 시 삭제 |
| 다중 키 읽기 | `read_for_keys(keys)`는 인덱스된 `IN` 쿼리 한 번으로 가져오고 각 키의 삽입 순서를 유지함. `read_for_flow(flow_id)`는 flow 인덱스로 한 플로의 행을 읽음(스테일은 여전히 한 세션의 행 순서 안에서 파생) |
| 동시성 | WAL이 읽기/쓰기를 겹치게 하고 쓰기는 `busy_timeout`으로 재시도 |
| 파일 없음 | `read_all()`은 `[]` 반환; 디렉터리는 첫 접근 시 생성 |

DB는 의도적으로 저장소 전체 범위이며 세션 범위가 아니다. 같은 계획을 검증하는 여러 세션이 공유하는 감사 추적이다.

### 보존 기간과 정리

`EVIDENCE_LEDGER`가 정책을 가진다: `pruning_enabled`(기본 true), `max_age_days`(30), `max_events_per_session`(100), `max_total_events`(10 000). 정리는 지연 실행으로, 각 `append`의 INSERT 뒤에 세 단계 DELETE를 수행하며 실패해도 방금 기록한 행은 잃지 않는다. 세션 삭제는 `purge_session`(evidence와 `verification_state`에 각각 `DELETE WHERE session_id = ?`)을 호출하고, boot 고아 sweep은 `purge_orphans(known_sessions)`를 호출하므로 정상 정리 경로를 우회한 세션의 evidence와 changed-path 상태도 제거된다. `changed_paths_limit`(200)이 세션별 파일 목록을 제한합니다. `purge_on_session_delete` / `purge_on_boot_sweep`가 명시 삭제를 제어하며, `pruning_enabled=false`는 지연 보존 기간만 끈다.

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
5. 세션 상태 레지스터에서 `StateKey.FLOW_ID`를 읽어 행의 `flow_id`에 스탬프합니다. 메인/비 TaskFlow 세션은 `NULL`로 남습니다.

**Fail-open:** 모든 레코더는 자신의 에러를 삼킵니다 — 원장 쓰기가 도구를 망가뜨리지 않습니다.

### 2. 파일 편집 — stale 이벤트

`write_file`과 `patch_file`은 `@invalidates_evidence(path_arg="file_path", session_arg="session_id")`로 데코레이트됩니다. 데코레이터는 `finally` 블록에서 실행되므로, **모든** 결과(성공, 에러 문자열, 예외 발생)가 `{"event": "stale", "file_path": "..."}` 행을 추가하고 `EvidenceLedger.record_file_edit()`를 호출합니다. 크로스 세션 스위치가 켜져 있으면 같은 호출이 evidence가 해당 경로를 포함하는(`modified_files` 적중 또는 레거시 command 부분문자열) 다른 각 세션에도 stale 행을 추가합니다.

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
  "modified_files": ["src/alpha.py"],
  "session_id": "agent:main:session:abc-123",
  "flow_id": "plan-4f7c2a91d0",
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

크로스 세션 행은 두 개의 선택 필드를 추가로 가집니다: `"scope": "cross_session"`과 `"source_session": "<writer session id>"`. 이 필드가 없는 행은 기존 per-session 마크입니다.

`broken`과 `unmark`도 같은 추가 전용 구조입니다: `broken`은 `command`, `exit_code`, `output_snippet`, `scope="regression_gate"`를, `unmark`는 `command`와 `scope`를 가집니다. 둘 다 이전 행을 다시 쓰지 않습니다.

---

## 유효하지 않음(staleness) 도출

유효하지 않음은**읽기 측에서 도출**되며, 플래그로 저장되지 않습니다. 규칙:

> evidence 행이 stale인 것은, **더 나중의** stale 이벤트(추가 순서에서 인덱스가 더 큼)가 지목한 `file_path`가 해당 행의 `modified_files` 집합에 포함되거나, command가 실제 경로 토큰으로 지목하는 경우. `modified_files`가 없는 레거시 행은 원래 command 부분문자열 규칙으로 폴백합니다.

```python
def derive_evidence_state(records, index):
    row = records[index]
    command = str(row.get("command") or "")
    modified = row.get("modified_files") or []
    broken = stale = False
    for later in records[index + 1:]:
        event = later.get("event")
        if event == "unmark" and later.get("command") == command:
            broken = stale = False
        elif event == "broken" and later.get("command") == command:
            broken = True
        elif event == "stale":
            path = str(later.get("file_path") or "")
            if modified:
                stale = stale or (path in modified) or _command_references_path(command, path)
            else:
                stale = stale or (path in command)
    return broken, stale
```

핵심 세부사항:

- **방향이 중요:** **더 나중의** stale 이벤트만이 **더 이전의** evidence 행을 무효화합니다. evidence 이전의 stale 이벤트는 파일이 편집된 후 재검증되었음을 의미합니다 — 검증은 신선합니다.
- **새 행은 정확한 집합:** `file_path in modified_files`, 또는 command의 실제 경로 토큰. 명령이 지목하지 않은 소스 파일(`pytest tests/test_beta.py` + `alpha.py` 쓰기)과 A가 쓰지 않은 테스트 파일을 잡고, 기존 부분문자열 오탐(`feature.py` vs `test_feature.py`)을 피합니다.
- **레거시 행 폴백:** `file_path in command`. B' 이전 행은 원래 동작을 유지합니다.
- **경로 정규화:** 누적 경로와 stale 이벤트 모두 writer 세션의 project root 기준으로 정규화합니다(`./`, `..`, root 내부 절대 → project-relative, root 외부 절대는 절대 유지). 문자열 정규화이며 심볼릭 링크를 해석하지 않고, 서로 다른 session root는 통합하지 않습니다.
- **세션 그룹:** `SessionEvidenceLedger.list_records()`는 `session_id`로 필터링하고, 도출은 한 세션의 행 순서 안에서만 비교합니다. 따라서 크로스 세션 무효화는 영향을 받는 세션 자신의 그룹에 **기록되며**(세션당 한 행), writer의 그룹에 남지 않습니다.
- **Broken과 unmark:** 같은 `command`의 더 나중 `broken` 이벤트는 `[broken]`(재검증 실패)으로 표시되고, 같은 `command`의 더 나중 `unmark`는 `[stale]`과 `[broken]`을 모두 지웁니다. `evidence_ledger.py`의 `derive_evidence_state()`가 유일한 구현이며, `[broken]`이 `[stale]`보다 우선합니다.

---

## 에비던스 요약 형식

`collect_evidence_summary(session_key=None, flow_id=None)`는 여러 줄의 문자열을 반환합니다(evidence 행이 없으면 `None`). `flow_id`를 넘기면 그 플로로 스탬프된 행만 읽습니다. `taskflow_finish`는 `flow_id`만 넘깁니다. flow 컨텍스트가 없는 호출자는 세션 키 집합을 넘길 수 있습니다:

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
| 상태 태그 | `derive_evidence_state()`가 broken이면 `" [broken]"`, 아니고 stale이면 `" [stale]"` |

이 문자열을 게이트와 판정자가 파싱합니다 — 리터럴 부분문자열 `"FAIL"`, `"[broken]"`, `"[stale]"`, `"[pass]"`를 검색합니다.

---

## 컨슈머 — 3개의 게이팅 레벨

### 레벨 1: 스텝별(TaskFlow step judge)

`taskflow_step(action="resume")`이 acceptance criteria를 가진 스텝을 디스패치할 때, `judge_step_result()`에 `evidence_summary=collect_evidence_summary(session_key=step.child_session_key)`를 전달합니다. step judge의 프롬프트에는 `## Verification Evidence` 섹션이 포함되어 요약(또는 `"(none recorded)"`)이 표시됩니다. judge는 이를 사용하여 PASS / RETRY / BLOCK을 결정합니다.

### 레벨 2: 서브에이전트별(completion judge + drain gate)

- **Completion judge 스킵:** goal-loop의 첫 턴에서, `_first_turn_self_evident(result_text, evidence_summary)`가 확인: result가 비어있지 않음 + 적어도 하나의 `[pass]` + `FAIL` 없음 + `[stale]` 없음 + `[broken]` 없음 → **judge 호출을 스킵**, auxiliary LLM 호출 1회 절약.
- **Completion judge 프롬프트:** 스킵되지 않으면, `judge_completion()`이 프롬프트에서 에비던스 요약을 받습니다.
- **Drain gate:** `SubagentCompletionDrainMiddleware`(부모 측 `before_model`)가 서브에이전트 완료 메시지를 드레인할 때 에비던스를 확인. 에비던스 없음 또는 `FAIL` → `[GATE] Completion blocked: verification evidence missing or failing` 차단 메시지를 주입.

### 레벨 3: 플로별(taskflow_flow(action="finish") Gate C)

TaskFlow가 DONE으로 전환되기 전, Gate C는 `flow_id`만으로 그 플로의 행을 읽습니다. 스텝 자식은 spawn 시 스탬프되고, finish 호출 중에는 creator가 스탬프되었다가 종료 시 복원되며, stale / broken / unmark 이벤트는 대상 세션의 flow 컨텍스트에서 자동 스탬프됩니다. 이 경로에는 세션 키 폴백이 없습니다. 스탬프 행이 없는 플로는 에비던스가 없으므로 통과합니다:

| 조건 | 결과 |
|------|------|
| 요약에 `"FAIL"` 포함 | 거부: "failing verification evidence" |
| 요약에 `"[broken]"` 포함 | 거부: "confirmed regression (a re-verified command now fails)" |
| 요약에 `"[stale]"` 포함 | 거부: "stale evidence (code changed since last verification), re-run verification commands before finishing" |
| 그 외(또는 에비던스 없음 / collector 에러) | 통과 |

게이트 순서: A(DAG 완전성) → B(미해결 스텝 없음) → C(에비던스) → E(골 judge).

---

## 설계 원칙

1. **어디서나 fail-open.** 모든 레코더는 자신의 에러를 삼킵니다. 모든 게이트는 collector 에러 시 "통과/위반 아님"을 반환합니다. 망가진 원장은 완료를 차단하거나 서브에이전트를 가두지 않습니다.
2. **불변 이벤트 행 + 읽기 시점 도출.** 기록된 행은 UPDATE되지 않습니다. 유효하지 않음은 읽기 시점에 행의 순서로부터 계산됩니다. 보존 기간은 오래된 행을 DELETE할 수 있지만(age/세션/전체 상한) 역사를 다시 쓰지 않습니다. reader는 남은 행 순서에서 신선도를 계산합니다.
3. **경로 참조, 콘텐츠가 아님.** 원장은 명령 문자열과 출력 스니펫(500자)을 기록하고, 파일 콘텐츠는 기록하지 않습니다. *무엇을 검증했는지*와 *통과했는지*를 기록하며, *파일에 무엇이 있었는지*는 기록하지 않습니다.
4. **자동 기록은 항상 켜져 있고, 두 파생 계층만 스위치 가능합니다.** 분류에 매치되는 모든 terminal/python_repl 호출이 행을 쓰고 모든 파일 편집이 writer 자신의 세션을 마크합니다(on/off 스위치 없음). 크로스 세션 브로드캐스트(`EVIDENCE_CROSS_SESSION_STALENESS["enabled"]`)와 finish 시 재검증(`EVIDENCE_REGRESSION_GATE["enabled"]`)이 게이트된 계층이며 기본은 둘 다 켜짐입니다. 끄면 원장 스키마를 바꾸지 않고 이전 동작으로 돌아갑니다.
5. **현재 스키마만 지원합니다.** `_SCHEMA`가 완전한 테이블·인덱스·`verification_state`를 한 번에 만듭니다. 증분 마이그레이션, 레거시 JSONL 가져오기, 스키마 버전 테이블은 없습니다. 오래된 데이터베이스는 제자리에서 수렴되지 않습니다.

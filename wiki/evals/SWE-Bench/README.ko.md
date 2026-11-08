# SWE-Bench(host harness)

SWE-Bench는 공식 Docker 평가가 아니라 저장소 자체의 host harness로 실행합니다.
harness는 각 인스턴스의 base commit을 고정하고 환경을 만든 뒤, 인스턴스 자체의
FAIL_TO_PASS / PASS_TO_PASS로 판정합니다. 답하는 질문은 "이 agent가 이 머신에서
이 두 HARD 인스턴스를 고쳤는가"이지 "리더보드 순위가 무엇인가"가 아닙니다.

## 측정 대상

- `server.service.messages.async_generate`를 통한 **메인 agent**. 실제 미들웨어
  체인과 선택된 도구 면을 사용합니다.
- 그것이 spawn한 **하위 agent**. 파일 쓰기와 harness가 diff를 읽기 전의 정산을
  포함합니다.
- **harness**: 인스턴스 초기화, 공식 테스트 패치, 정확한 테스트 판정. harness 오류는
  invalid로 기록하며 수정 실패로 보지 않습니다.

## Harness와 슬라이스

- 데이터셋: `princeton-nlp/SWE-bench_Lite`, test split.
- HARD 슬라이스: `django__django-17051` 및 `django__django-16820`. `sympy__sympy-20639`는 2026-11-08에 실행 슬라이스에서 제거했습니다. test patch가 NumberSymbol과 Symbol 분모의 오래된 의미론을 바꾸는데, 그 선택은 issue와 수정 전 테스트에서 유도할 수 없기 때문입니다. Sympy 과거 실행은 `evals/swebench/RESULTS.md`에 남아 있습니다.
- 인스턴스 캐시: `--instance-cache-dir /tmp/swebench-instance-cache`.
- Arm: 중립 프롬프트, `--persona coding`, delegation pressure
  (`SHERRY_EVAL_DELEGATION_PRESSURE=1`). delegate-only arm과 그 미들웨어는
  2026-11-02에 삭제되었으며, 오래된 결과 행은 기록으로만 남깁니다.
- 리포트는 `evals/results/swebench/<run_id>/eval_report.json`; 인스턴스 diff는 같은
  디렉터리에 있습니다.

- 2026-11-04부터 작업 prompt에는 인스턴스의 test patch와 FAIL_TO_PASS 이름이
  (읽기 전용으로) 포함됩니다. checkout은 실행 전에 test patch로 미리 패치되어 agent가 대상 테스트를 실행할 수 있습니다. prompt는 두 세트의 정확한 명령도 제공합니다(Django는 `python tests/runtests.py <labels>`, 그 외는 `python -m pytest <node ids>`). 채점 시 파일을 초기화한 뒤 다시 적용하며, 리포트는 `target_tests_exposed`와 `tests_preapplied`를 기록합니다.
- 결정론적 디코딩: 모든 eval 턴은 temperature 0으로 고정된 `built_agent`로 빌드됩니다.
  `SHERRY_EVAL_MAX_ITERATIONS`는 메인 모델 호출 상한을 선택적으로 올립니다
  (미설정/0이면 출하 기본 180). 리포트는 `model.temperature`와
  `budget.main_max_iterations`를 기록합니다.

## 실패 이력(2026-11-01)

| 실행 | Arm | 결과 | 무슨 일이 있었나 |
| :--- | :--- | :--- | :--- |
| 2026-11-01 000025 | 프롬프트 + no-edit 게이트 | 1/2 오염 | Django가 테스트 파일 추가; Sympy 726B patch가 P2P 파괴 |
| 2026-11-01 002305 | delegate-only | 2/2 깨끗 | 메인에 직접 도구 없음; 자식이 구현·검증 |
| 2026-11-01 143952 | 첫 소프트 게이트(10/20) | 1/2 | 게이트가 spawn을 강제한 뒤 메인이 patch를 반복해 P2P 파괴 |
| 2026-11-01 174750 | 소프트 게이트(8/16, 누적) | 2/2 깨끗 | 16회 읽기 후 메인의 읽기/쓰기 차단, 구현 위임 |
| 2026-11-03 010944 | 7/14 조언형(누적) | 0/2 | Django는 조건만 바꿔 고유 제약 오류가 남았고, Sympy는 diff 없이 90회 호출 예산을 소진 |
| 2026-11-03 194320 | 네트워크 브리지 + 시그니처 연속(Sympy만) | 0/1 | 비어 있지 않은 diff지만 `test_pretty_sqrt` P2P 회귀. 연속 경고는 WARN뿐이며 90회 예산 소진 |
| 2026-11-03 203547 | + 정확한 시그니처 BLOCK(Sympy만) | 0/1 | 약 20회에서 차단이 작동했지만 agent는 `probe1.py`…`probe19.py`(출력 동일·인자 상이)로 전환해 90회를 모두 소진 |
| 2026-11-03 205218 | + 결과 반복(Sympy만) | 0/1 | terminal/python_repl이 6회 동일 결과에서 차단됨. agent는 72/90에서 patch했고 이후 검증할 수 없었음 |
| 2026-11-03 210023 | + patch_file 성공 시 차단 해제(Sympy만) | 0/1 | 28회의 깨끗한 실행으로 가드 미작동. P2P 통과, F2P는 NumberSymbol vs Symbol의 숨은 의미론을 놓침(기존 `test_issue_6285`는 이전 동작을 단언) |
| 2026-11-04 003521 | + 대상 테스트를 prompt에 제시(HARD 2) | 0/2 | 테스트는 보이지만 실행할 수 없어 두 인스턴스 모두 90회를 소진 |
| 2026-11-04 004714 | + 대상 테스트를 checkout에 사전 적용(HARD 2) | 1/2 | Django 통과. Sympy는 `e.is_Symbol`(`1/x_17`에는 오류)을 사용해 repro 스크립트에서 루프 |
| 2026-11-04 005721 | 실패 후 지시 강화(Sympy만) | 0/1 | 같은 잘못된 조건. F2P는 0 |
| 2026-11-08 111407 | HARD Django만, prompt에 F2P+P2P | 1/1 | Django 통과. 첫 실행은 P2P `NoneType` 회귀로 0/1, 두 번째 통과 |
| 2026-11-08 133112 | temperature=0 + prompt에 F2P/P2P | 격리 실패 | temperature pin이 workspace 재정의 전에 `config.path`를 import해 live workspace에서 실행됨. 1/1은 집계하지 않음 |
| 2026-11-08 133743 | pin 순서 수정 후 temperature=0 | 1/1 | Django 통과. 17회 호출 / 60.6초. F2P/P2P 모두 통과, 임시 파일 없음 |
| 2026-11-08 135604 | temperature=0, 유효 반복 총 4회 | 4/4 | 모두 통과. 호출 수는 16～82회(60.6～286.3초). 가장 긴 실행은 90회 예산 중 82회 사용 |
| 2026-11-08 145011 | 새 HARD 인스턴스 `django__django-16820`, temperature=0 | 0/1 | gold check 통과(F2P 5 + P2P 91). 에이전트는 92회에서 90회 예산을 소진했고 F2P는 통과, P2P는 미통과 |
| 2026-11-08 184144 | 동일 인스턴스, `SHERRY_EVAL_MAX_ITERATIONS=180` | 1/1 | 24회 호출 / 96.5초로 통과. 상한 확대는 잘림 위험을 없앴지만 수정의 원인은 아님(같은 온도에서도 궤적이 크게 다름) |
| 2026-11-08 222819 | 최종 코드: 정확한 명령 + 기본 180 예산(16820) | 0/1 | F2P 5/5, 실제 P2P 단언 실패 5건. guardrail이 테스트 파일 추가 읽기를 막은 뒤 에이전트가 중단 |

## 드러난 문제

1. **옛 iteration 예산은 도구 쌍을 두 번 셌다.** 도구 호출이 `AIMessage`에 1,
   `ToolMessage`에 1을 소모해 90이 약 45회 도구 루프처럼 보였다. 이제 모델 호출만
   셉니다.
2. **`terminal`은 루프 감지의 사각지대.** `ToolGuardrails`는 멱등 도구만 해시합니다.
   `terminal`은 비멱등이라 동일 명령 32회도 no-progress 판정이 나지 않습니다.
3. **"정보가 충분하다"에 수렴점이 없었다.** 변경 지점을 찾은 뒤에도 검증을 계속하거나
   상류 PR을 찾으러 갔다. 편집과 검증으로 넘어가게 강제하는 장치가 없었다.
4. **평가 harness가 테스트 파일 편집을 거부하지 않았다.** 깨끗한 diff는 제품 계약의
   일부지만 `changed_paths`는 기록만 했다. 테스트 파일은 대상 테스트를 통과해도
   오염된 성공이 된다. 이 구멍은 닫혔다. `dirty_test_edits`가 harness 패치 밖에서
   에이전트가 수정한 테스트 파일을 표시하고 테스트 결과와 무관하게
   `resolved=False`로 만든다.
5. **쓰기는 검증이 아니다.** 첫 no-edit arm은 patch를 만들었지만 기존 테스트를
   실행하지 않았고, P2P 회귀는 판정 때 처음 드러났다.
6. **자식 결과와 token을 관찰하기 어렵다.** 타임아웃된 자식의 checkpoint가 사라질 수
   있고, 메인은 상태 확인을 위해 다시 spawn했으며, 리포트는 메인 session token만
   셌다.
8. **Django의 병렬 runner는 크래시 시 보고 중이던 실패를 잃을 수 있다.**
   예외를 pickle할 수 없으면 풀이 `TypeError: cannot pickle 'traceback'
   object`로 중단되고, 리포트에는 실패 테스트 이름이 남지 않는다. Django
   judge는 이제 `--parallel 1`로 실행하며, 같은 diff에서 91개 결과와 5개
   이름 있는 실패를 얻는다.

7. **측정용 pin이 샌드박스를 무력화할 수 있다.** `SHERRY_WORKSPACE_DIR` 재정의 전에
   eval 접합점을 import하면 `WORKSPACE_DIR`가 live persona 디렉터리에 고정된다.
   실행은 1/1로 보고했지만 `eval_workspace_has_sessions=false`와
   `live_sessions_unchanged=false`가 이를 드러냈다. 리포트의 isolation 블록이
   그런 실행을 능력 집계에서 제외하는 게이트다.

### 현재 해결

1. **수정됨.** `IterationBudget`은 모델 호출당 1단위; 도구 결과는 무료.
2. **수정됨.** `ToolGuardrails`에 상태 무관 시그니처 연속(도구 + 인자 해시, 3회 동일 호출, terminal은 자체 metadata의 5회)을 추가해 턴당 최대 3회 경고한 뒤 그 정확한 시그니처를 차단합니다. 비멱등 도구의 결과 반복(3회 경고, 6회 연속 동일 결과 시 도구 차단)도 있습니다. `patch_file` 성공 시(metadata `clears_guardrail_blocks`) 차단·연속·실패 카운터를 지워 검증을 재개할 수 있습니다. 읽기 전용 명령 분류기는 필요 없습니다.
3. **도구 가드에 의한 완화이며 하드 종료 게이트는 아님.** 차단된 시그니처는 다른 접근을 강제하고, 20261103_210023 Sympy 실행은 28회 모델 호출로 수렴해 인접 테스트 파일을 실행했으며 prompt도 이를 요구합니다. 독립적인 '종료 전 테스트 통과 필수' 게이트는 없습니다.
4. **미해결.** harness는 `changed_paths`를 기록하지만 `tests/` 편집을 invalid로 만들지 않음. 소프트 게이트는 확률을 낮출 뿐 공식 가드가 아님.
5. **부분 완화.** escalated 리마인더는 spawn 후 메인의 patch 루프를 멈추고 구현을 위임하도록 권하지만 "종료 전 테스트 통과 필수" 하드 게이트는 없음.
6. **부분 수정.** 턴당 token 예산은 메인 + 모든 깊이의 자손을 루트 턴에 귀속. eval 리포트는 여전히 메인 session만 세고, 자식 checkpoint/결과 읽기는 미해결.

## no-edit 게이트와 실패 이유

옛 eval 게이트는 15회 도구 호출 후 경고하고 iteration 예산의 70%에서 조사를
차단했으며, 어떤 쓰기든 리셋했습니다. 그래서 세 가지 실패가 예측 가능했습니다:

- 쓰기를 **도구 이름**(`patch_file` / `write_file`)으로 감지해 `terminal cp`나
  heredoc 편집이 보이지 않았다. Django는 바로 `cp`로 테스트 파일을 만들었다.
- **어떤** 쓰기든 성공으로 봤다. `tests/` 아래 쓰기나 곧 되돌린 patch도 포함한다.
- 첫 쓰기가 그 턴의 남은 시간 동안 게이트를 껐다. Sympy의 첫 잘못된 patch 이후
  약 30회 terminal 탐침과 P2P 회귀가 방치됐다.

삭제되었으며 수렴 장치가 될 수 없다.

## read-before-write 라이선스가 첫 소프트 게이트 루프를 막지 못한 이유

`agent/tools/pub_base/read_state.py`의 라이선스는 **`write_file` 덮어쓰기**를
보호합니다. 기존 파일은 그것을 읽은 session만 교체할 수 있고, 읽기 revision이 CAS
전제 조건이 됩니다. 이것은 **`patch_file`에는 적용되지 않습니다**. `patch_file`은
old_string/new_string, 느슨한 매칭, stale-write CAS로 교체하며 사전 읽기를 요구하지
않고, 두 도구 모두 patch가 맞는지 테스트가 통과하는지 모릅니다.

첫 소프트 게이트(10/20)는 제품 쓰기가 들어오면 차단을 풀었고 `patch_file`을 대상에
넣지 않았습니다. 메인은 researcher를 spawn했고 게이트가 리셋된 뒤 `compiler.py`에
약 70회 `patch_file`을 시도했습니다. 이는 **patch 재시도 병리**이지 라이선스 없는
`write_file` 덮어쓰기가 아니므로 라이선스는 발동하지 않았습니다. 이전 8/16 게이트는
`patch_file`도 차단했고 2026-11-01 실행은 그렇게 끝났습니다. 현재 7/12 조언형 설계는
차단을 의도적으로 제거했습니다.

## 현재 설계: DelegationPressureGate(7/12 조언형)

2026-11-01 실행은 이전 8/16 하드 차단 버전을 측정한 것이며, 아래의 7/12
조언형 동작은 2026-11-02에 충돌을 해소해 확정한 설계입니다. 2026-11-03의 0/2 재실행이 첫 측정이며 HARD 슬라이스를 해결하지 못했습니다 — `evals/swebench/RESULTS.md` 참조.

`agent/middlewares/delegation_pressure/core.py`:

- 한 턴 안의 **읽기/탐색** 호출을 누적합니다.
- **7**회에서 게이트 자체의 mid-turn `abefore_model` 경로로 `<system-reminder source="delegation_pressure">` 경고를 한 번 주입합니다.
- **12**회에서 더 강한 escalated 시스템 리마인더를 주입합니다. 도구는 계속 사용 가능하며 게이트는 조언만 하고 차단하지 않습니다.
- 브라우저 디버깅 도구는 집계에서 제외됩니다.
- accepted/pending spawn이 성공하면 카운트를 리셋해 메인
  에이전트가 검사/검증할 수 있으며, 이후 7/12에서 다시 작동합니다. 실패한 spawn과
  쓰기만으로는 리셋되지 않고, `after_agent`도 턴 경계에서 리셋합니다.

메인 에이전트에만 설치되며, 자식 미들웨어 빌더는 추가하지 않고 `_active`도
`:subagent:`/`:swarm:` 세션을 거부합니다.

## 네트워크 상태 브리지

실패가 네트워크 성격인 terminal 명령은 이제 도구 오류가 됩니다: `curl` / `wget` /
`http` / `https` / `ssh` / `scp` / `sftp` / `ping` / `dig` / `nslookup`의 비영
종료, 또는 `git clone|fetch|pull|push|ls-remote|remote|submodule`의 비영 종료는
`ToolException`을 발생시킵니다. `handle_tool_error`가 이를
`ToolMessage(status="error")`로 바꾸므로 exact-failure 카운터가 인식합니다. 로컬
명령(pytest, 컴파일러, 스크립트)의 비영 종료는 일반 텍스트(`status="success"`)로
반환되어 agent가 실패를 읽고 반복할 수 있습니다. `web_fetch`의 HTTP 4xx/5xx도
동일하게 raise합니다.

eval harness는 실행 전에 GitHub를 프로브하고, 도달할 수 없으면 시드된
`AGENTS.md`에 "네트워크를 계속 탐색하지 말라"는 directive를 추가합니다. 이것이
Sympy의 curl/GitHub 탐색 루프를 제거했습니다. 남은 Sympy F2P 실패는
NumberSymbol vs Symbol의 숨은 테스트 의미론이며 네트워크 탐색이 아닙니다.

## 결과

| Arm | Resolved | 시간 | 메인 session tokens | 추정 비용 |
| :--- | :--- | :--- | :--- | :--- |
| `DelegationPressureGate` 8/16(run 20261101_174750) | 2/2 | 1693.4s | 1,962,778 / 12,673 | $1.000 |
| `DelegationPressureGate` 7/14 조언형(run 20261103_010944) | 0/2 | 669.7s | 8,370,656 / 31,119 | $4.232 |
| Delegate-only(run 20261101_145717) | 2/2 | 3469.0s | 2,561,580 / 20,925 | $1.312 |

Django는 제품 코드와 릴리스 문서만 바꿨고, Sympy는 라이브러리 파일 하나만 바꿨습니다.
성공한 두 diff 모두 테스트 파일이 없습니다. token/cost는 메인 session만이며 자식
사용량은 집계되지 않습니다. 따라서 두 arm 비교는 시간과 메인 컨텍스트 신호이지
전체 청구서가 아닙니다.

## 재현

```bash
SHERRY_EVAL_DELEGATION_PRESSURE=1 uv run --no-sync python evals/swebench/suite.py \
  --hard --persona coding --keep-instances \
  --instance-cache-dir /tmp/swebench-instance-cache
```

## 주의

- 두 인스턴스는 신호이지 벤치 순위가 아니다. 일반적 개선을 주장하기 전에 같은
  슬라이스를 반복하라.
- host harness는 공식 Docker harness가 아니다. Python과 의존성 버전이 벤치마크
  이미지와 다르다.
- 깨끗한 결과가 테스트 편집 가드를 닫지는 않는다. harness는 여전히 `changed_paths`의
  `tests/` 아래 항목을 invalid로 표시해야 한다.
- 브라우저 도구는 설계상 제외했다. 이 실행은 브라우저 기반 조사에 대해 아무것도
  증명하지 않는다.

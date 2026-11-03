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
- HARD 슬라이스: `django__django-17051`, `sympy__sympy-20639`.
- 인스턴스 캐시: `--instance-cache-dir /tmp/swebench-instance-cache`.
- Arm: 중립 프롬프트, `--persona coding`, delegation pressure
  (`SHERRY_EVAL_DELEGATION_PRESSURE=1`). delegate-only arm과 그 미들웨어는
  2026-11-02에 삭제되었으며, 오래된 결과 행은 기록으로만 남깁니다.
- 리포트는 `evals/results/swebench/<run_id>/eval_report.json`; 인스턴스 diff는 같은
  디렉터리에 있습니다.

## 실패 이력(2026-11-01)

| 실행 | Arm | 결과 | 무슨 일이 있었나 |
| :--- | :--- | :--- | :--- |
| 2026-11-01 000025 | 프롬프트 + no-edit 게이트 | 1/2 오염 | Django가 테스트 파일 추가; Sympy 726B patch가 P2P 파괴 |
| 2026-11-01 002305 | delegate-only | 2/2 깨끗 | 메인에 직접 도구 없음; 자식이 구현·검증 |
| 2026-11-01 143952 | 첫 소프트 게이트(10/20) | 1/2 | 게이트가 spawn을 강제한 뒤 메인이 patch를 반복해 P2P 파괴 |
| 2026-11-01 174750 | 소프트 게이트(8/16, 누적) | 2/2 깨끗 | 16회 읽기 후 메인의 읽기/쓰기 차단, 구현 위임 |
| 2026-11-03 010944 | 7/14 조언형(누적) | 0/2 | Django는 조건만 바꿔 고유 제약 오류가 남았고, Sympy는 diff 없이 90회 호출 예산을 소진 |
| 2026-11-03 194320 | 네트워크 브리지 + 시그니처 연속(Sympy만) | 0/1 | 비어 있지 않은 diff지만 `test_pretty_sqrt` P2P 회귀. 연속 경고는 WARN뿐이며 90회 예산 소진 |

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
   오염된 성공이 된다. 이 구멍은 아직 열려 있으며, 이번 성공은 우연히 테스트를
   편집하지 않았을 뿐이다.
5. **쓰기는 검증이 아니다.** 첫 no-edit arm은 patch를 만들었지만 기존 테스트를
   실행하지 않았고, P2P 회귀는 판정 때 처음 드러났다.
6. **자식 결과와 token을 관찰하기 어렵다.** 타임아웃된 자식의 checkpoint가 사라질 수
   있고, 메인은 상태 확인을 위해 다시 spawn했으며, 리포트는 메인 session token만
   셌다.

### 현재 해결

1. **수정됨.** `IterationBudget`은 모델 호출당 1단위; 도구 결과는 무료.
2. **완화됨.** `DelegationPressureGate`는 7회 읽기에서 `<system-reminder>` 경고, 14회에서 escalated 버전을 주입하며 `terminal`/읽기 도구는 계속 사용 가능합니다. `ToolGuardrails`는 비멱등 terminal을 해시하지 않으며 읽기 전용 명령 분류기는 미구현.
3. **부분 완화.** escalated 리마인더는 14회에서 `sessions_spawn`을 권하지만 더 이상 도구를 차단하지 않습니다. 대상 테스트 실행을 요구하는 전용 검증 게이트는 미구현.
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
`patch_file`도 차단했고 2026-11-01 실행은 그렇게 끝났습니다. 현재 7/14 조언형 설계는
차단을 의도적으로 제거했습니다.

## 현재 설계: DelegationPressureGate(7/14 조언형)

2026-11-01 실행은 이전 8/16 하드 차단 버전을 측정한 것이며, 아래의 7/14
조언형 동작은 2026-11-02에 충돌을 해소해 확정한 설계입니다. 2026-11-03의 0/2 재실행이 첫 측정이며 HARD 슬라이스를 해결하지 못했습니다 — `evals/swebench/RESULTS.md` 참조.

`agent/middlewares/delegation_pressure/core.py`:

- 한 턴 안의 **읽기/탐색** 호출을 누적합니다.
- **7**회에서 게이트 자체의 mid-turn `abefore_model` 경로로 `<system-reminder source="delegation_pressure">` 경고를 한 번 주입합니다.
- **14**회에서 더 강한 escalated 시스템 리마인더를 주입합니다. 도구는 계속 사용 가능하며 게이트는 조언만 하고 차단하지 않습니다.
- 브라우저 디버깅 도구는 집계에서 제외됩니다.
- accepted/pending spawn이 성공하면 카운트를 리셋해 메인
  에이전트가 검사/검증할 수 있으며, 이후 7/14에서 다시 작동합니다. 실패한 spawn과
  쓰기만으로는 리셋되지 않고, `after_agent`도 턴 경계에서 리셋합니다.

메인 에이전트에만 설치되며, 자식 미들웨어 빌더는 추가하지 않고 `_active`도
`:subagent:`/`:swarm:` 세션을 거부합니다.

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

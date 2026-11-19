# 평가

이 트리는 에이전트를 **외부** 벤치마크로 어떻게 측정하는지, 그리고 각 결과가 무엇을
증명하고 무엇을 증명하지 않는지 기록합니다. 제품 문서와 의도적으로 분리했습니다.
벤치마크는 harness·모델·에이전트를 함께 측정하므로 점수는 능력의 백분율이 아닙니다.

## 스위트

| 스위트 | 측정 대상 | 진입점 |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | 숨겨진 테스트 패치와 benchmark 자체 grader를 쓰는 외부 코딩 과제. 기본 경로는 docker 또는 podman으로 benchmark의 작업 이미지를 실행하고, `--local`은 Docker 없는 호스트 모드 | `evals/deepswe/suite.py` |
| 코딩 능력 | 자체 설계한 코딩 과제와 내장 프롬프트 arm | `evals/coding_capability/` |
| 외부 벤치 | 브리지된 코딩 벤치마크와 harness 어댑터 | `evals/` |

## 측정 규칙

- 선택한 슬라이스에서 **N of M**을 보고하고, 백분율을 리더보드 순위로 제시하지 않습니다.
- **harness** 결과와 **모델** 결과를 구분합니다. provider 오류, 테스트 패치 미적용,
  타임아웃은 "수정 실패"가 아닙니다.
- **arm**(중립 프롬프트, 코딩 템플릿, delegation-pressure), 모델,
  git revision, 정확한 명령을 기록합니다.
- 깨끗한 diff도 판정의 일부입니다. `tests/` 아래 변경이나 PASS_TO_PASS 파괴는 대상
  테스트가 통과해도 성공이 아닙니다.
- 메인 session만 포함한 token/cost는 그렇다고 명시해야 합니다. 자식 session 사용량은
  현재 모든 리포트에서 집계되지 않습니다.

## 실행

```bash
# DeepSWE 컨테이너 경로 (benchmark 작업 이미지, docker 또는 podman):
uv run --no-sync python evals/deepswe/suite.py --preflight-only  # 이미지 pull + verifier 빌드 + GOLD 확인, 모델 호출 없음
SHERRY_EVAL_CONTAINER_BIN=podman uv run --no-sync python evals/deepswe/suite.py --timeout 1800

# DeepSWE 기본 슬라이스를 Docker 없는 호스트 모드로 실행:
uv run --no-sync python evals/deepswe/suite.py --local --timeout 1200
```

`SHERRY_EVAL_CONTAINER_BIN`이 엔진을 고정합니다(`docker` / `podman`, 기본은 docker 우선,
없으면 podman). `--preflight-only`는 모델 호출 없이 컨테이너 경로 전체(이미지 pull,
verifier 빌드, GOLD 자체 점검)를 검증합니다. 채점 실행에는 `MAIN_LLM_API_KEY`가 따로
필요합니다. podman에서는 작업/verifier 컨테이너를 `--http-proxy=false`로 만들고
(podman은 호스트 프록시를 모든 컨테이너에 주입하므로, 죽은 프록시가 있으면 benchmark
자체의 localhost 테스트가 실패합니다), verifier는 `--network none`으로 빌드합니다
(WSL2에는 nftables가 없어 빌드 시점의 bridge를 만들 수 없습니다).

각 실행은 `evals/results/deepswe/<run_id>/`에 `eval_report.json`과 작업별 diff를
씁니다. 디렉터리는 gitignore되며, 이 wiki에는 안정된 결론만 남깁니다.

## harness가 강제하는 것

DeepSWE는 실제 메인 에이전트를 구동하므로, harness는 규칙을 제시한 뒤 **검증**합니다.
프롬프트를 신뢰하지 않습니다.

- **오케스트레이션 게이트** — 파일 편집이나 자식 spawn 전에 `todowrite`와 TaskFlow flow
  하나(`taskflow_flow`의 `action="plan"`)가 필요합니다. 없으면 변경 계열 도구가 규칙을
  돌려줍니다. 사용량은 도구별로 집계되어 `orchestration_compliant`로 보고됩니다.
- **수용 체크리스트** — 작업 텍스트의 판정 가능한 조항(*must* / *raise* / *reject* /
  *only* / *charset* 등 규범 키워드)을 번호 체크리스트로 만들어 프롬프트에 넣고,
  게이트가 모든 `sessions_spawn(functional_role="reviewer")` 작업에도 덧붙입니다.
  그래서 검증은 계획에 동의하는 것이 아니라 조항별 PASS/FAIL이 됩니다.
- **계획 계속 실행** — 프로덕션의 자동 턴 트리거는 서버 hook이므로 harness가 직접
  구동합니다. 매 턴 후 샌드박스 계획 registry를 읽어 미완료 항목이 있는 동안 상한이 있는
  `[PLAN CONTINUATION]` 턴을 보냅니다(최대 3회). diff가 바뀌지 않는 계속 실행은 즉시
  멈춥니다(`no_progress_stop`).
- **리포트 필드** — `orchestration`(정책, 작업별 카운트, 게이트 카운터,
  `acceptance_checklist`, `reviewer_checklist_injections`), `continuations`,
  `no_progress_stop`, 그리고 verifier 판정과 patch.

알려진 공백: 컨테이너 경로는 각 턴을 `asyncio.wait_for`로 제한합니다. `--local`의 worker
프로세스와 달리 강제 종료가 없어, 진행 중인 provider 호출이 기한을 넘겨 남을 수 있습니다.

## 결과

- [DeepSWE](../../evals/deepswe/README.md) — 로컬 모드 결과와 실패 분석은
  `evals/deepswe/RESULTS.md`.

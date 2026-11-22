# 평가

이 트리는 에이전트를 **외부** 벤치마크로 어떻게 측정하는지, 그리고 각 결과가 무엇을
증명하고 무엇을 증명하지 않는지 기록합니다. 제품 문서와 의도적으로 분리했습니다.
벤치마크는 harness·모델·에이전트를 함께 측정하므로 점수는 능력의 백분율이 아닙니다.

## 스위트

| 스위트 | 측정 대상 | 진입점 |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | 숨겨진 테스트 패치와 benchmark 자체 grader를 쓰는 외부 코딩 과제. 기본 경로는 docker 또는 podman으로 benchmark의 작업 이미지를 실행합니다 | `evals/deepswe/suite.py` |
| [DevBench（SWE-bench Verified）](../../evals/devbench/README.md) | 공식 F2P/P2P 목록을 이미지 내 pytest로 판정. 에이전트는 호스트측 `/testbed` 복사본 편집 | `evals/devbench/suite.py` |
| [Multi-SWE-bench](../../evals/multi_swe_bench/README.md) | devbench 코어로 ByteDance 다국어 데이터 실행(`--lang` 필터, 이미지는 레코드에서) | `evals/multi_swe_bench/suite.py` |
| [TAU-bench](../../evals/tau_bench/README.md) | 모의 비즈니스 API를 sherry 도구로 랩. 기록된 호출+출력으로 일반 판정 | `evals/tau_bench/suite.py` |
| [GAIA](../../evals/gaia/README.md) | 범용 어시스턴트 과제, 호스트 모드, 전체 도구면. 공식 답 정규화 | `evals/gaia/suite.py` |
| [CyberSecEval 계열](../../evals/cybersec/README.md) | 보안 선다형/코드 분석, 호스트 모드, 선택지 문자 일치 | `evals/cybersec/suite.py` |
| [WebArena](../../evals/webarena/README.md) | docker-compose 스택 상주. browser 도구 유지. URL형 평가 네이티브 판정 | `evals/webarena/suite.py` |
| [VisualWebArena](../../evals/visualwebarena/README.md) | WebArena + 비전 델타(JS-evaluate 활성, vision-model 증거 보고) | `evals/visualwebarena/suite.py` |
| [OSWorld](../../evals/osworld/README.md) | Ubuntu 작업 컨테이너, TERMINAL-FIRST(계획에 따라 VNC 강등). vm_* 상태 평가 네이티브 판정 | `evals/osworld/suite.py` |

공유 기반은 `evals/shared/`: 데이터셋 해석, 배치+재개, 통합 보고서(계획의 token/문맥 적중률/code-intel 요약 포함), compose를 포함한 컨테이너 수명주기, 세 개의 트래커(`token_tracker`, `context_analyzer`, `code_intel_tracker`). 모든 스위트는 계약상 데이터 옵션：데이터셋·컨테이너 런타임·키 부재는 큰 소리로 SKIP(exit 3), 조용한 통과는 없습니다.
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

알려진 공백: 컨테이너 경로는 각 턴을 `asyncio.wait_for`로 제한합니다. 진행 중인 provider
호출에 대한 강제 종료가 없어 기한을 넘길 수 있습니다(삭제된 `--local` 모드의 worker 프로세스
강제 종료가 수정 참고).

## 작업 1–14 실측

디렉터리 순서의 처음 14개 작업을 2026-11-22 컨테이너 실행
(`20261122_044351` … `20261122_191618`)에서 측정했다. "최선"은 그 작업의 가장
좋은 실행이며, `F2P`는 benchmark의 fail-to-pass 집합, `P2P`는 초록을 유지해야
하는 pass-to-pass 회귀 집합, 편성(오케스트레이션)은 `todowrite`와 TaskFlow
flow를 모두 사용했음을 뜻한다.

| # | 작업 | 언어 | 최선 F2P | P2P | 실시간 | 편성 |
|---|---|---|---|---|---|---|
| 1 | abs-module-cache-flags | Go | **20/20 — resolved** | 3/3 | 3243 s | ok |
| 2 | abs-stepped-slices | Go | 5/6 | 6/6 | 1859 s | ok |
| 3 | actionlint-action-pinning-lint | Go | **55/55 — resolved** | 145/145 | 446 s | VIOLATED (flow 없음) |
| 4 | adaptix-name-mapping-aliases | Python | 38/44 | 2738/2738 | 1608 s | ok |
| 5 | aiomonitor-task-snapshots-diff | Python | 51/53 | 8/8 | 2241 s | ok |
| 6 | anko-default-function-arguments | Go | 0/2 | 119/119 | 1468 s | VIOLATED (flow 없음) |
| 7 | anko-typed-variable-bindings | Go | 7/9 | 94/94 | 787 s | ok |
| 8 | arcane-drift-detection-baselines | Go | **82/82 — resolved** | 2/2 | 2072 s | ok (todo 66) |
| 9 | arktype-json-schema-refs-dependencies | TypeScript | 23/25 | 1679/1679 | 2147 s | ok |
| 10 | awilix-async-container-initialization | TypeScript | 19/24 | 162/162 | 870 s | ok |
| 11 | bandit-incremental-cache-control | Python | 79/88 | 275/275 | 1989 s | ok |
| 12 | bandit-interprocedural-taint-checks | Python | 65/66 | 293/293 | 949 s | ok |
| 13 | bandit-structured-nosec-directives | Python | 65/69 | 281/282 | 1238 s | ok |
| 14 | boa-hierarchical-evaluation-cancellation | Rust | 0/17 | 7/7 | 2196 s | ok |

이 표가 말하는 것:

- **14개 중 3개가 resolved**(21%)이고, 다섯 개는 몇 항목 차이다
  (12: 65/66, 13: 65/69, 5: 51/53, 9: 23/25, 11: 79/88).
- **F2P는 이분법이 아니라 기울기**다. 부분 점수는 "기능의 대부분을 구현했다"와
  "아무것도 하지 않았다"를 구분한다. resolved 한 비트로는 둘이 섞인다.
- **예산이 개별 수정보다 중요하다**. 같은 모델로 작업 1은 1800초 턴 예산에서
  9/20, 7200초에서 20/20이었다. 시간이 끝났을 때도 진행 중이었다.
- **VIOLATED 두 행은 실제 기록**이다. 작업 3과 6은 `terminal`만으로 작업했고,
  당시에는 셸 도구가 편성 게이트 밖이었다.
- **회귀 신호 하나**. 작업 13은 P2P를 1개 잃었다(281/282). 숨은 테스트가 동작
  변화를 잡아냈다.
- 작업 10–13의 첫 시도는 실행되지 않았다. provider가 HTTP 402(잔액 부족)를
  반환해 각 작업이 약 20초, 도구 호출 0회로 실패했다. provider 장애이며 모델
  결과가 아니다.
- 작업 14의 첫 시도는 harness 실패(16.9초)였고 재실행은 정상이었다. 지금은 단발
  이미지 받기 실패를 재시도하며 전체 스윕을 중단하지 않는다.

## 결과

- [DeepSWE](../../evals/deepswe/README.md) — 로컬 모드 결과와 실패 분석은
  `evals/deepswe/RESULTS.md`.

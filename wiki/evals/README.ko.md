# 평가

이 트리는 에이전트를 **외부** 벤치마크로 어떻게 측정하는지, 그리고 각 결과가 무엇을
증명하고 무엇을 증명하지 않는지 기록합니다. 제품 문서와 의도적으로 분리했습니다.
벤치마크는 harness·모델·에이전트를 함께 측정하므로 점수는 능력의 백분율이 아닙니다.

## 스위트

| 스위트 | 측정 대상 | 진입점 |
| :--- | :--- | :--- |
| [DeepSWE](../../evals/deepswe/README.md) | 숨겨진 테스트 패치와 benchmark 자체 grader를 쓰는 외부 코딩 과제. `--local`은 Docker 없는 호스트 모드 | `evals/deepswe/suite.py` |
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
# DeepSWE 기본 슬라이스를 Docker 없는 호스트 모드로 실행:
uv run --no-sync python evals/deepswe/suite.py --local --timeout 1200
```

각 실행은 `evals/results/deepswe-local/<run_id>/`에 `eval_report.json`과
작업별 diff를 씁니다. 디렉터리는 gitignore되며, 이 wiki에는 안정된 결론만 남깁니다.
Docker 모드는 benchmark 이미지를 사용하므로 로컬 모드와 다른 경로입니다.

## 결과

- [DeepSWE](../../evals/deepswe/README.md) — 로컬 모드 결과와 실패 분석은
  `evals/deepswe/RESULTS.md`.

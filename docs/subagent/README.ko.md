# 🧩 서브에이전트 설계: 두 역할 축, 권한 가드, 계층화된 완료 게이트

[**English**](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 이 문서는 [서브에이전트 시스템 README](../../agent/tools/subagent/README.ko.md)의 설계 계층 자매편입니다. 후자는 런타임과 API 레퍼런스——spawn 파이프라인 단계, 레지스트리 상태 기계, announce 전달, 도구 schema, 전체 설정 표——입니다. 이 페이지는 역할 모델, spawn 권한 가드, 4계층 완료 게이트 스택 뒤의 설계 불변식을 서술하고 각 계층이 받아들이는 실패 모드를 명시합니다.

사실 출처: `agent/tools/subagent/**`(`types/`, `capabilities/`, `roles/`, `spawn/`), `agent/tools/taskflow/step_judge.py`와 `agent/tools/taskflow/tools/`, `agent/middlewares/subagent_completion_drain/`, `config/features/agent_side/`. 아래의 모든 진술은 해당 코드에 대해 검증되었습니다.

## 목차

- [개요](#-개요)
- [두 개의 역할 축](#-두-개의-역할-축)
- [기능 역할: 정의, 로딩, 작용](#-기능-역할-정의-로딩-작용)
  - [정의 파일](#-정의-파일)
  - [페일오픈 로더](#-페일오픈-로더)
  - [역할이 결정하는 것](#-역할이-결정하는-것)
- [spawn 권한 가드](#-spawn-권한-가드)
- [완료 판정기와 goal loop](#-완료-판정기와-goal-loop)
- [계층화된 완료 게이트](#-계층화된-완료-게이트)
- [다른 하위 시스템과의 관계](#-다른-하위-시스템과의-관계)
  - [합성 집계](#-합성-집계)
- [설정](#-설정)
- [테스트 맵](#-테스트-맵)
- [한계](#-한계)

## 🎯 개요

파견된 worker는 서로 다른 두 질문에 대한 두 개의 직교하는 답으로 설명됩니다:

1. **누가 spawn할 수 있고, 무엇을 제어하는가?**——**깊이 역할**(`SubagentSessionRole`). 중첩 깊이에서 도출되며 spawn 권한과 제어 범위를 단독으로 결정합니다.
2. **어떤 종류의 worker인가?**——**기능 역할**(`FunctionalRole`). 명시적 특수화로서 자식 LLM 티어, 도구 허용 목록, 역할별 프롬프트 절을 단독으로 결정합니다.

완료는 그다음 자식 실행에서 부모 턴까지 바깥으로 쌓인 네 개의 상시 켜진 계층으로 검증됩니다: 자식 실행의 완료 판정기와 goal loop, TaskFlow 단계 판정기, `taskflow_finish` 흐름 게이트, 그리고 부모 턴의 완료 drain 프로그램 게이트. 두 역할 축과 게이트 스택이 이 페이지의 주제입니다. API 세부는 [모듈 README](../../agent/tools/subagent/README.ko.md)에 있습니다.

### 🧱 불변식

1. **두 축은 결코 합쳐지지 않는다.** 기능 역할은 spawn 권한을 부여할 수 없고, 깊이 역할은 도구 허용 목록이나 프롬프트를 바꿀 수 없습니다. 모든 spawn에서 둘이 합성됩니다.
2. **`general`은 항등 역할이다.** 기능 역할 힌트가 없으면 spawn 동작은 그대로입니다: 어떤 정의도 로드되지 않고 LLM 티어는 깊이 역할이 공급합니다.
3. **spawn 권한은 두 번 깊이로 게이트된다**——조립 시점(도구 정책 교집합)과 호출 시점(도구 자체의 권한 검사).
4. **완료 게이트는 상시 켜져 있다.** 스택 어디에도 활성/비활성 스위치가 없으며 입력은 예산과 판정 기준뿐입니다.
5. **페일오픈은 모델 오류, 파싱 불가능한 출력, 이용 불가능한 조회에만 적용되며 부정적 판정에는 결코 적용되지 않는다.** 파싱된 `RETRY`, `BLOCK`, 실패한 증거 행은 여전히 차단합니다.
6. **자식 컨텍스트는 항상 격리된다.** 자식은 자신의 빈 메시지 목록에서 시작하고 부모 트랜스크립트를 상속하지 않으며, 이를 바꾸는 모드도 없습니다.
7. **완료 캐리어는 주장이지 검증된 결과가 아니다.** 부모 턴이 받는 것은 자식의 자기 보고입니다. 세션에 통과 증거가 없으면 drain 게이트가 필수 검증 메시지를 덧붙입니다.

## 🧭 두 개의 역할 축

| 축 | 해석 출처 | 결정하는 것 |
|----|-----------|-------------|
| **깊이 역할**(`SubagentSessionRole`) | 중첩 깊이 | spawn 권한, 제어 범위 |
| **기능 역할**(`FunctionalRole`) | 명시 힌트 → `agent_id` 일치 → 설정 기본값 | 자식 LLM, 도구 허용 목록, 프롬프트 절 |

**깊이 역할.** `resolve_subagent_capabilities(depth, max_depth)`는 깊이 `0`을 `MAIN`과 `ControlScope.CHILDREN`으로, `depth >= max_depth`를 `LEAF`와 `ControlScope.NONE`으로, 그 사이를 `ORCHESTRATOR`와 `CHILDREN`으로 매핑합니다. 권한 판정의 유일한 술어는 `can_spawn_children(role)`이며 `MAIN`과 `ORCHESTRATOR`에만 참입니다. 깊이 자체는 먼저 실행 레코드에서, 다음으로 세션 키의 `:subagent:` 등장 횟수에서 해석됩니다(`get_subagent_depth`). `max_spawn_depth` 기본값은 `2`, 하드 상한도 `2`입니다.

**기능 역할.** `_resolve_functional_role(hint, agent_id)`는 명시 힌트(알 수 없는 힌트는 경고를 남기고 다음으로 내려감), `agent_id` 이름, 설정된 `default_functional_role`, 마지막으로 `GENERAL` 순으로 시도합니다. `GENERAL`은 어떤 정의도 로드하기 전에 단락되며, 이것이 힌트 없는 spawn에서 깊이 기반 동작이 그대로 유지되는 이유입니다.

두 축은 양방향으로 직교합니다: 깊이 1의 `researcher`는 여전히 spawn할 수 있는 `ORCHESTRATOR`이고, 깊이 상한의 `general`은 여전히 spawn할 수 없는 `LEAF`입니다.

## 🧰 기능 역할: 정의, 로딩, 작용

### 📄 정의 파일

내장 정의는 패키지 안에 추적·배포 가능한 형태로 `agent/tools/subagent/roles/definitions/<name>/AGENTS.md`에 실려 있습니다. 선택적 사용자 재정의(미추적)는 `workspace/subagent_roles/<name>/AGENTS.md`에 둘 수 있습니다. 해석 순서는 **재정의 → 패키지 기본값 → 없음**이며 재정의 디렉터리 이름은 설정 가능합니다. 각 파일은 YAML frontmatter(`name`, `description`, `model_tier`, `tools`)와 자식 프롬프트에 덧붙는 markdown 본문을 가집니다. `tools: inherit`는 "모든 도구"로 해석되고, 알 수 없는 `model_tier`는 `inherit`로 폴백합니다.

| 역할 | 용도 | 유효 LLM 티어 | 역할 도구 집합 |
|------|------|----------------|----------------|
| `general` | 기본 worker; 항등 역할(패키지 정의는 결코 로드되지 않음) | 깊이 역할 | 모든 도구 + ast-grep `ast_grep_search`, `ast_grep_rewrite` |
| `researcher` | 읽기 전용 코드베이스·웹 조사 | `auxiliary` | `read_file`, `terminal`, `web_search` + 코드 인텔리전스 `explore`, `callers`, `callees`, `impact` + LSP `lsp_goto_definition`, `lsp_find_references`, `lsp_workspace_symbol`, `lsp_call_hierarchy`, `lsp_rename`, `lsp_diagnostics`, `lsp_format`, `lsp_status` + ast-grep `ast_grep_search`, `ast_grep_rewrite` |
| `executor` | 쓰기 가능한 구현과 명령 실행 | `auxiliary` | `read_file`, `write_file`, `patch_file`, `terminal`, `python_repl` + ast-grep `ast_grep_search`, `ast_grep_rewrite` |
| `reviewer` | 읽기 전용 diff·품질 감사 | `auxiliary` | `read_file`, `terminal` + ast-grep `ast_grep_search`, `ast_grep_rewrite` |

**ast-grep은 전 역할 공통, 코드 인텔리전스는 아니다.** 모든 기능 역할(`general` 포함)은 ast-grep 구조 검색/재작성 도구 `ast_grep_search`, `ast_grep_rewrite`도 함께 받습니다. tree-sitter 코드 인텔리전스 `explore` / `callers` / `callees` / `impact`는 심볼 인덱스가 필요하므로 계속 `researcher` 전용입니다. LSP 도구 `lsp_goto_definition` / `lsp_find_references` / `lsp_workspace_symbol` / `lsp_call_hierarchy` / `lsp_rename` / `lsp_diagnostics` / `lsp_format` / `lsp_status` 역시 같은 이유로 `researcher` 전용이며, 실행 중인 언어 서버가 필요하여 하위 에이전트가 필요할 때 지연 시작하고 유휴 시 자동 종료합니다. `lsp_rename`과 `lsp_format`은 기본적으로 미리보기만 하고, `lsp_diagnostics`는 비동기 진단 알림을 기다리며, `lsp_status`는 아무것도 시작하지 않고 가용성만 보고합니다.

### 🛡️ 페일오픈 로더

`load_role_definition(role)`은 후보 경로를 순회하며 처음 존재하는 파일을 파싱하고, 모든 실패에서 `None`을 반환합니다: 파일 없음, frontmatter 누락 또는 미종료, frontmatter가 매핑이 아님, `tools` 값 무효, 파일 읽기 불가. 파싱 실패마다 경고가 기록되며 호출자는 `None`을 `general`로 취급합니다. 로더가 페일오픈인 대상은 *모델, 파일, 파싱* 문제뿐이며——망가진 정의가 특권 있는 정의로 바뀌는 일은 결코 없습니다. `load_all_role_definitions()`는 결과를 프로세스 내에 캐시합니다. 워크스페이스 재정의를 편집한 뒤에는 `invalidate_role_cache()`를 호출하세요.

### ⚙️ 역할이 결정하는 것

**LLM 선택.** 명시적 `model_override`가 최우선이고, 그다음 `general`이 아닌 역할의 `model_tier`가 적용되며, 그마저 없으면 깊이 역할이 결정합니다(ORCHESTRATOR → 메인 LLM, LEAF → 보조 LLM).

```text
명시적 model_override
  └─► 기능 역할 model_tier("main" | "auxiliary")
        └─► 깊이 역할(ORCHESTRATOR → 메인 LLM, LEAF → 보조 LLM)
```

**시스템 프롬프트.** `general`이 아닌 역할(또는 비어 있지 않은 역할 설명/본문)은 `## Your Role`에 `{ROLE}` 특수화 줄을 더하고 정의 본문을 담은 `## Role Instructions` 절을 덧붙입니다.

**도구 정책.** 역할의 `tools` 목록은 허용 목록이 되고, 허용 목록 자체가 이미 제한하므로 기본 거부 목록은 비워집니다. spawn별 `extra_tools`는 그 허용 목록에 합류하며, 둘 다 무조건적인 `main_only` 메타데이터 게이트와 명시적 거부 목록의 적용을 계속 받습니다.

## 🔒 spawn 권한 가드

spawn 권한은 두 개의 독립 지점에서 집행되므로 단일 누출로 권한 상승이 불가능합니다:

- **조립 시점(주 게이트).** 최종 허용 목록이 역할 허용 목록과 `extra_tools`로 구축된 뒤, Phase 8.6이 이를 `can_spawn_children(role)`과 교집합합니다: spawn할 수 없는 모든 역할에서 `sessions_spawn`과 `sessions_yield`가 목록에서 제거됩니다. 상속 모드에서는 목록이 비어 있고 기본 거부 목록이 이미 이 쌍을 막으므로 교집합은 무연산입니다.
- **호출 시점(심층 방어).** `check_spawn_permission(session_id)`는 먼저 정규 호출자 키를 해석하고(자식·swarm 키는 그대로, 다른 id는 `agent:main:session:` 접두사), 다음으로 깊이를 해석하며(실행 레코드 우선, 세션 키 형태 차선), 마지막으로 깊이 역할을 해석해 spawn할 수 없는 호출자를 거부합니다. `(allowed, reason)` 튜플을 반환하고 결코 예외를 던지지 않으며, 도구는 기존 문자열 계약으로 거부를 렌더링합니다.

```text
조립: 최종 허용 목록 ∩ can_spawn_children(role)   → sessions_spawn / sessions_yield 제거
호출: check_spawn_permission(session_id)          → "status=forbidden", 예외 없음
```

호출 시점 가드는 `sessions_spawn`(권한 상승 경로)을 도구와 런타임 spawn 래퍼 양쪽에서 뒷받침합니다. `sessions_yield`는 조립 시점 제거만으로 보호됩니다.

## 🧪 완료 판정기와 goal loop

완료 판정기(`spawn/completion_judge.py`)는 서브에이전트 계층 게이트입니다. 온도 `0`의 보조 LLM이 작업, 자식의 최신 응답(`8000`자로 절단), 자식 세션의 검증 증거 요약을 받아 `DONE` 또는 `CONTINUE`를 한 문장 이유와 함께 반환합니다. `CONTINUE` 판정은 짧은 연속 프롬프트도 함께 전달합니다. 파싱은 단계적으로 안전하게 퇴화하며——먼저 JSON 복구, 다음으로 정규식 스캔——모델 오류나 사용할 수 없는 출력은 명시적 페일오픈 이유와 함께 `DONE`으로 확정됩니다.

goal loop는 예산이 한 턴을 초과하는 모든 spawn에서 실행되고, `COMPLETION_JUDGE["goal_max_turns"]`(기본 `5`)를 예산으로 삼아 첫 턴까지 포함합니다. spawn별 `goal_max_turns` 파라미터가 이를 재정의합니다. `CONTINUE`일 때 판정기의 프롬프트는 **같은 checkpoint 스레드**의 다음 `HumanMessage`로 주입되므로, 연속은 새 대화가 아니라 같은 자식 대화를 연장합니다.

```text
턴 1 ─► 판정기 DONE ────────────────────────────────────────► 확정
     └─► 판정기 CONTINUE(continuation_prompt) ─► 턴 2 ─► 판정기 …
            (DONE, 빈 연속 프롬프트, 또는 turns_used == goal_max_turns까지 반복)
```

예산을 다 써도 실행은 `OK`로 확정되고 `error="goal_loop_budget_exhausted"`가 기록됩니다——이 루프는 예산 소진을 실패로 바꾸지 않습니다. 연속 프롬프트가 비어 있어도 루프는 끝납니다. 예산 `1`이면 자식은 단일 턴으로 남고, 판정기는 아예 호출되지 않습니다.

## 🚦 계층화된 완료 게이트

네 게이트가 자식 실행에서 바깥으로 쌓입니다. 넷 모두 상시 켜져 있고, 입력은 예산과 판정 기준뿐입니다.

| # | 계층 | 게이트 | 필요한 입력 | 페일오픈 동작 |
|---|------|--------|-------------|----------------|
| 1 | 자식 실행 | 완료 판정기 + goal loop(`spawn/completion_judge.py`) | 작업, 최신 응답, 증거 요약; 예산 `goal_max_turns`(기본 `5`) | 모델 오류 / 파싱 불가 → `done` |
| 2 | 단계 | TaskFlow 단계 판정기(`agent/tools/taskflow/step_judge.py`) | 단계의 `validation_criteria`(없으면 판정기 호출 없음) | 모델 오류 / 파싱 불가 → `pass` |
| 3 | 흐름 | `taskflow_finish` 게이트 A–D(`agent/tools/taskflow/tools/taskflow_finish.py`) | DAG 상태와 흐름 증거; 게이트 D에는 `todo`와 `plan_path`도 필요 | 원장 읽기 불가 / 검증기 오류 → 통과 |
| 4 | 부모 턴 | 완료 drain 프로그램 게이트(`agent/middlewares/subagent_completion_drain/`) | 세션의 검증 증거 요약 | 조회 실패 → 게이트 생략 |

**2계층——단계 판정기.** `taskflow_resume`이 자식 결과를 주입한 뒤, `validation_criteria`를 가진 단계는 `PASS` / `RETRY` / `BLOCK`으로 판정됩니다. `RETRY`는 단계 재시도 횟수가 `STEP_JUDGE["max_retries"]`(기본 `2`) 미만이면 판정기 피드백과 함께 단계를 재디스패치하고, 예산을 다 쓰면 단계를 차단합니다. 판정 기준은 판정기의 입력이지 스위치가 아닙니다——기준이 없는 단계는 판정기로 보내지지 않습니다.

**3계층——흐름 게이트.** `taskflow_finish`는 다음을 모두 만족할 때까지 `DONE` 전환을 거부합니다: 게이트 A가 모든 단계를 `done` 또는 `blocked`로 보고, 게이트 B가 차단된 단계를 보지 않고, 게이트 C가 `FAIL`이나 `[stale]` 흐름 증거를 보지 않으며, 그리고——호출자가 `todo`와 `plan_path`를 모두 제공한 경우에만——게이트 D가 `SisyphusVerifier`를 통과할 것. 게이트 D의 연결은 스위치가 아니라 입력입니다: 그것이 없으면 완료에 독립적 판정이 없고, 생략된 게이트는 조용히 넘어갑니다.

**4계층——부모 턴 게이트.** drain이 대기 중인 완료 캐리어를 주입할 때 부모 세션의 증거도 검사합니다. 캐리어는 그대로 주입되고, 세션에 통과 증거가 없으면 필수 검증 메시지가 그 뒤에 덧붙습니다. 이 검사는 무조건적이며——어떤 설정으로도 끌 수 없고——증거 조회 자체가 이용 불가능할 때만 페일오픈합니다.

파싱된 부정적 판정은 항상 차단합니다: 페일오픈이 덮는 것은 모델 오류, 파싱 불가능한 출력, 이용 불가능한 조회이며 실제 `RETRY`, `BLOCK`, 실패한 증거 행이 아닙니다. 게이트 내부 세부(브레이커 임계값, announce 재시도, 증거 원장)는 [런어웨이 루프 방지 README](../loop-prevention/README.ko.md)와 [장기 실행 작업 README](../long-running-tasks/README.ko.md)에 있습니다.

## 🔗 다른 하위 시스템과의 관계

| 하위 시스템 | 이 경계를 넘는 것 | 세부 |
|-------------|-------------------|------|
| TaskFlow 엔진 | 단계 디스패치, `validation_criteria`, `aggregate_deps`, 흐름 게이트 A–D | [장기 실행 작업](../long-running-tasks/README.ko.md) |
| 런어웨이 루프 방지 | 완료 게이트, 판정기 예산, drain과 announce 재시도 동작 | [런어웨이 루프 방지](../loop-prevention/README.ko.md) |
| 컨텍스트 엔진 | MesMemory에 영속되는 것은 완료 캐리어뿐; 자식 트랜스크립트는 checkpoint에만 존재 | [Context Engine README](../../context_engine/README.ko.md) |
| 런타임 레인 | `SUBAGENT` 레인이 동시 자식 실행을 제한; 초과 spawn은 `PENDING`으로 대기 | [런어웨이 루프 방지](../loop-prevention/README.ko.md) |
| 메모리 파일 | 비어 있지 않은 drain은 `MEMORY.md`와 `USER.md`를 재조정(읽기 → 영속화, 페일오픈) | [서브에이전트 시스템 README](../../agent/tools/subagent/README.ko.md) |

### 🧵 합성 집계

`taskflow_run_task(aggregate_deps=True)`는 합성 단계를 표시해 디스패치되는 작업 텍스트에 의존 단계의 기록된 결과를 싣습니다. 플래그는 단계에 저장되고, `build_task_with_dep_results(step, steps, results)`가 `depends_on` 순서로 `## Upstream Results` 블록을 덧붙입니다. 각 의존성마다 `### {step_id}` 절 하나를 두고, 의존성의 `child_session_key`를 흐름의 `{child_session_key, result, result_hash}` 레코드와 대조합니다. 기록이 없는 의존성은 `no result recorded` 자리표시자를 기여합니다. 집계는 디스패치나 재시도마다 안정적인 레코드에서 다시 도출되므로 저장된 단계 작업이 재작성되는 일은 결코 없습니다. 플래그가 없으면 작업 텍스트는 그대로입니다. 전체 동작은 [장기 실행 작업 README](../long-running-tasks/README.ko.md)에 있습니다.

## ⚙️ 설정

| 노브 | 위치 | 기본값 | 효과 |
|------|------|--------|------|
| `goal_max_turns` | `config/features/agent_side/completion_judge.py` | `5` | goal loop 턴 예산(첫 턴 포함) |
| spawn별 `goal_max_turns` | `sessions_spawn` 파라미터 | `None` → 설정값 | 단일 spawn의 예산 재정의 |
| `max_retries` | `config/features/agent_side/step_judge.py` | `2` | 단계가 차단되기 전까지의 `RETRY` 횟수 |
| `default_functional_role` | `SubagentConfig` | `"general"` | 힌트와 `agent_id` 일치가 모두 실패할 때의 폴백 |
| `roles_override_dir_name` | `SubagentConfig` | `"subagent_roles"` | 선택적 역할 재정의를 두는 워크스페이스 디렉터리 |
| `max_spawn_depth` | `SubagentConfig` | `2` | 역할이 `LEAF`로 뒤집히는 깊이(하드 상한 `2`) |
| `verify_commands` | `config/features/agent_side/evidence_ledger.py` | test / lint / build / typecheck / format 명령군 | 검증 증거로 자동 기록되는 명령 |

## 🗺️ 테스트 맵

| 영역 | 테스트 |
|------|--------|
| 역할 enum과 로더 | `tests/agent/tools/subagent/types/test_functional_role.py`, `tests/agent/tools/subagent/roles/test_loader.py` |
| 역할 기반 spawn 동작 | `tests/agent/tools/subagent/spawn/test_functional_role_integration.py`, `tests/agent/tools/subagent/spawn/test_system_prompt_role.py` |
| spawn 권한 가드 | `tests/agent/tools/subagent/test_spawn_privilege_guard.py` |
| 완료 판정기와 drain | `tests/agent/tools/subagent/test_completion_judge.py`, `tests/agent/tools/subagent/test_completion_drain.py` |
| 부모 턴 게이트 | `tests/agent/middlewares/test_completion_drain_gate.py` |
| 단계 판정기 | `tests/agent/tools/taskflow/test_step_judge.py`, `tests/agent/tools/taskflow/test_resume_with_judge.py` |
| 흐름 게이트 | `tests/agent/tools/taskflow/test_finish_gate.py` |
| 합성 집계 | `tests/agent/tools/taskflow/test_synthesize.py`, `tests/agent/tools/taskflow/test_synthesize_e2e.py` |

## ⚠️ 한계

- 모든 판정기는 설계상 페일오픈입니다: 도달할 수 없거나 파싱할 수 없는 판정기는 `done` / `pass`로 확정됩니다. 가용성이 엄격함보다 우선합니다——멈춘 판정기가 실행을 가두어서는 안 됩니다.
- `goal_max_turns`가 `1`이면 자식은 단일 턴이 되고, 그 spawn에서는 완료 판정기가 호출되지 않습니다.
- 호출 시점 권한 가드가 뒷받침하는 것은 `sessions_spawn`입니다; `sessions_yield`는 조립 시점 제거에만 의존합니다.
- 게이트 D는 `todo`와 `plan_path`가 모두 있을 때만 활성화됩니다; 그 연결이 없으면 `taskflow_finish`에 독립적 완료 판정이 없습니다.
- 부모 턴 게이트는 구조화 원장이 아니라 텍스트 증거 요약과 그 `FAIL` / `[stale]` 마커로 추론합니다.
- `aggregate_deps`는 원시 의존성 결과를 연결할 뿐 요약하지 않으므로, 거대한 결과는 디스패치되는 작업 텍스트를 키웁니다.
- 자식 컨텍스트 격리는 설계상 고정입니다: 부모 트랜스크립트를 상속하는 모드는 존재하지 않습니다.

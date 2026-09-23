# 🔎 Code Intel: 서브에이전트를 위한 4계층 코드 검색

[**English**](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · **한국어**

> 이 문서는 [서브에이전트 시스템 README](../../agent/tools/subagent/README.ko.md)와 [서브에이전트 설계 페이지](../subagent/README.ko.md)의 설계 계층 자매편입니다. 전자는 spawn 파이프라인, 역할별 도구 정책, `sessions_*` 도구의 런타임과 API 레퍼런스이고, 후자는 두 축 역할 모델(깊이 역할 × 기능 역할)을 설명합니다. 이 페이지는 그 역할들이 사용하는 코드 검색 프레임워크——tree-sitter 심볼 인덱스, ast-grep 구조 검색과 재작성, LSP 정밀 검색 도구, 임베딩 기반 시맨틱 코드 검색——를 기록합니다.

사실 출처: `agent/tools/code_intel/**`, `config/features/agent_side/code_intel.py`, `config/features/agent_side/code_intel_semantic.py`, `config/features/agent_side/ast_grep.py`, `config/features/agent_side/lsp.py`, `config/path.py`, `agent/tools/subagent/types/functional_role.py`, `agent/tools/subagent/spawn/core.py`, `agent/tools/subagent/spawn/system_prompt.py`, `agent/tools/subagent/roles/definitions/librarian/AGENTS.md`. 아래의 모든 서술은 이 코드에 대해 검증되었습니다.

## 목차

- [개요](#-개요)
- [설계 불변식](#-설계-불변식)
- [제 1 계층: Tree-sitter 심볼 인덱스](#-제-1-계층-tree-sitter-심볼-인덱스)
- [제 2 계층: ast-grep 구조 검색과 재작성](#-제-2-계층-ast-grep-구조-검색과-재작성)
- [제 3 계층: LSP 정밀 검색](#-제-3-계층-lsp-정밀-검색)
- [제 4 계층: 시맨틱 코드 검색](#-제-4-계층-시맨틱-코드-검색)
- [역할 모델과 도구 면](#-역할-모델과-도구-면)
- [설정](#-설정)
- [환경 능력 매트릭스](#-환경-능력-매트릭스)
- [한계와 실패 모드](#-한계와-실패-모드)
- [테스트 맵](#-테스트-맵)
- [관련 문서](#-관련-문서)

## 🎯 개요

Code Intel은 네 가지 검색 엔진으로 서브에이전트의 코드 이해 질문에 답하며, 각 엔진은 서로 다른 질문 형태를 담당합니다:

| 계층 | 답하는 질문 | 도구 | 개방 범위 |
|------|-------------|------|-----------|
| **Tree-sitter 심볼 인덱스** | 심볼은 어디에 정의되고 누가 호출하는가? | `explore`, `callers`, `callees`, `impact` | `researcher` + `librarian` |
| **ast-grep** | 어떤 코드가 이 구조 형태를 가지며 어떻게 재작성하는가? | `ast_grep_search`, `ast_grep_rewrite` | 모든 서브에이전트 |
| **LSP** | 타입 검사기는 이 위치에 대해 무엇을 아는가? | `lsp_goto_definition`, `lsp_find_references`, `lsp_workspace_symbol`, `lsp_call_hierarchy`, `lsp_rename`, `lsp_diagnostics`, `lsp_format`, `lsp_status` | `researcher` + `librarian` |
| **시맨틱 검색** | 이 개념을 구현한 코드는 무엇인가? | `semantic_code_search` | `researcher` + `librarian` |

네 계층은 하나의 워크플로로 결합됩니다: `explore`가 심볼과 호출 경로를 찾고, `ast_grep_search`가 언어를 넘나들며 구조 패턴을 찾고, LSP 도구가 타입을 인식해 정의와 참조를 해석하며, `semantic_code_search`가 이름이 맞지 않을 때 개념으로 검색합니다. 네 계층 모두 자식 에이전트 조립 시점에 구성되어 주입됩니다.

```text
child agent assembly (spawn/core.py)
  base tools → role policy (allow / deny / main_only gate)
    ├─ role ∈ CODE_INTEL_ROLES?  → + explore/callers/callees/impact/semantic_code_search
    │                              + the eight lsp_* tools
    └─ always                    → + ast_grep_search / ast_grep_rewrite
```

## 🧱 설계 불변식

1. **메인 에이전트는 이 도구들을 절대 보지 못합니다.** 어떤 code-intel 또는 LSP 빌더도 `_MAIN_TOOLS_BUILDERS`에 등록되지 않으며, 주입은 `_build_child_agent` 내부에서 역할 도구 정책 뒤에만 일어납니다.
2. **도구 면은 역할로 제한됩니다.** tree-sitter 스위트와 8개 LSP 도구는 `CODE_INTEL_ROLES`(`researcher`, `librarian`)에만 전달됩니다. ast-grep은 핵심 구조 능력이므로 모든 기능 역할이 받습니다.
3. **fail-open이 계약입니다.** 없는 바이너리, 파싱할 수 없는 파일, 사용할 수 없는 모델, 타임아웃, 거부된 요청은 모두 실행 가능한 메시지가 담긴 JSON 페이로드를 반환합니다. 어떤 검색 실패도 자식 턴으로 예외를 던지지 않습니다.
4. **모든 것이 구조적으로 유계입니다.** 파일 수, 파일당 바이트, 빌드 시간, 배치, 결과 수, 경로 수, 패턴 크기, 열린 파일 수에 상한이 있습니다. LSP 관리자는 LRU 동시성 상한과 유휴 회수를 추가합니다.
5. **쓰기는 옵트인이며 포함 관계를 검증합니다.** `ast_grep_rewrite`는 `dry_run=false`가 아니면 미리보기만 하고, `lsp_rename`과 `lsp_format`은 적용 플래그 없이는 미리보기만 합니다. 모든 쓰기 경로는 디스크를 건드리기 전에 경로 탈출과 프로젝트 루트 포함 관계를 다시 검증합니다.
6. **새 모델 공급자를 도입하지 않습니다.** 시맨틱 검색은 기존 `models/embed_model`과 `models/reranker_model` 래퍼를 재사용하며, 유일한 새 저장소는 SQLite 인덱스입니다.
7. **루트는 호출마다 해석됩니다.** `SHERRY_CODE_INTEL_ROOT`, `SHERRY_SG_ROOT`, `SHERRY_LSP_ROOT`가 드릴과 테스트를 위해 작업 루트를 덮어씁니다. 설정하지 않으면 프로세스 cwd와 공용 `resolve_project_path` 게이트가 적용됩니다.

## 🌳 제 1 계층: Tree-sitter 심볼 인덱스

인덱스는 네 모듈로 구현됩니다: `agent/tools/code_intel/extract.py`(순수 바이트 → 심볼과 호출 지점), `agent/tools/code_intel/indexer.py`(SQLite 영속화와 증분 순회), `agent/tools/code_intel/query.py`(매칭과 호출 그래프 질의), `agent/tools/code_intel/tools.py`(LangChain 래퍼).

| 문법 | 언어 키 | 확장자 | 심볼 노드 |
|------|---------|--------|-----------|
| Python | `python` | `.py` | `function_definition`, `class_definition`, `call` |
| TypeScript | `typescript`, `tsx` | `.ts`, `.tsx`, `.js`, `.jsx` | `function_declaration`, `class_declaration`, `method_definition`, `variable_declarator`(화살표와 함수 표현식), `call_expression` |
| Rust | `rust` | `.rs` | `function_item`, `struct_item`, `enum_item`, `trait_item`, `impl_item`, `call_expression`, `macro_invocation` |
| Go | `go` | `.go` | `function_declaration`, `method_declaration`, `type_spec`(`struct_type`와 `interface_type`), `call_expression` |

노드 이름과 문법 버전은 고정된 패키지에 대해 실측되었습니다——`tree-sitter 0.26.0`, `tree-sitter-python 0.25.0`, `tree-sitter-typescript 0.23.2`, `tree-sitter-rust 0.24.2`, `tree-sitter-go 0.25.0`——파서는 `Parser(Language(capsule))`로 생성됩니다. `javascript`는 `typescript` 문법으로, `jsx`는 `tsx`로 별칭 처리됩니다. 결과는 세 개의 SQLite 테이블 `symbols`, `call_edges`, `index_meta`에 저장됩니다. 호출 그래프는 두 번째 단계에서 우선순위대로 해석됩니다: 같은 파일, 같은 디렉터리, 임의의 전역 일치, 마지막으로 거리 ≤ 2의 Levenshtein 퍼지 일치입니다.

증분성과 상한: 각 파일의 `mtime`과 `size`가 `index_meta`와 비교되고, 바뀐 파일만 다시 파싱되며, 삭제된 파일은 해석된 인바운드 간선의 분리를 포함해 제거되고, 쓰기는 100개 파일 단위 트랜잭션으로 배치 처리됩니다. 순회는 `code_intel_index_max_files`(5000) 또는 `code_intel_index_timeout_s`(60)에서 멈추고 `truncated`를 보고합니다. fail-open은 파일 단위입니다: 문법 오류, 초대형 파일(1 MB 초과), 알 수 없는 확장자, 읽기 오류는 각각 `index_meta` 행을 기록하고 건너뜁니다——문법 오류 파일이 부분적으로 인덱싱되는 일은 없습니다. 변경 호출은 인스턴스 잠금으로 직렬화되고, 연결은 WAL과 30초 busy timeout을 사용합니다.

질의는 읽기 전에 증분 인덱스를 새로 고치므로 편집 후 검색이 오래된 심볼을 반환하지 않고, 트리가 바뀌지 않았으면 값싼 no-op입니다. 네 도구는 다음과 같습니다:

| 도구 | 입력 | 출력 | 상한 |
|------|------|------|------|
| `explore` | 모호한 개념, 심볼 이름, 자연어 의도 | 일치한 심볼의 소스, docstring, 직접 호출자와 피호출자; 일치가 없으면 제안 | 심볼 10개, 소스 8000자 |
| `callers` | 심볼 이름 | 그 심볼을 호출하는 모든 함수와 메서드, 호출 지점 행 포함 | 정확한 이름 우선, 퍼지 폴백 |
| `callees` | 심볼 이름 | 그 심볼이 내보내는 모든 호출, 알려진 경우 해석된 대상 파일과 행 포함 | 중복 호출 지점 병합 |
| `impact` | 심볼 이름 | 전이적 호출자(폭발 반경), 깊이와 경유한 이름 포함 | 깊이 3, `truncated` 플래그 |

## 🧬 제 2 계층: ast-grep 구조 검색과 재작성

ast-grep은 검색 패턴을 정규식이 아니라 코드로 취급하므로 `$NAME`은 AST 노드 하나를, `$$$NAME`은 0개 이상의 노드를 일치시킵니다. 구현은 `agent/tools/code_intel/ast_grep/`에 있습니다: `resolver.py`(바이너리 발견), `provisioner.py`(검증된 다운로드), `install_hints.py`(복구 힌트), `runner.py`(두 도구). 독립 실행형 폴백 설치 스크립트는 같은 디렉터리의 `scripts/install.sh`와 `scripts/install.ps1`입니다.

바이너리 발견은 5계층이며, 모든 후보는 비어 있지 않은 일반 파일이면서 출력에 `ast-grep`이 포함되는 짧은 `--version` 프로브를 통과해야 합니다:

1. 명시적 재정의——환경 변수 `SHERRY_SG_PATH`.
2. 프로비저닝된 런타임——`~/.sherry/runtime/ast-grep/<platform>-<arch>/sg`.
3. code-intel bin 캐시——`.codeintel/ast-grep/bin`(`ast-grep` 다음 `sg`).
4. `PATH` 조회——`ast-grep` 다음 `sg`, Windows `PATHEXT` 존중.
5. Homebrew와 Linuxbrew 접두사.

어떤 계층도 해석하지 못하면 첫 도구 호출이 고정된 `0.43.0` 릴리스를 자동 프로비저닝합니다: 플랫폼별 URL과 SHA-256은 `config/features/agent_side/ast_grep.py`에 있고, 다운로드는 60초 타임아웃으로 유계되며, 체크섬 불일치는 치명적입니다(fail-closed——검증되지 않은 바이트를 설치하지 않습니다). 압축 해제는 표준 라이브러리 `zipfile`을 사용합니다(호스트에 `unzip`이 없을 수 있기 때문). 자체 경로 기준으로 재실행되어 일부 샌드박스에서 동작하지 않는 `sg` 런처보다 실제 `ast-grep` 바이너리가 우선되며, 추출물은 모드 755로 원자적으로 기록됩니다. 해석 결과는 파일이 존재하는 동안 프로세스 단위로 캐시됩니다.

두 도구는 환경을 세척한 뒤 `sg` CLI를 서브프로세스로 30초 타임아웃으로 실행합니다:

| 도구 | 동작 | 안전 기본값 |
|------|------|-------------|
| `ast_grep_search` | 기본으로 `--strictness smart`를 붙여 `sg run --json=stream` 실행; 압축된 일치(파일, 1 기반 행, 텍스트, 메타 변수) 반환 | 최대 50 일치, 64 경로, 16 KiB 패턴 |
| `ast_grep_rewrite` | 교체 목록을 미리보기; `dry_run=false`(`--update-all`)일 때만 제자리 적용 | `dry_run` 기본값 `true` |

서브프로세스 실행 전에 모든 경로가 심사됩니다: 재정의가 없으면 정규 `resolve_project_path` 게이트를 쓰고, `SHERRY_SG_ROOT`가 설정되면 같은 탈출 판정과 그 루트에 대한 포함 검사를 적용합니다——따라서 적용형 재작성은 프로젝트 안에만 쓸 수 있습니다.

## 🛰️ 제 3 계층: LSP 정밀 검색

LSP 계층은 `agent/tools/code_intel/lsp/`에 있습니다: `protocol.py`(URI, 1 기반과 0 기반 위치, 결과 포맷), `resolver.py`(바이너리 발견), `installer.py`(허용 목록 자동 설치), `fallback.py`(가용성에서 실행 가능 메시지로), `client.py`(stdio 기반 JSON-RPC), `manager.py`(프로세스 수명주기), `tools.py`(8개 도구). 도구는 1 기반 `line`과 `character`를 받아 내부에서 LSP 네이티브 0 기반 위치로 변환합니다.

| 도구 | 용도 | 기본 동작 |
|------|------|-----------|
| `lsp_goto_definition` | 타입 인식 정의 점프 | 읽기 전용 |
| `lsp_find_references` | 심볼의 모든 참조 | 읽기 전용 |
| `lsp_workspace_symbol` | 워크스페이스 심볼 퍼지 검색 | 읽기 전용, 결과 수 상한 |
| `lsp_call_hierarchy` | 들어오는 호출자 또는 나가는 피호출자 | 읽기 전용 |
| `lsp_rename` | 워크스페이스 이름 변경 | `WorkspaceEdit` 미리보기; `dry_run=false`일 때만 적용 |
| `lsp_diagnostics` | 파일의 오류와 경고 | 비동기 `publishDiagnostics` 알림을 기다리고 `timed_out` 보고 |
| `lsp_format` | 전체 파일 또는 범위 포맷 | 미리보기; `write=true`일 때만 적용; 미지원이면 성공을 가장하지 않고 `supported=false` 보고 |
| `lsp_status` | 설정된 모든 서버의 정직한 가용성 | 아무것도 시작하지 않음 |

발견 순서는 ast-grep 계층과 같지만 marker 게이트가 더해집니다: 명시적 절대 경로, 그다음 같은 계층에 대응 marker 파일이 있을 때만 신뢰하는 저장소 로컬 bin 디렉터리(`pyproject.toml`은 `.venv/bin`, `package.json`은 `node_modules/.bin`, `Cargo.toml`은 `target/debug`, `go.mod`는 `bin`, 저장소 루트까지 상향 순회), 그다음 `~/.sherry/runtime/lsp` 배치, 그다음 `PATH`, 마지막으로 Homebrew. 해석 결과는 `(cwd, command, platform)` 단위로 캐시됩니다.

언어 서버는 무거운 상주 서브프로세스이므로 프로세스 단위 관리자가 제약합니다: 서버는 `(language, cwd)`에 대한 첫 요청에서 지연 시작하고, 동시에 살아 있는 수는 최대 `lsp_max_concurrent_servers`(2)개이며 초과 시 가장 오래 사용되지 않은 것을 내보냅니다. 유휴 스위퍼는 `lsp_idle_shutdown_s`(300초)를 넘겨 사용되지 않은 서버를 회수하고, `shutdown_all`은 `atexit` 훅에서 실행됩니다——클라이언트가 고아 프로세스를 남기는 일은 없습니다. 각 클라이언트는 `Content-Length`로 JSON-RPC를 프레이밍하고, 리더 스레드에서 응답을 분배하고, 서버→클라이언트 요청에 빈 결과로 답하고, 모든 대기를 타임아웃으로 유계하며, 열린 파일을 32개로 제한하고(가장 오래된 것 닫기), 1 MB 초과 파일을 거부합니다.

가용성은 3상태로 보고됩니다: `available`, `not_installed`(설정되었지만 발견을 통과한 바이너리가 없음——설치 힌트와 로컬 설치 명령 반환), `not_configured`(언어가 `lsp_enabled_languages`에 없음). 사용할 수 없는 모든 경로는 도구 이름, 설치 힌트, 그리고 `explore` 또는 `terminal`(rg/grep)로의 폴백을 반환합니다. 자동 설치는 기본적으로 꺼져 있고(`lsp_auto_install=False`), 켜져도 대상 언어의 허용 목록 명령만 60초 타임아웃과 세척된 환경으로 실행합니다.

## 🧠 제 4 계층: 시맨틱 코드 검색

시맨틱 검색은 임의의 행 창을 자르는 대신 제 1 계층 심볼 테이블을 임베딩하므로, 각 벡터는 정확히 하나의 함수, 메서드, 클래스를 설명합니다. `agent/tools/code_intel/semantic/chunker.py`는 심볼과 그 위치를 밝히는 헤더 행과 심볼 본문으로 청크를 만듭니다(읽기 실패 시 저장된 스냅숏으로 폴백). `agent/tools/code_intel/semantic/indexer.py`가 `code_embeddings` 테이블을 소유하고, `agent/tools/code_intel/semantic/search.py`가 코사인으로 순위를 매기고 선택적으로 재순위합니다.

인덱스는 구조적으로 증분입니다: 먼저 심볼 인덱스를 새로 고치고, 심볼이 더 이상 없는 임베딩을 삭제하고, 벡터가 없는 심볼만 임베딩합니다. 행은 16개 청크 단위(설정 가능)로 기록되고, 한 번의 빌드로 최대 1000개 청크, 파일당 최대 60개, 청크당 최대 2000자를 임베딩합니다. 벡터는 `array('d')` BLOB으로 `code_embeddings` 테이블에 저장되며, 같은 테이블에 `model` 이름과 `dim`도 보관합니다. 저장된 모델 변경이나 차원 충돌을 감지하면 같은 호출에서 인덱스를 비우고 재빌드하며, 부분적으로 임베딩된 인덱스는 이미 임베딩된 심볼을 건너뛰므로 재개할 수 있습니다. 임베딩 백엔드는 기존 `models.build_embed_model`(`EMBEDDING_*` 환경 변수로 선택, 기본은 로컬 `bge-m3`)이며 새 공급자를 도입하지 않습니다.

검색은 먼저 인덱스를 자가 치유하고, 질의를 임베딩하고, 저장된 모든 청크를 순수 Python 코사인 유사도로 순위를 매기고, 후보 풀 40을 유지하고, 리랭커가 설정되어 있으면 상위 후보를 기존 `models.build_reranker_model`에 넘깁니다. 최종 페이지는 기본 `code_intel_semantic_default_top_k`(5)개, 최대 20개입니다. fail-open 상태는 페이로드에 명시됩니다: 인덱싱된 임베딩이 없으면 먼저 인덱스를 만들라는 메시지를 보내고, 질의 모델을 쓸 수 없으면 `degraded` 메시지를 반환하며, 리랭커가 없으면 코사인 순서를 유지하며 `reranked=false`가 되고, 인덱스와 질의 차원이 다르면 무의미한 채점 대신 재빌드를 요구합니다.

## 🧭 역할 모델과 도구 면

`agent/tools/subagent/types/functional_role.py`의 `CODE_INTEL_ROLES`는 도구 주입(`agent/tools/subagent/spawn/core.py`)과 프롬프트 안내(`agent/tools/subagent/spawn/system_prompt.py`) 모두의 단일 진실 원천이므로, 도구 면과 그 문서가 서로 어긋날 수 없습니다:

| 기능 역할 | Tree-sitter 스위트 | LSP 도구 | ast-grep |
|-----------|--------------------|----------|----------|
| `researcher` | 있음 | 있음 | 있음 |
| `librarian` | 있음 | 있음 | 있음 |
| `general` | 없음 | 없음 | 있음 |
| `executor` | 없음 | 없음 | 있음 |
| `reviewer` | 없음 | 없음 | 있음 |

메인 에이전트는 이 표 밖에 전혀 있습니다: 빌더는 `_MAIN_TOOLS_BUILDERS`에 결코 추가되지 않으며, 격리 테스트가 `build_lsp_tools`의 부재를 그 목록과 `agent.tools` 네임스페이스 양쪽에서 주장합니다. `agent/tools/subagent/roles/definitions/librarian/AGENTS.md`의 `librarian` 정의는 의도된 외부 저장소 워크플로——`terminal`로 클론하고, `explore`와 `semantic_code_search`로 인덱싱·검색하고, 영구 링크로 답하기——를 기록합니다. 두 축 역할 모델 자체(깊이 역할 × 기능 역할)는 [서브에이전트 설계 페이지](../subagent/README.ko.md)에, 역할별 도구 목록은 [서브에이전트 시스템 README](../../agent/tools/subagent/README.ko.md)에 있습니다.

## ⚙️ 설정

| 객체 | 모듈 | 주요 항목(기본값) |
|------|------|-------------------|
| `CODE_INTEL` | `config/features/agent_side/code_intel.py` | 최대 5000 파일, 파일당 1 MB, 빌드 예산 60초, 배치 100, explore 10 심볼과 8000자, 호출 깊이 3, 퍼지 점수 0.3 |
| `CODE_INTEL_SEMANTIC` | `config/features/agent_side/code_intel_semantic.py` | 모델 `bge-m3`, top-K 5(최대 20), 후보 풀 40, 배치 16, 빌드당 1000 청크, 파일당 60, 청크당 2000자 |
| `AST_GREP` | `config/features/agent_side/ast_grep.py` | 고정 `0.43.0`, 50 일치, 16 KiB 패턴, 30초 실행 타임아웃, 64 경로, 60초 프로비저닝 타임아웃, 5초 버전 프로브 |
| `LSP` | `config/features/agent_side/lsp.py` | 요청 10초, 시작 15초, 진단 15초, 동시 서버 2, 유휴 종료 300초, 50 결과, 열린 파일 32, 파일당 1 MB, 자동 설치 꺼짐 |
| `CODE_INTEL_ROLES` | `agent/tools/subagent/types/functional_role.py` | `researcher`와 `librarian` |

인덱스 데이터베이스 경로는 기본 `CODE_INTEL_DIR / "index.db"`이며, `config/path.py`의 `CODE_INTEL_DIR = ROOT_DIR / ".codeintel"`입니다. `SHERRY_CODE_INTEL_ROOT`와 `SHERRY_CODE_INTEL_DB`가 호출 시 루트와 데이터베이스 경로를 덮어쓸 수 있습니다.

## 🖥️ 환경 능력 매트릭스

발견은 능력이 아닙니다: 아래 표는 2026-09-23 개발 호스트에서 실측한 상태이며, "발견"은 해석기가 후보 바이너리를 찾았다는 뜻일 뿐 서버가 시작하거나 응답한다는 증명이 아닙니다. `python`만 실제 종단 간 스모크가 있고, `typescript`는 발견뿐이며, `rust`는 rustup 심으로 해석되지만 `rust-analyzer` 구성 요소가 설치되어 있지 않아 시작이 실패하고 폴백 메시지로 강등되며, 나머지 7개 언어는 `not_installed`입니다.

| 언어 | 서버 명령 | 이 호스트에서 발견 | 검증 깊이 |
|------|-----------|--------------------|-----------|
| `python` | `basedpyright-langserver --stdio` | 예——저장소 `.venv/bin` | 실제 스모크: 정의, 참조, 진단, 이름 변경 미리보기, 상태 |
| `typescript` | `typescript-language-server --stdio` | 예——`PATH` | 발견뿐 |
| `rust` | `rust-analyzer` | 바이너리 있음——툴체인 구성 요소 없음 | 시작이 폴백으로 강등 |
| `go` | `gopls` | 아니오 | 설치 힌트와 폴백 |
| `cpp` | `clangd` | 아니오 | 설치 힌트와 폴백 |
| `java` | `jdtls` | 아니오 | 설치 힌트와 폴백 |
| `ruby` | `ruby-lsp` | 아니오 | 설치 힌트와 폴백 |
| `bash` | `bash-language-server start` | 아니오 | 설치 힌트와 폴백 |
| `vue` | `vue-language-server --stdio` | 아니오 | 설치 힌트와 폴백 |
| `yaml` | `yaml-language-server --stdio` | 아니오 | 설치 힌트와 폴백 |

같은 호스트의 나머지 세 엔진:

| 엔진 | 상태 | 증거 |
|------|------|------|
| Tree-sitter 인덱스 | 4개 문법 설치됨(`tree-sitter 0.26.0` 계열) | 전체 code-intel 스위트 통과(281개 테스트) |
| ast-grep | 프로비저닝된 런타임 계층에서 사용 가능(`ast-grep 0.43.0`) | 기본 구조 검색이 실제 일치를 반환 |
| 시맨틱 검색 | 로컬 `bge-m3` 백엔드 사용 가능 | 실제 임베딩 스모크가 개념 질의에 관련 심볼을 반환 |

이 능력이 없는 머신은 실패 대신 강등됩니다: ast-grep이 해석되지 않으면 검증된 자동 프로비저닝 경로가 동작하고, 임베딩 백엔드를 쓸 수 없으면 `semantic_code_search`가 `degraded` 메시지를 반환하며 `explore`와 `terminal`을 가리킵니다.

## ⚠️ 한계와 실패 모드

- **인덱스는 호출자가 트리거하고 스스로 치유합니다.** 백그라운드 인덱서는 없습니다: 첫 질의가 인덱스를 만들고, 이후 모든 질의가 증분 갱신합니다. 큰 저장소는 첫 호출에서 빌드 비용을 지불하며 파일과 시간 상한으로 유계됩니다(`truncated` 보고).
- **파일 이벤트 자동 동기화는 채택하지 않았습니다.** 시맨틱 검색이 쿼리마다 인덱스를 자가 치유하고 인덱스는 필요할 때 재구축되므로, 상주 파일 감시 계층은 프로세스 오버헤드만 더하고 이득은 미미합니다.
- **문법 오류, 초대형, 알 수 없는 확장자 파일은 건너뜁니다.** 각각 이유를 기록한 `index_meta` 행이 붙고, 부분 인덱싱되지 않습니다.
- **임베딩 배치는 모델을 다시 로드합니다.** 각 `embed_documents` 호출이 백엔드를 로드하므로(로컬 GGUF 로더는 배치마다 호출됨) 배치는 로드 시간과 최대 메모리의 절충입니다. 빌드당·파일당 상한이 이를 유계로 유지합니다.
- **리랭커 미설정은 순위 품질 손실일 뿐입니다.** 없으면 결과는 코사인 순서로 남고 페이로드가 `reranked=false`를 알리며, 있어도 실패하면 같은 코사인 순서로 폴백합니다.
- **ast-grep의 첫 사용은 바이너리를 다운로드합니다.** 프로비저닝 경로는 60초 타임아웃으로 유계되고 체크섬 불일치면 설치를 거부합니다. 오프라인이거나 자산이 없는 플랫폼에서는 설치 힌트를 대신 반환합니다.
- **LSP 서버는 무겁습니다.** 관리자는 동시 2개로 제한하고 300초 유휴 후 회수하며 가장 오래 사용되지 않은 서버를 내보냅니다. 어떤 언어의 첫 요청은 서버 시작 비용을 지불하고, 서버가 창 안에 아무것도 발행하지 않으면 `lsp_diagnostics`가 `timed_out`을 반환할 수 있습니다.
- **이 호스트의 LSP 커버리지는 부분적입니다.** 발견 가능한 것은 `python`과 `typescript`뿐입니다. `rust`는 해석되지만 툴체인 구성 요소 없이는 시작할 수 없고, 나머지 7개 언어는 설치가 필요합니다. 자동 설치는 기본적으로 꺼져 있어 서버 부재가 패키지 관리자 실행을 유발하지 않습니다.
- **librarian의 도구 면은 `read_file` / `terminal` / `web_search`에 code-intel 제품군을 더한 것입니다.** 여기에는 `search_files`가 포함되지 않습니다 —— 해당 빌더가 `_MAIN_TOOLS_BUILDERS`에 없으므로, 외부 저장소 키워드 검색은 `terminal`(rg/grep)과 `explore`로 수행합니다.
- **쓰기는 설계상 2단계입니다.** `ast_grep_rewrite`, `lsp_rename`, `lsp_format`은 명시적으로 요청받았을 때만 씁니다. 미리보기가 안전한 기본이며, 적용된 편집도 포함 검사를 받습니다.

## 🗺️ 테스트 맵

| 영역 | 테스트 |
|------|--------|
| 심볼 인덱스, 호출 그래프, 종단 간 질의 | `tests/agent/tools/code_intel/test_indexer.py`, `tests/agent/tools/code_intel/test_query.py`, `tests/agent/tools/code_intel/test_e2e.py`, `tests/agent/tools/code_intel/test_integration.py` |
| 인덱스 도구와 역할 격리 | `tests/agent/tools/code_intel/test_tools.py`, `tests/agent/tools/code_intel/test_integration.py` |
| ast-grep 발견, 프로비저닝, 도구 | `tests/agent/tools/code_intel/ast_grep/test_resolver.py`, `tests/agent/tools/code_intel/ast_grep/test_provisioner.py`, `tests/agent/tools/code_intel/ast_grep/test_runner.py`, `tests/agent/tools/code_intel/ast_grep/test_install_hints.py`, `tests/agent/tools/code_intel/ast_grep/test_ast_grep_e2e.py` |
| LSP 프로토콜, 리졸버, 설치기, 폴백, 클라이언트, 관리자, 도구 | `tests/agent/tools/code_intel/lsp/test_protocol.py`, `tests/agent/tools/code_intel/lsp/test_resolver.py`, `tests/agent/tools/code_intel/lsp/test_installer.py`, `tests/agent/tools/code_intel/lsp/test_fallback.py`, `tests/agent/tools/code_intel/lsp/test_client.py`, `tests/agent/tools/code_intel/lsp/test_manager.py`, `tests/agent/tools/code_intel/lsp/test_lsp_tools.py`, `tests/agent/tools/code_intel/lsp/test_lsp_extended.py` |
| LSP 역할 격리와 실제 스모크 | `tests/agent/tools/code_intel/lsp/test_role_isolation.py`, `tests/agent/tools/code_intel/lsp/test_lsp_smoke.py`, `tests/agent/tools/code_intel/lsp/test_lsp_e2e.py` |
| 시맨틱 청킹, 인덱싱, 검색, 스모크 | `tests/agent/tools/code_intel/semantic/test_chunker.py`, `tests/agent/tools/code_intel/semantic/test_indexer.py`, `tests/agent/tools/code_intel/semantic/test_search.py`, `tests/agent/tools/code_intel/semantic/test_semantic_e2e.py`, `tests/agent/tools/code_intel/semantic/test_semantic_smoke.py` |
| 역할 배선과 프롬프트 절 | `tests/agent/tools/subagent/types/test_functional_role.py`, `tests/agent/tools/subagent/roles/test_loader.py`, `tests/agent/tools/subagent/spawn/test_functional_role_integration.py`, `tests/agent/tools/subagent/spawn/test_system_prompt_role.py` |

## 🔗 관련 문서

| 페이지 | 내용 |
|--------|------|
| [서브에이전트 시스템 README](../../agent/tools/subagent/README.ko.md) | 런타임과 API 레퍼런스: spawn 파이프라인, 레지스트리, 역할별 도구 목록 |
| [서브에이전트 설계](../subagent/README.ko.md) | 두 축 역할 모델(깊이 역할 × 기능 역할)과 spawn 권한 가드 |
| [Context Engine README](../../context_engine/README.ko.md) | 시맨틱 계층이 재사용하는 임베딩 저장과 검색 패턴 |

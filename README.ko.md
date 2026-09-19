# 🍊 EMA AI Agent - Sherry

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **LangChain/LangGraph와 멀티모달 기술로 구축된 딥 롤플레잉 AI 에이전트.**

## ✨ 소개

EMA AI Agent는 장기 기억과 복잡한 추론 능력을 갖춘 고도로 의인화된 AI 에이전트 시스템입니다. 단순한 챗봇이 아니라, 독립적인 **페르소나**, 동적인 **스킬 시스템**, 그리고 예약 작업과 백그라운드 서브에이전트를 통한 능동적 행동을 갖춘 가상 컴패니언입니다.

에이전트의 캐릭터 **Sherry(치바나 셰리)** 는 자칭 소녀 탐정입니다. 겉으로는 언제나 밝고 활기차지만, 속은 침착하고 날카롭습니다. 시스템 전체는 세션을 넘어 기억이 축적되는 몰입형 지속 롤플레이를 지원하도록 설계되었습니다.

---

## 🚀 주요 기능

### 1. 🧠 계층형 메모리 시스템 (Context Engine)
- **단기 세션 메모리**([MesMemory](context_engine/README.md)): 대화 이력을 SQLite(WAL 모드)에 영구 저장하고 FTS5 인덱스를 자동 생성 — 중국어 전문 검색용 trigram 토크나이저 테이블 포함; 영속화는 두 시점으로 나뉩니다(`MessagePersistenceMiddleware`): 도구 결과는 반환되는 즉시 플러시되고, 나머지 새 human/ai/tool 메시지는 각 모델 호출 경계에서 증분 플러시됩니다(`persisted_message_ids` 워터마크로 write-once). 원본 저장소는 압축 발생에 의존하지 않습니다
- **히스토리 조회**: 최근 N턴, 페이지네이션 히스토리, 턴 범위 지정 쿼리를 프롬프트 컨텍스트로 포맷
- **세션 체크포인팅**: 스레드 세이프 비동기 SQLite 체크포인터(`langgraph-checkpoint-sqlite`)가 재시작 후에도 에이전트 상태를 유지하며, 오래된 체크포인트는 자동 정리
- **대화 요약**: Summarization 미들웨어가 auxiliary LLM으로 긴 대화 이력을 도중에 압축
- **컨텍스트 거버넌스**: 과대한 도구 결과와 인간 메시지는 복구 가능한 디스크 사본을 남기고 프롬프트에서 축출되며, `read_file` 결과는 슬라이스되고, 어떤 압축보다도 먼저 LLM 없는 테일 클립이 돌며, 체인 요약은 대화 페이로드에서 제외됩니다
- **프라이빗 지식 그래프 RAG**: `multimodal_rag` 스킬이 문서/폴더를 엔티티-관계 그래프로 인덱싱(벤더드 LightRAG + RAG-Anything, `snkv` 벡터 스토리지)하고 멀티홉 그래프 검색으로 답변
- **경험 추출(Experience Extraction)**: 네 개의 라이프사이클 경로가 대화 이력을 재사용 가능한 경험으로 축적합니다: 압축 시점 memory review(압축마다), 압축 시 todo가 전부 완료일 때의 plan 추출, 압축 전 memory flush, 압축 후 todo fork. 이들은 MEMORY.md / USER.md, plan 지식 베이스(`agent/tools/todolist/knowledge/`), `skills/auto/`, `todos.db`에 각각 기록합니다
- ▶️ _아키텍처, 데이터 모델, API 세부사항은 [Context Engine README](context_engine/README.md) 참조_
- ▶️ _네 개 추출 경로와 스킬 큐레이션은 [Experience README](docs/experience/README.ko.md) 참조_

### 2. 🛠️ 동적 스킬 시스템
- **SKILL.md 표준**: 스킬은 YAML 프론트매터(`name`, `description`, 선택적 `scope: all | main_only | subagent_only`)를 가진 Markdown 파일이며, 로더가 `skills/` 하위의 모든 `SKILL.md`를 자동으로 발견합니다
- **내장 스킬**([skills/builtin/](skills/builtin/)): `cron`, `heartbeat`, `clawhub`(GitHub 스킬 설치기), `skill_creator`(새 스킬 생성), `image_to_text`, `speech_to_text`, `video_text_to_text`, `text_to_image`, `multimodal_rag`, `code_wiki`, `llm_wiki`
- **스킬 관리 도구**: 에이전트가 런타임에 스킬을 나열, 조회, 관리할 수 있습니다. 서드파티 업로드 스킬(`skills/plugins/`)은 명시적으로 활성화될 때까지 비활성 상태로 유지됩니다
- **SkillSpector 보안 스캔**([server/service/skill_scanner.py](server/service/skill_scanner.py)): 서드파티 스킬은 활성화 전에 NVIDIA SkillSpector로 스캔됩니다(정적 YARA/룰 분석 + auxiliary LLM을 통한 선택적 LLM 시맨틱 분석). 플래그가 지정된 스킬은 설치가 차단됩니다
- **스킬 큐레이터**: context engine의 curator 스레드가 `skills/auto/` 하위의 자동 학습 스킬을 관리 — 자세한 내용은 [Experience README](docs/experience/README.ko.md)
- **도구 타임아웃**: 도구 호출은 `TOOL_CALL_TIMEOUT_MINUTES`(기본값 5)로 제한되어 교착 상태를 방지
- ▶️ _미들웨어 파이프라인(가드레일, 반복 예산, HITL, 정규화, 요약, 멀티모달 처리)은 [Middlewares README](agent/middlewares/README.md) 참조_

### 3. 🤖 멀티레벨 서브에이전트 시스템
- **7개 런타임 도구**: `sessions_spawn`, `sessions_yield`, `sessions_send`, `sessions_kill`, `sessions_steer`, `agents_list`, `subagents_list`
- **계층적 역할**: 깊이 제한 중첩(기본 최대 2단계, 하드 상한 2), MAIN → ORCHESTRATOR → LEAF 역할과 최소 권한 도구 스코프
- **컨텍스트 모드**: ISOLATED(새 컨텍스트) 또는 FORK(부모 트랜스크립트 복사), 파일 첨부 지원
- **신뢰성 있는 전달**: 결과는 멱등성 검사와 지수 백오프 재시도를 갖춘 EventBus announce 파이프라인을 통해 반환
- **영속 레지스트리**: 실행 기록을 SQLite에 저장하고, sweeper가 고아 실행을 복구하며, followup 체커는 런 타임아웃이 설정된 경우에만 이를 강제 (기본값: 없음)
- **Swarm 모드**: FIFO 스케줄링과 설정 가능한 동시성으로 배치 서브태스크 실행
- ▶️ _전체 아키텍처는 [Subagent System README](agent/tools/subagent/README.md) 참조_

### 4. 🌐 멀티채널 접근
- **Robyn 백엔드**([server/](server/)): 비동기 HTTP API + WebSocket(`/sessions/ws`), `127.0.0.1:8080`에서 리슨하며 업로드된 미디어를 `/static`, `/images`, `/audio`, `/video`로 제공
- **데스크톱 클라이언트**([client/](client/)): Tauri 2 + Nuxt 4(Vue 3 + TypeScript) SPA로, 시스템 트레이, 전역 단축키, 오프라인 히스토리 캐시(Dexie/IndexedDB), 다크/라이트 모드, i18n 지원
- **QQ 봇**: 플러그인 시스템을 통한 QQ 채널 어댑터([plugins/channels/qq/](plugins/channels/qq/))
- **메시지 버스**([bus/core.py](bus/core.py)): 내부 비동기 큐가 채널과 에이전트 코어를 분리

### 5. 👁️ 멀티모달 인터랙션
- **이미지 이해(ITTT)**: Image-to-Text 비전 모델로 사용자가 업로드한 이미지 분석
- **비디오 이해(VTTT)**: Video-Text-to-Text 모델로 비디오 콘텐츠 분석
- **음성 인식(STT)**: FunASR 기반 로컬 음성 인식
- **Text-to-Image(TTI)**: `text_to_image` 스킬로 텍스트 설명에서 이미지 생성
- **문서 파싱**: 지식 그래프 RAG 파이프라인을 위한 MinerU 기반 멀티모달 문서 수집

### 6. ⏰ 예약 및 능동적 행동
- **Cron 서비스**([skills/builtin/core/cron/](skills/builtin/core/cron/scripts/README.md)): 1회성(`at`), 간격(`every`), cron 표현식(`cron`, croniter + 타임존 기반) 에이전트 작업을 JSON 작업 스토어에 영구 저장하고, 작업별 실행 이력과 채널 전달 지원
- **Heartbeat 서비스**([skills/builtin/core/heartbeat/](skills/builtin/core/heartbeat/README.md)): 주기적 웨이크업(기본 30분)으로 `HEARTBEAT.md`의 미완료 작업을 확인하고, LLM이 skip/run을 판단하며, 결과는 알림 게이트를 통과

---

## 🏗️ 기술 스택

**Python 3.13**(의존성 관리는 [uv](https://docs.astral.sh/uv/) 사용) 기반이며, 다음 핵심 기술을 사용합니다:

| 모듈 | 기술 |
| :----- | :--------- |
| **에이전트 프레임워크** | LangChain 1.3+(`create_agent` + 미들웨어), LangGraph 컴파일 그래프 |
| **체크포인팅** | langgraph-checkpoint-sqlite(스레드 세이프 비동기 SQLite 세이버) |
| **웹 서버** | Robyn(HTTP + WebSocket + 정적 호스팅) |
| **데이터베이스** | aiosqlite 기반 SQLite(FTS5 전문 검색, WAL 모드) |
| **그래프 RAG** | 벤더드 LightRAG + RAG-Anything(multimodal_rag 스킬), `snkv[vector]` 스토리지 |
| **로컬 추론** | llama-cpp-python(GGUF: bge-m3 embedding, bge-reranker-v2-m3 reranker, auxiliary/ITTT/VTTT 모델), FunASR(STT) |
| **문서 파싱** | mineru-vl-utils |
| **웹 검색** | langchain-tavily(Tavily API) |
| **LLM 프로바이더** | langchain-openai, langchain-deepseek, langchain-community + 20개 이상 프로바이더 레지스트리(OpenAI, Anthropic, DeepSeek, Zhipu GLM, DashScope Qwen, Gemini, Moonshot Kimi, MiniMax, Groq, OpenRouter, SiliconFlow, Volcengine, Azure OpenAI, Ollama, vLLM 등) |
| **구조화 출력** | instructor, json_repair |
| **평가(Evaluation)** | RAGAS(그래프 RAG 품질 지표) + 자체 샌드박스 평가 프레임워크(`evals/`) |
| **MCP** | langchain-mcp-adapters(`plugins/mcp_server/`에서 서버 구성) |
| **작업 스케줄링** | croniter, asyncio |
| **비동기 메시징** | asyncio 큐(MessageBus, EventBus) |
| **미디어 처리** | OpenCV(headless), Pillow, websockets / websocket-client |
| **데스크톱 클라이언트** | Tauri 2 + Nuxt 4(Vue 3, TypeScript, pnpm) |
| **로깅** | loguru(선택적 LangSmith 트레이싱) |

---

## 📂 프로젝트 구조

```text
EMA_AI_agent/
├── agent/                  # 에이전트 코어 로직
│   ├── core.py             # 메인 에이전트 루프(LangChain create_agent → LangGraph 그래프)
│   ├── wrapper/            # 그래프 레벨 래퍼(반복 가드, 컨텍스트 한도)
│   ├── checkpointer/       # 스레드 세이프 비동기 SQLite 체크포인터
│   ├── middlewares/        # 미들웨어 파이프라인(요약, 가드레일, HITL 등)
│   └── tools/              # 에이전트가 사용하는 도구
│       ├── subagent/       # 멀티레벨 서브에이전트 시스템(spawn/registry/swarm 등)
│       ├── todolist/       # 세션 범위 todo 계획 레이어
│       │   └── knowledge/  # Plan 지식 베이스 + `knowledge` 도구
│       ├── file_tools/     # 파일 I/O 도구(읽기, 쓰기, 패치, 검색)
│       ├── skill_tools/    # 스킬 관리 도구(나열, 조회, 관리)
│       ├── pub_base/       # 공유 도구 유틸리티 및 기반
│       ├── mcp_plugin.py   # MCP 도구 통합
│       ├── web_search.py   # 웹 검색 도구(Tavily)
│       ├── python_repl.py  # Python 코드 실행
│       ├── terminal.py     # 터미널 명령 실행
│       ├── memory.py       # 메모리 확인 도구
│       ├── question.py     # HITL 다지선다 질문 도구
│       └── message_search.py # 대화 FTS5 검색 도구
│
├── bus/                    # 메시지 버스(비동기 큐)
│   └── core.py             # MessageBus — 인바운드/아웃바운드 큐
│
├── channels/               # 채널 인터페이스 정의
│   ├── base.py             # 추상 채널 기반 클래스
│   ├── manager.py          # 채널 라이프사이클 매니저
│   └── registry.py         # 채널 등록
│
├── client/                 # 데스크톱 클라이언트(Tauri 2 + Nuxt 4, pnpm)
│   ├── app/                # Nuxt 4 SPA 소스(Vue 3)
│   ├── src-tauri/          # Tauri 2 네이티브 셸(Rust)
│   └── README.md           # 클라이언트 문서
│
├── config/                 # 중앙화된 설정(paths, feature TypedDicts, schema, settings)
│   ├── __init__.py         # API 호스트/포트(127.0.0.1:8080)
│   ├── path.py             # 파일 경로 설정
│   ├── schema.py           # 설정 스키마 모델
│   ├── sherry_settings.py  # sherry.jsonc 로더
│   └── features/           # 객체별 feature TypedDict와 기본 인스턴스
│
├── context_engine/         # 메모리 엔진(MesMemory)
│   ├── core.py             # 히스토리 조회 및 FTS5 검색 API
│   ├── store/              # 세션 메시지 스토어(SQLite + FTS5, WAL)
│   ├── events/             # append-only 이벤트 로그 + projector
│   ├── embeddings/         # 벡터 시맨틱 검색(indexer / search)
│   └── curator/            # 자동 스킬 큐레이션
│
├── docs/                   # 서브시스템 설계 문서(언어별 README)
│   ├── experience/         # 경험 추출 경로 + 스킬 큐레이션
│   ├── session_memory/     # 세션 메모리 역량
│   ├── summarization/      # 압축 트리거 및 쿨다운
│   ├── loop-prevention/    # 폭주 루프 방지 하네스
│   ├── sandbox/            # 평가 샌드박스 및 도구 격리
│   ├── token-guard/        # 128K 컨텍스트 윈도우 하한
│   ├── context-governance/ # 영속화, 축출, 테일 클립, 요약 필터링
│   └── long-running-tasks/ # TaskFlow 오케스트레이션
│
├── evals/                  # 평가 프레임워크(dispatcher + 5개 스위트)
│   ├── evals.py            # 스위트 러너: uv run python evals/evals.py [suite]
│   ├── sandbox.py          # 실행 중 저장소 쓰기를 리디렉션하는 샌드박스
│   ├── graph_rag/          # 그래프 RAG 파이프라인의 RAGAS 지표
│   ├── subagent/           # 서브에이전트 스폰 파이프라인 벤치마크
│   ├── long_running_task/  # TaskFlow DAG 평가
│   ├── session_memory/     # 세션 메모리 스택 점검
│   ├── nudge_extraction/   # AI 판정 plan 추출
│   └── results/            # 실행별 리포트(gitignore됨)
│
├── logs/                   # 로깅 시스템
│   ├── logger.py           # 로그 설정(loguru)
│   └── output/             # 로그 출력 디렉터리
│
├── models/                 # 모델 래퍼 및 가중치
│   ├── LLMs/               # LLM 설정(main_llm.py, reasoner_llm.py, auxiliary_llm/, reasoning_* 프로바이더)
│   ├── ITTT_model/         # Image-to-Text 모델(클라우드 API 또는 로컬 GGUF)
│   ├── VTTT_model/         # Video-Text-to-Text 모델(클라우드 API 또는 로컬 GGUF)
│   ├── STT_model/          # Speech-to-Text 모델(FunASR)
│   ├── embed_model/        # 임베딩 모델(로컬 bge-m3 GGUF 또는 클라우드 API)
│   ├── reranker_model/     # 크로스 인코더 리랭커(로컬 GGUF 또는 클라우드 API)
│   ├── extract_model/      # 엔티티 추출 모델(서드파티 가중치)
│   └── providers/          # LLM 프로바이더 사양 및 레지스트리
│       └── registry.py    # 20개 이상 프로바이더의 ProviderSpec
│
├── plugins/                # 플러그인 시스템
│   ├── channels/           # 채널 플러그인(QQ 봇 어댑터)
│   └── mcp_server/         # MCP 서버 설정
│
├── pub/                    # 공용 유틸리티 및 데이터 모델
│   ├── func/               # 공용 유틸리티 함수
│   │   ├── format/         # 텍스트 포맷 유틸리티
│   │   ├── media/          # 미디어 처리 유틸리티
│   │   ├── message/        # 메시지 처리 유틸리티
│   │   └── validator/      # 입력 검증 유틸리티
│   └── types/              # 공유 데이터 모델
│       ├── message.py      # MultiModalMessage, Chat 등
│       ├── bus.py          # 메시지 버스 데이터 모델
│       └── client.py       # 클라이언트 데이터 모델
│
├── runtime/                # 런타임 상태 및 유틸리티
│   ├── session/            # 세션 단위 레지스트리
│   │   ├── core.py         # 싱글톤 SessionRegister 기반 + 세션별 정리
│   │   ├── relation_register.py # 세션/socket 관계 레지스트리
│   │   ├── state_register.py   # 상태 레지스트리
│   │   ├── count_call_register.py # 사용량/통계 카운터
│   │   ├── timer_call_register.py # 타이머 레지스트리
│   │   └── _callback_executor.py # 비동기 콜백 실행기
│   └── process/            # 프로세스 단위 서비스
│       ├── crash_loop_breaker.py # 부팅 크래시 루프 감지
│       └── periodic_backoff.py   # 주기적 백오프 상태
│
├── server/                 # Robyn 백엔드 서비스
│   ├── __main__.py         # 서버 엔트리포인트(python -m server)
│   ├── DAO/                # 데이터 접근 객체
│   ├── service/            # 비즈니스 로직 서비스(skill_scanner.py 포함)
│   └── trigger/            # 라우트 및 핸들러 등록
│       ├── http/           # HTTP 엔드포인트 트리거
│       ├── ws/             # WebSocket 트리거
│       ├── channels/       # 채널 수신 트리거
│       └── subagent/       # 서브에이전트 결과 트리거
│
├── skills/                 # 스킬 라이브러리(SKILL.md 정의 파일)
│   ├── loader.py           # 스킬 자동 발견 및 등록
│   ├── skills_snapshot.py  # 스킬 프롬프트 스냅샷 구축
│   ├── auto/               # 자동 학습 스킬(curator 관리)
│   ├── plugins/            # 서드파티 업로드 스킬(기본 비활성)
│   └── builtin/            # 내장 스킬
│       ├── core/           # cron, heartbeat, clawhub, skill_creator, image_to_text,
│       │                   # speech_to_text, video_text_to_text, multimodal_rag
│       ├── text_to_image/  # Text-to-image 스킬
│       ├── code_wiki/      # 코드베이스 wiki 생성 스킬
│       └── llm_wiki/       # Markdown 지식베이스 스킬
│
├── src/                    # 런타임 데이터 디렉터리
│   ├── checkpoints/        # 세션 체크포인트
│   ├── data/               # 데이터 저장소
│   ├── store/              # 데이터 스토어
│   ├── rag/                # RAG 인덱스 출력
│   └── images/ audio/ video/ # 업로드 미디어(정적 서빙)
│
├── temp/                   # 임시 파일
│
├── tests/                  # 테스트 스위트(pytest)
│
├── workspace/              # 캐릭터 프로파일 및 행동 정의
│   ├── SOUL.md             # 성격 대비, 말투
│   ├── AGENTS.md           # 도구 사용 우선순위, 안전 경계
│   ├── USER.md             # 사용자별 상호작용 선호
│   ├── HEARTBEAT.md        # heartbeat 서비스의 미완료 작업
│   ├── prompt_builder.py   # 프로파일 → 프롬프트 빌더
│   ├── file_sync.py        # 워크스페이스 템플릿 지연 동기화(언어별)
│   ├── template/           # 페르소나 템플릿(en / zh / ja / ko)
│   └── memory/             # 장기 기억 저장소
│
├── .env.example            # 환경 변수 템플릿
├── pyproject.toml          # Python 의존성(uv 관리)
├── uv.lock                 # uv 락파일
├── start.sh                # 백엔드 시작 스크립트
└── cron_jobs.json          # Cron 작업 스케줄 데이터
```

---

## 📚 서브모듈 문서

각 주요 서브시스템에는 상세 README가 있습니다:

| 서브모듈 | 설명 | 문서 |
|-----------|-------------|---------------|
| **Context Engine** | 단기 세션 메시지 메모리(MesMemory) | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) · [JA](context_engine/README.ja.md) · [KO](context_engine/README.ko.md) |
| **경험 체계** | 대화 이력을 재사용 가능한 경험으로 축적하는 네 개 추출 경로와 `skills/auto/`를 관리하는 Curator | [EN](docs/experience/README.md) · [ZH](docs/experience/README.zh.md) · [JA](docs/experience/README.ja.md) · [KO](docs/experience/README.ko.md) |
| **세션 메모리** | SESSION 계획 역량: memory flush, 압축 쿨다운, compaction lock, 이벤트 로그, 시맨틱 검색 | [EN](docs/session_memory/README.md) · [ZH](docs/session_memory/README.zh.md) · [JA](docs/session_memory/README.ja.md) · [KO](docs/session_memory/README.ko.md) |
| **서브에이전트 시스템** | 멀티레벨 서브에이전트 스폰, 병렬 실행 및 결과 전달 | [EN](agent/tools/subagent/README.md) · [ZH](agent/tools/subagent/README.zh.md) · [JA](agent/tools/subagent/README.ja.md) · [KO](agent/tools/subagent/README.ko.md) |
| **미들웨어** | 에이전트 라이프사이클 미들웨어 파이프라인 | [EN](agent/middlewares/README.md) · [ZH](agent/middlewares/README.zh.md) · [JA](agent/middlewares/README.ja.md) · [KO](agent/middlewares/README.ko.md) |
| **채널** | 채널 인터페이스 및 어댑터 시스템 | [EN](channels/README.md) · [ZH](channels/README.zh.md) · [JA](channels/README.ja.md) · [KO](channels/README.ko.md) |
| **데스크톱 클라이언트** | Tauri 2 + Nuxt 4 데스크톱/모바일 SPA 클라이언트 | [EN](client/README.md) · [ZH](client/README.zh.md) · [JA](client/README.ja.md) · [KO](client/README.ko.md) |
| **Cron 서비스** | 예약/주기적 에이전트 작업 실행 | [EN](skills/builtin/core/cron/scripts/README.md) · [ZH](skills/builtin/core/cron/scripts/README.zh.md) · [JA](skills/builtin/core/cron/scripts/README.ja.md) · [KO](skills/builtin/core/cron/scripts/README.ko.md) |
| **Heartbeat 서비스** | 주기적 웨이크업 작업 확인 | [EN](skills/builtin/core/heartbeat/README.md) · [ZH](skills/builtin/core/heartbeat/README.zh.md) · [JA](skills/builtin/core/heartbeat/README.ja.md) · [KO](skills/builtin/core/heartbeat/README.ko.md) |
| **요약 압축** | 컨텍스트 압축 미들웨어: 5개 트리거 지점, 4경로 오버플로 라우터, 스래싱 방지 가드 | [EN](docs/summarization/README.md) · [ZH](docs/summarization/README.zh.md) · [JA](docs/summarization/README.ja.md) · [KO](docs/summarization/README.ko.md) |
| **루프 방지** | 폭주 루프 가드, 지수 백오프 브레이커, 프로세스 크래시 게이팅 | [EN](docs/loop-prevention/README.md) · [ZH](docs/loop-prevention/README.zh.md) · [JA](docs/loop-prevention/README.ja.md) · [KO](docs/loop-prevention/README.ko.md) |
| **샌드박스** | 터미널 및 Python REPL 격리: 환경 변수 스크러빙, OS 네이티브 격리, 승인 게이트 | [EN](docs/sandbox/README.md) · [ZH](docs/sandbox/README.zh.md) · [JA](docs/sandbox/README.ja.md) · [KO](docs/sandbox/README.ko.md) |
| **장기 실행 작업** | TaskFlow DAG 엔진, 토큰 예산, 데드라인, 턴 간 메모리 연속성 | [EN](docs/long-running-tasks/README.md) · [ZH](docs/long-running-tasks/README.zh.md) · [JA](docs/long-running-tasks/README.ja.md) · [KO](docs/long-running-tasks/README.ko.md) |
| **Token Guard** | 두 LLM의 128K 컨텍스트 윈도우 하한(부팅, 빌드, 스폰, env 쓰기) | [EN](docs/token-guard/README.md) · [ZH](docs/token-guard/README.zh.md) · [JA](docs/token-guard/README.ja.md) · [KO](docs/token-guard/README.ko.md) |
| **Context Governance** | 경계별 영속화, 도구 결과·인간 메시지 축출, `read_file` 슬라이스, 오버플로 테일 클립, 요약 필터링 | [EN](docs/context-governance/README.md) · [ZH](docs/context-governance/README.zh.md) · [JA](docs/context-governance/README.ja.md) · [KO](docs/context-governance/README.ko.md) |

## ⚡ 빠른 시작

### 1. 사전 요구 사항
- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** — 의존성 매니저. `.venv`를 자동으로 생성하고 관리하므로 가상환경을 직접 만들 필요가 없습니다.

```bash
git clone <your-repo-url>
cd EMA_AI_agent
uv sync   # .venv를 생성하고 uv.lock 기준으로 의존성을 정확히 설치
```

### 2. 환경 변수 설정
`.env` 예시 파일을 복사하고, 최소한 메인 채팅 모델과 Tavily 키를 채워 넣으세요:

```bash
cp .env.example .env
```

| 변수 | 필수 | 설명 |
| :------- | :------- | :---------- |
| `MAIN_LLM_PROVIDER` / `MAIN_LLM_NAME` / `MAIN_LLM_API_BASE` / `MAIN_LLM_API_KEY` / `MAIN_LLM_MAX_TOKEN` | ✅ | 메인 채팅 모델(JSON 출력 및 도구 호출 지원 필요); `MAIN_LLM_MAX_TOKEN`은 131072 (128K) 이상이어야 합니다 |
| `MAIN_LLM_ENABLE_THINKING` / `MAIN_LLM_REASONING_EFFORT` | — | 범용 추론 스위치. 프로바이더별로 매핑됨(DeepSeek / OpenAI / GLM / Anthropic) |
| `TAVILY_API_KEY` | 웹 검색 사용 시 ✅ | 웹 검색 도구 활성화 |
| `REASONER_LLM_*` | — | 사고 연쇄(Chain-of-thought) 추론 모델 |
| `AUXILIARY_LLM_*` | — | 요약/단순 작업용 경량 모델(기본값은 클라우드 API. `AUXILIARY_LLM_MODEL_LOCAL=true`로 로컬 GGUF 모델 사용); `AUXILIARY_LLM_MAX_TOKEN`은 131072 (128K) 이상이어야 합니다 |
| `ITTT_*` / `VTTT_*` / `TTI_*` / `STT_*` | — | 이미지 / 비디오 / 이미지 생성 / 음성 모델 설정 |
| `RERANKER_*` / `EMBEDDING_*` | — | 검색용 리랭커 및 임베딩(아래 모델 참고 사항 확인) |
| `SKILL_SCANNER_ENABLED` / `SKILL_SCANNER_LLM` | — | SkillSpector 보안 스캐너 스위치(기본값 켜짐) |
| `TOOL_CALL_TIMEOUT_MINUTES` / `LOG_LEVEL` | — | 도구 타임아웃(5분) 및 로그 레벨(INFO) |
| `WORKSPACE_TEMPLATE_LANG` | — | 페르소나 템플릿 언어: `en` / `zh` / `ja` / `ko`(첫 사용 시 지연 복사) |
| `"LANGSMITH"` (sherry.jsonc) | — | 선택적 LangSmith 트레이싱 |

### 3. 모델 참고 사항 (HuggingFace 자동 다운로드)
**로컬 GGUF** 모드로 구성된 모델은 첫 사용 시 Hugging Face에서 `models/<model>/model_weight/`로 자동 다운로드됩니다 — 수동 다운로드가 필요 없습니다:

- **임베딩**: `EMBEDDING_MODEL_LOCAL=true`(기본값)이면 로컬 `bge-m3` Q8_0 GGUF를 첫 실행 시 자동 다운로드합니다.
- **리랭커**: `.env` 템플릿의 기본값은 **클라우드 API**(`RERANKER_MODEL_LOCAL=false`, OpenAI 호환 `bge-reranker-v2-m3`)입니다. `true`로 설정하면 로컬 GGUF 리랭커(자동 다운로드, 약 636 MB)로 전환됩니다.
- **ITTT / VTTT / Auxiliary LLM**: 템플릿 기본값은 클라우드 API이며, `*_MODEL_LOCAL=true`로 설정하면 로컬 GGUF 모델로 전환됩니다(역시 자동 다운로드).

> 첫 다운로드 시 huggingface.co 접근이 필요합니다(중국 본토 사용자는 프록시나 미러가 필요할 수 있습니다). 중단된 다운로드는 다음 시작 시 재개되며, `models/<model>/model_weight/`를 삭제하면 강제로 다시 다운로드합니다.

### 4. 백엔드 시작
`start.sh`는 uv가 관리하는 `.venv`를 활성화하고 Robyn 백엔드를 실행합니다(Ollama나 프런트엔드를 시작하지 않습니다):

```bash
chmod +x start.sh
./start.sh          # .venv 인터프리터로 python -m server --fast --disable-openapi 실행
```

수동 시작(동일):

```bash
uv run python -m server
```

백엔드는 **http://127.0.0.1:8080** 에서 리슨하며 WebSocket 엔드포인트는 `/sessions/ws`입니다.

### 5. (선택) 데스크톱 클라이언트
Tauri 2 + Nuxt 4 클라이언트는 [client/](client/)에 있으며 Node.js 18+, pnpm, Rust가 필요합니다:

```bash
cd client
pnpm install
pnpm dev          # 브라우저 모드, 개발 서버 http://localhost:3000
pnpm tauri dev    # 네이티브 데스크톱 모드
```

클라이언트는 기본적으로 `http://127.0.0.1:8080`의 Python 백엔드에 연결됩니다(`client/.env`의 `VITE_API_BACK_URL`로 설정 가능). 자세한 내용은 [클라이언트 README](client/README.md)를 참조하세요.

---

## 🧪 테스트

테스트는 `tests/` 아래에 **소스 트리를 미러링**하며(`tests/agent/...`, `tests/server/...`, `tests/context_engine/...`), uv를 통해 **pytest**로 실행합니다(단일 테스트 파일이나 소규모 선택은 `uv run pytest`). 모든 테스트 파일은 모듈 수준 `pytestmark`(`unit` / `integration` / `module` / `system` / `regression`)를 가지며, 이것이 어느 runner 그룹에서 실행될지 결정합니다.

### 권장: 프로세스 격리 runner

전체 스위트(및 CI)에는 split runner를 사용하세요. 이 runner는 **세 개의 순차 pytest 프로세스**(결코 병렬 아님)로 MARKER(디렉터리가 아님) 기준으로 테스트를 선택하고, 종료 코드를 집계하며, 그룹별 요약과 최종 판정(모든 그룹 통과 시에만 종료 코드 0)을 출력합니다:

```bash
uv run python tests/run_tests_split.py                  # 허메틱 스위트(기본, llm_e2e 제외)
uv run python tests/run_tests_split.py --with-llm-e2e   # 실제 LLM e2e 테스트만(전용 job 모드)
uv run python tests/run_tests_split.py -- -k spawn -q   # `--` 뒤 인자는 pytest로 전달
```

| 그룹 | Marker | 내용 |
| :---- | :----- | :------- |
| **A** | `unit` | 순수 로직, 완전 mock 테스트 |
| **B** | `integration or module or system` | 허메틱 통합 / 모듈 / 시스템 테스트 |
| **C** | `regression` | 크로스 모듈 회귀 테스트 |

**왜 프로세스를 분리할까?** `tests/agent/tools/subagent/conftest.py`는 conftest *임포트* 시점에 stub callable을 프로세스 전역 `sys.modules`에 설치합니다. 단일 프로세스 전체 스위트 실행에서 pytest는 수집 단계(테스트 실행 전)에 모든 conftest와 테스트 모듈을 임포트하므로, 그 stub이 프로세스 전체에서 살아남아 스위트를 넘어 누출됩니다. 지연(호출 시점) 임포트는 stub을 해석하지만, 더 일찍 실제 객체를 바인딩한 모듈은 오래된 바인딩을 유지합니다. 그 결과 subagent 테스트에서 멀리 떨어진 스위트에서 혼란스럽고 순서에 의존하는 실패가 나타납니다(예: 스킬 스코프 단언이 stub의 고정 스킬 목록을 보거나 `TypeError` 트레이스백이 conftest lambda를 지목). 그룹을 별도 프로세스로 실행하면 이런 크로스 스위트 오염이 구조적으로 불가능해집니다. (stub 자체는 `c730a46`부터 복원 안전합니다. runner는 심층 방어의 운영 계층입니다.)

**Windows 참고:** 자식 pytest 프로세스의 환경에 `PYTHONIOENCODING=utf-8`이 주입되고 runner가 `errors="replace"`로 출력을 캡처하므로, GBK 콘솔 코드페이지가 출력을 손상시키거나 실행을 크래시시키지 않습니다.

### 실제 LLM e2e 테스트(`llm_e2e` marker)

`tests/agent/tools/subagent/`의 두 테스트 파일(`test_real_e2e.py`, `test_spawn_direct_e2e.py`)에 있는 세 개의 테스트가 **실제 LLM API**를 호출합니다. 이들은:

- **기본적으로 선택 해제**되며(`-m "not llm_e2e"`, `pyproject.toml` addopts와 runner 양쪽에 설정),
- `@pytest.mark.timeout` 예산(pytest-timeout)으로 제한됩니다: 단순 테스트 300초, 동시 테스트 600초,
- 명시적으로, **전용 job**에서 실행합니다: `uv run python tests/run_tests_split.py --with-llm-e2e`(`-m llm_e2e` 선택) 또는 `uv run pytest -m llm_e2e`.

**예상 런타임**(단독, 실제 백엔드): 단순 작업 약 30–60초, 복잡한 최악의 경우 약 10분, 동시 작업 약 2–9분. 이 예산을 초과하면 정상적인 지연이 아니라 실제 hang이며, 테스트별 timeout이 이를 제한합니다(단순 300초 / 동시 600초).

**CI:** `.github/workflows/ci.yml`는 `main`에 대한 모든 push/PR에서 `uv run python tests/run_tests_split.py`를 실행하며, 스위트를 **세 개의 순차 pytest 프로세스**(결코 병렬 아님)로 수행합니다: A = `unit`, B = `integration` + `module` + `system`, C = `regression`. `--with-llm-e2e`는 별도의 더 느린 job으로 유지하세요(API 토큰을 소모하며, 다른 스위트와 병렬로 절대 실행하지 마세요).

> **참고:** `tests/full/`은 위 표준 그룹 밖의 보조/실험 디렉터리입니다. 특히 `tests/full/test_main_agent_e2e.py`는 라이브 네트워크 테스트로 `llm_e2e` 태그가 **없으므로**, 태그를 달기 전에는 CI에 연결하지 마세요.

### Evals

`evals/`는 pytest와 나란히 있는 자체 제작 샌드박스 평가 프레임워크입니다. 등록된 모든 스위트를 실행하거나 이름으로 하나만 실행할 수 있습니다:

```bash
uv run python evals/evals.py                # 등록된 모든 스위트
uv run python evals/evals.py graph_rag      # 이름으로 단일 스위트 실행
```

| 스위트 | 평가 내용 |
| :---- | :---------------- |
| `graph_rag` | multimodal_rag 파이프라인을 RAGAS로 채점(faithfulness, answer relevancy, context recall, context precision) |
| `subagent` | 실제 `spawn_subagent_direct` 파이프라인의 결정적 작업 벤치(작업 성공률 + 지연) |
| `long_running_task` | 의존성 DAG에 대한 TaskFlow 오케스트레이션 루프(스텝 성공률, 흐름 완료, wall time) |
| `session_memory` | 세션 메모리 스택 6개 점검: 쿨다운, compaction lock, 체크포인트 복원, 멱등 재생, 컨텍스트 적격성, 시맨틱 검색 랭킹 |
| `nudge_extraction` | plan 추출 과정을 auxiliary LLM이 grounded, 재사용 가능, 비일반적 스킬인지 판정 |

모든 스위트는 `evals/sandbox.py` 안에서 실행되며(저장소 쓰기가 임시 샌드박스로 리디렉션됨), 리포트를 `evals/results/<suite>/<run_id>/`에 기록합니다. 이 디렉터리는 **gitignore**되어 있어 실행별 리포트는 절대 커밋되지 않습니다.

---

## 📝 캐릭터 프로파일 예시

에이전트의 행동은 `workspace/` 아래의 파일들에 의해 결정됩니다:

- **SOUL.md**: 성격 대비, 말투, 행동 논리를 정의합니다.
- **AGENTS.md**: 도구 사용 우선순위, 안전 경계, 윤리 지침을 정의합니다.
- **USER.md**: 사용자별 상호작용 선호도와 알려진 정보를 저장합니다.
- **HEARTBEAT.md**: heartbeat 예약 서비스의 미완료 작업을 나열합니다.
- **prompt_builder.py**: 프로파일 파일로부터 시스템 프롬프트를 만듭니다.
- **file_sync.py**: 누락된 페르소나 파일을 `workspace/template/<lang>/`(`WORKSPACE_TEMPLATE_LANG`으로 선택)에서 지연 복사하며, 사용자의 수정을 절대 덮어쓰지 않습니다.

---

## 🤝 기여하기

Issue와 Pull Request를 환영합니다! 새 스킬을 추가하는 방법:

1. `skills/` 아래에 폴더를 생성합니다(서드파티 스킬은 `skills/plugins/`).
2. YAML 프론트매터(`name`, `description`, 선택적 `scope`)가 있는 `SKILL.md`에 스킬의 사용법과 절차를 작성합니다.
3. 에이전트를 재시작하세요 — 로더가 모든 `SKILL.md`를 자동으로 발견하여 모델에 노출합니다.(실행 중인 에이전트에게 내장 `skill_creator` 스킬로 생성해 달라고 요청할 수도 있습니다.)

`skills/plugins/` 아래의 서드파티 스킬은 SkillSpector로 스캔되며, 명시적으로 활성화될 때까지 비활성 상태로 유지됩니다.

---

연락처: QQ 3132225629

## 📄 라이선스

이 프로젝트는 MIT 라이선스에 따라 배포됩니다.

---

> **💡 팁**: 이 프로젝트는 고도의 AI 에이전트와 딥 롤플레잉에 대한 탐구에서 영감을 받았습니다.

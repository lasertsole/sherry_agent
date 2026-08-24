# 🍊 EMA AI Agent - Sherry

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **LangGraph와 멀티모달 기술로 구축된 딥 역할극 AI 에이전트.**

## ✨ 소개

EMA AI Agent는 장기 기억과 복잡한 추론 능력을 갖춘 고도로 의인화된 AI 에이전트 시스템입니다. 단순한 챗봇을 넘어, 독립적인 **페르소나(Persona)**, 동적인 **기술 기억 그래프(Skill Memory Graph)**, 예약 작업과 백그라운드 서브에이전트를 통한 능동적 행동을 가진 가상 동반자입니다.

에이전트의 캐릭터인 **Sherry**는 친밀도에 따라 전환되는 이중 성격 대비(온화함/차가움)를 가진 탐정 소녀로, 시스템 전체는 세션 간 축적되는 기억을 통해 몰입감 있고 지속적인 역할극을 지원하도록 설계되었습니다.

---

## 🚀 핵심 기능

### 1. 🧠 딥 메모리 시스템 (컨텍스트 엔진 + 경험 그래프)
- **이중 메모리 아키텍처**: 단기 세션 메모리([MesMemory](context_engine/README.md)) + 장기 경험 지식 그래프
- **경험 그래프**: 작업 실행에서 신호가 강한 재사용 가능한 경험을 추출해 구조화된 노드와 엣지로 변환하는 증류 우선 지식 그래프
- **다중 역할 지식 베이스**: 메인 에이전트 + 커맨더가 공유하는 전략 수준 그래프(`default`), 워커용 작업 수준 그래프(`worker`)
- **이중 경로 회수**: 정밀(벡터/FTS5 → 커뮤니티 확장 → PPR) + 일반화(커뮤니티 벡터 → 대표자) 검색 및 재순위화
- **커뮤니티 감지 및 요약**: Leiden 알고리즘이 그래프를 분할하고 효율적인 장기 검색을 위한 요약을 생성
- **영구 저장**: SQLite + FTS5 + 벡터 임베딩, 세션 간 지식 상속 지원
- ▶️ _아키텍처, 데이터 모델 및 API 세부 정보는 [컨텍스트 엔진 README](context_engine/README.md) 및 경험 그래프 문서를 참조_

### 2. 🛠️ 동적 기술 시스템
- **SKILL.md 표준**: 표준화된 Markdown 형식으로 정의된 기술 — 에이전트가 능동적을 읽고 새로운 능력을 학습
- **도구 호출**: 내장 웹 검색, 파일 I/O, 코드 실행(Python Repl), 터미널 명령, 메시지 검색 등
- **서브에이전트**: 백그라운드에서 복잡한 시간 소모 작업을 병렬로 실행 지원, 메시지 버스를 통한 비동기 결과
- **경험 피드백 루프**: 서브에이전트가 초안 도구로 발견사항 기록 → 작업 완료 후 경험 증류 → 경험 그래프에 반영 → 향후 작업에 회수 및 주입
- ▶️ _수명 주기, 커맨더 아키텍처, 증류 파이프라인 및 API 문서는 [서브에이전트 시스템 README](agent/tools/subagent/README.md)를 참조_
- **도구 타임아웃**: Python REPL, 터미널, 웹 검색 도구는 각각 독립적인 구성 가능한 타임아웃을 가지며 만료 시 자동 종료
- ▶️ _미들웨어 파이프라인은 [미들웨어 README](agent/middlewares/README.md)를 참조_

### 3. 🌐 다중 채널 액세스
- **웹 UI**: Streamlit으로 구축된 현대적 채팅 인터페이스, 멀티모달 입력(이미지, 음성) 지원
- **차세대 클라이언트** ([client](client/)): Tauri 2 + Nuxt 4 데스크톱/모바일 SPA 클라이언트
- **QQ 봇**: 플러그인 시스템(`plugins/channels/`)을 통한 QQ 채널 어댑터
- **메시지 버스**: 내부 비동기 메시지 큐([MessageBus](bus/core.py))가 입출력 채널을 분리

### 4. 👁️ 멀티모달 상호작용
- **시각 이해**: 사용자가 업로드한 이미지를 인식하고 분석하는 Image-to-Text(VL) 모델 지원

### 5. ⏰ 예약 및 능동적 행동
- **크론 서비스** ([skills/builtin/core/cron/](skills/builtin/core/cron/scripts/README.md)): 주기적, 일회성 또는 크론 표현식 기반 에이전트 작업 예약
- **하트비트 서비스** ([skills/builtin/core/heartbeat/](skills/builtin/core/heartbeat/README.md)): HEARTBEAT.md를 주기적으로 확인하고 유휴 시간에 보류 작업을 자동 실행하는 주기적 웨이크업

---

## 🏗️ 기술 스택

**Python 3.13** 기반, 핵심 기술:

| 모듈 | 기술 |
| :----- | :--------- |
| **에이전트 프레임워크** | LangChain 1.3+, langchain-classic, LangGraph |
| **벡터 및 검색** | FAISS, LightRAG, Sentence Transformers, BGE/BAAI 임베딩 시리즈 |
| **데이터베이스** | SQLite (FTS5 전문 검색), LanceDB |
| **그래프 알고리즘** | igraph + Leiden Algorithm (커뮤니티 감지), PageRank |
| **웹 서버** | Robyn + FastAPI (이중 비동기 서버) |
| **프런트엔드 UI** | Streamlit, Tauri 2 + Nuxt 4 (차세대 클라이언트) |
| **LLM 지원** | DeepSeek, OpenAI, Ollama (로컬 모델), langchain-deepseek |
| **작업 예약** | croniter, asyncio |
| **비동기 메시징** | asyncio.Queue (MessageBus) |

---

## 📂 프로젝트 구조

```text
EMA_AI_agent/
├── agent/                  # 에이전트 핵심 로직 및 미들웨어
│   ├── core.py             # 메인 에이전트 루프 (LangGraph 컴파일 그래프)
│   ├── checkpointer/       # 세션 상태 체크포인팅
│   ├── codeact/            # CodeAct 에이전트 (코드 인터랙티브 실행)
│   │   ├── core.py         # CodeAct 루프 및 도구 오케스트레이션
│   │   └── utils.py        # CodeAct 유틸리티
│   ├── middlewares/        # 미들웨어 파이프라인
│   │   ├── summarization.py         # 대화 요약
│   │   ├── tool_call_normalize.py   # 도구 호출 정규화 및 라우팅
│   │   ├── tool_guardrails.py       # 도구 안전 가드레일
│   │   ├── iteration_budget.py      # 턴 예산 제한기
│   │   ├── multimodal_processor.py  # 비전 입력 처리
│   │   ├── heartbeat_staleness.py   # 하트비트 신선도 확인
│   │   └── context_engine/          # 컨텍스트 엔진 훅
│   ├── tools/              # 에이전트 액세스 가능 도구
│   │   ├── subagent/       # 서브에이전트 시스템 (계층적 작업 분해)
│   │   │   ├── base.py     # SubagentManager (싱글턴 오케스트레이터 + 증류)
│   │   │   ├── core.py     # Subagent spawn 도구 (@tool)
│   │   │   ├── draft.py    # 초안 도구 — 실행 중 핵심 발견 기록
│   │   │   ├── distiller.py # 작업 후 경험 증류
│   │   │   ├── commander/  # LangGraph 기반 커맨더 에이전트
│   │   │   ├── templates/  # 결과 공지 템플릿
│   │   │   └── type.py     # SubAgentOutput 데이터 모델
│   │   ├── file_tools/     # 파일 I/O 도구 (읽기, 쓰기, 패치, 검색)
│   │   ├── skill_tools/    # 기술 관리 도구 (목록, 보기, 관리)
│   │   ├── pub_base/       # 공유 도구 유틸리티 및 인프라
│   │   ├── mcp_plugin.py   # MCP 플러그인 도구
│   │   ├── web_search.py   # 웹 검색 도구
│   │   ├── python_repl.py  # Python 코드 실행 (타임아웃 있는 서브프로세스)
│   │   ├── terminal.py     # 터미널 명령 실행 (샌드박스, 타임아웃 포함)
│   │   ├── memory.py       # 메모리 검사 도구
│   │   └── message_search.py # 대화 검색 도구
│   └── utils/              # 에이전트 보조 유틸리티
│
├── bus/                    # 메시지 버스 (비동기 큐)
│   └── core.py             # MessageBus — 인바운드/아웃바운드 큐 및 이벤트
│
├── channels/               # 채널 인터페이스 정의
│   ├── base.py             # 추상 채널 베이스
│   ├── manager.py          # 채널 수명 주기 관리자
│   └── registry.py         # 채널 등록
│
├── client/                 # 차세대 클라이언트 (Tauri 2 + Nuxt 4)
│   ├── app/                # Nuxt 4 SPA 소스
│   │   ├── app.vue         # 루트 컴포넌트 엔트리
│   │   ├── pages/          # 페이지 컴포넌트
│   │   ├── layouts/        # 레이아웃 컴포넌트
│   │   ├── composables/    # Vue 3 컴포저블 로직
│   │   ├── assets/         # CSS 및 설정 에셋
│   │   ├── nuxt.config.ts  # Nuxt 4 설정
│   │   └── package.json    # 의존성 매니페스트
│   ├── src-tauri/          # Tauri 2 네이티브 셸 (Rust)
│   │   ├── src/            # Rust 소스
│   │   ├── Cargo.toml      # Rust 의존성
│   │   └── tauri.conf.json # Tauri 2 설정
│   └── README.md           # 영어 문서
│
├── config/                 # 중앙 집중식 설정
│   ├── path.py             # 파일 경로 설정
│   ├── schema.py           # 설정 스키마 모델
│   └── num.py              # 숫자/튜닝 파라미터
│
├── context_engine/         # 메모리 엔진
│   ├── core.py             # 메시지 검색 및 검색 API
│   └── store/              # 단기 세션 메시지 메모리 (SQLite + FTS5)
│
├── logs/                   # 로깅 시스템
│   ├── logger.py           # 로그 설정
│   └── output/             # 로그 출력 디렉터리
│
├── models/                 # 모델 래퍼
│   ├── LLMs/               # LLM 모델 설정
│   │   ├── auxiliary_llm/       # 경량 챗 모델
│   │   ├── main_llm.py         # 기본 챗 모델
│   │   ├── reasoner_llm.py     # 추론(reasoning) 모델
│   │   └── reasoning_normalizer.py # 프로바이더 간 reasoning_content 정규화
│   ├── VTTT_model.py       # 비디오-텍스트-텍스트 모델
│   ├── ITTT_model.py        # 이미지-텍스트 모델
│   ├── STT_model/          # 음성-텍스트 모델
│   ├── embed_model/        # 텍스트 임베딩 모델
│   ├── reranker_model/     # 크로스-인코더 재순위화 모델
│   └── extract_model/      # 엔티티 추출 모델
│
├── plugins/                # 플러그인 시스템
│   ├── channels/           # 채널 플러그인 (QQ 봇 등)
│   └── mcp_server/         # MCP 서버 설정
│
├── providers/              # LLM 프로바이더 사양 및 레지스트리
│   ├── registry.py         # 지원되는 모든 프로바이더의 ProviderSpec 항목
│   └── __init__.py         # 프로바이더 레지스트리 내보내기
│
├── pub_func/               # 공통 유틸리티 함수
│   ├── format/             # 텍스트 포맷 유틸리티
│   ├── media/              # 미디어 처리 유틸리티
│   ├── message/            # 메시지 처리 유틸리티
│   └── validator/          # 입력 검증 유틸리티
│
├── runtime/                # 런타임 상태 및 유틸리티
│   ├── core.py             # 핵심 런타임 수명 주기
│   ├── _callback_executor.py   # 비동기 콜백 실행기
│   ├── count_call_register.py   # 사용량/통계 카운터
│   ├── relation_register.py    # 관계/친밀도 추적
│   ├── state_register.py   # 상태 레지스트리
│   └── timer_call_register.py   # 타이머 레지스트리
│
├── server/                 # Robyn 백엔드 서비스 및 API 라우트
│   ├── __main__.py         # 서버 엔트리 포인트
│   ├── DAO/                # 데이터 액세스 객체
│   ├── service/            # 비즈니스 로직 서비스
│   └── trigger/            # 트리거 매니저
│       ├── core.py         # 트리거 매니저
│       ├── channels/       # 인바운드 채널 트리거
│       ├── http/           # HTTP 엔드포인트 트리거
│       └── subagent/       # 서브에이전트 결과 트리거
│
├── skills/                 # 기술 라이브러리 (SKILL.md 정의 파일)
│   ├── loader.py           # 기술 자동 발견 및 등록
│   ├── skills_snapshot.py  # 기술 프롬프트 스냅샷 빌드
│   ├── skills_snapshot.json # 캐시된 기술 프롬프트 스냅샷
│   ├── auto/               # 자동 학습 기술
│   ├── plugins/            # 플러그인 제공 기술
│   └── builtin/            # 내장 기술 구현
│       └── core/           # 핵심 내장 기술
│           ├── web_search/     # 웹 검색 및 스크레이프
│           ├── cron/           # 크론 예약 작업 기술
│           ├── heartbeat/      # 하트비트 주기 확인 기술
│           ├── image_to_text/  # 이미지 이해
│           ├── speech_to_text/ # 음성 인식
│           ├── video_text_to_text/ # 비디오 이해
│           ├── multimodal_rag/ # RAG 기반 지식 검색
│           ├── clawhub/        # GitHub 리포지토리 클로너
│           └── skill_creator/  # 새 기술 자동 생성
│
├── src/                    # 런타임 데이터 디렉터리
│   ├── checkpoints/        # 세션 체크포인트
│   ├── data/               # 데이터 저장소
│   ├── sessions/           # 세션 런타임 저장소
│   └── store/              # 데이터 스토어
│
├── static/                 # 정적 에셋
│   ├── avatar/             # 캐릭터 아바타 이미지
│   └── images/             # 기타 이미지
│
├── temp/                   # 임시 파일
│
├── tests/                  # 테스트 스위트
│
├── type/                   # 공유 데이터 모델
│   ├── message.py          # MultiModalMessage, Chat 등
│   ├── bus.py              # 메시지 버스 데이터 모델
│   └── client.py           # 클라이언트 데이터 모델
│
├── workspace/              # 캐릭터 프로필 및 동작 정의
│   ├── IDENTITY.md         # 이름, 나이, 관심사, 관계
│   ├── SOUL.md             # 성격 대비, 말투
│   ├── AGENTS.md           # 도구 사용 우선순위, 안전 경계
│   ├── USER.md             # 사용자별 상호작용 선호 및 알려진 사실
│   ├── HEARTBEAT.md        # 하트비트 서비스용 보류 작업
│   ├── character.json      # 캐릭터 설정
│   ├── prompt_builder.py   # 프로필-프롬프트 빌더
│   ├── template/           # 프롬프트 템플릿
│   └── memory/             # 장기 기억 저장소
│
├── .env                    # 환경 변수 (API 키, 모델 경로)
├── .env.example            # 환경 변수 템플릿
├── pyproject.toml          # Python 의존성 (uv 관리)
├── uv.lock                 # uv용 lockfile
├── start.sh                # 원클릭 시작 스크립트
├── introduce.md            # 프로젝트 소개 (EN)
├── introduce.zh.md         # 프로젝트 소개 (ZH)
├── TODOList.md             # 개발 로드맵 (EN)
├── TODOList.zh.md          # 개발 로드맵 (ZH)
└── cron_jobs.json          # 크론 작업 스케줄 데이터
```

---

## 📚 서브모듈 문서

각 주요 하위 시스템은 자체 상세 README가 있습니다:

| 서브모듈 | 설명 | 문서 |
|-----------|-------------|---------------|
| **컨텍스트 엔진** | 단기 세션 메시지 메모리 (MesMemory) | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) |
| **서브에이전트 시스템** | 계층적 작업 분해, 병렬 실행 및 경험 증류 | [EN](agent/tools/subagent/README.md) · [ZH](agent/tools/subagent/README.zh.md) |
| **미들웨어** | 에이전트 수명 주기 미들웨어 파이프라인 | [EN](agent/middlewares/README.md) · [ZH](agent/middlewares/README.zh.md) |
| **채널** | 채널 인터페이스 및 어댑터 시스템 | [EN](channels/README.md) · [ZH](channels/README.zh.md) |
| **차세대 클라이언트** | Tauri 2 + Nuxt 4 데스크톱/모바일 SPA 클라이언트 | [EN](client/README.md) · [ZH](client/README.zh.md) |
| **크론 서비스** | 예약/주기 에이전트 작업 실행 | [EN](skills/builtin/core/cron/scripts/README.md) · [ZH](skills/builtin/core/cron/scripts/README.zh.md) |
| **하트비트 서비스** | 주기적 웨이크업 작업 확인 | [EN](skills/builtin/core/heartbeat/README.md) · [ZH](skills/builtin/core/heartbeat/README.zh.md) |

---

## ⚡ 빠른 시작

### 1. 전제 조건
**Python 3.13+**가 설치되어 있는지 확인하세요.

```bash
git clone https://github.com/your-repo/EMA_AI_agent.git
cd EMA_AI_agent
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv sync
```

### 2. 모델 다운로드
첫 실행 시 시스템이 **Hugging Face**에서 자동으로 임베딩 모델과 리랭커 모델을 `models/embed_model` 및 `models/reranker_model`로 다운로드합니다. 참고:

- **네트워크**: huggingface.co에 접근 가능한지 확인하세요 (중국 사용자는 프록시 또는 미러가 필요할 수 있음).
- **인내심 필요**: 모델 가중치가 큽니다 (수백 MB ~ 수 GB). 다운로드 시간은 연결 속도에 따라 달라집니다.
- **중단 시 재개**: 다운로드가 중단되면 해당 디렉터리를 삭제하고 다시 시작하여 재다운로드하세요.

> 모델을 수동으로 다운로드하여 디렉터리에 배치하면 자동 다운로드를 건너뛸 수 있습니다.

### 3. 환경 변수 설정
`.env` 예제를 복사하고 API 키(DeepSeek, OpenAI 등) 및 모델 경로를 채우세요.

```bash
cp .env.example .env
# MAIN_LLM_API_KEY, 모델 경로 등을 구성하려면 .env를 편집하세요.
```

### 4. 서비스 시작
제공된 `start.sh` 스크립트를 사용하여 로컬 Ollama 모델, 백엔드, 프런트엔드 UI를 한 번에 시작하세요.

```bash
chmod +x start.sh
./start.sh
```

### 5. (선택) 수동 시작

각 구성 요소를 수동으로 시작할 수도 있습니다:

```bash
python -m server  # 백엔드 시작
```

---

## 📝 캐릭터 프로필 예시

에이전트의 동작은 `workspace/` 아래의 Markdown 파일에 의해 결정됩니다:

- **IDENTITY.md**: 이름, 나이, 관심사, 관계 등을 정의
- **SOUL.md**: 성격 대비, 말투 및 행동 논리를 정의
- **AGENTS.md**: 도구 사용 우선순위, 안전 경계 및 윤리 가이드라인을 정의
- **USER.md**: 사용자별 상호작용 선호 및 알려진 사실 저장
- **HEARTBEAT.md**: 하트비트 예약 서비스용 보류 작업 목록
- **character.json**: 구조화된 캐릭터 설정 (JSON)

---

## 🤝 기여

Issues와 Pull Requests를 환영합니다! 새 기술을 추가하려면:

1. `skills/` 아래에 폴더를 만듭니다.
2. 기술의 사용법과 단계를 설명하는 `SKILL.md`를 작성합니다.
3. 에이전트를 다시 시작하면 새 기술을 자동으로 발견하고 로드합니다.

---

연락처: QQ 3132225629

## 📄 라이선스

이 프로젝트는 MIT 라이선스에 따라 라이선스가 부여됩니다.

---

> **💡 팁**: 이 프로젝트는 고급 AI 에이전트와 딥 역할극 탐구에서 영감을 받았습니다.

# client

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

## 개요

`client`는 EMA AI Agent의 프런트엔드입니다. [server/](../server/)의 Python 백엔드(Robyn, 기본 `http://127.0.0.1:8080`)와 통신하는 **스트리밍 SPA 채팅 클라이언트**입니다.

**Tauri 2 + Nuxt 4(Vue 3 + TypeScript)**로 구축되었습니다:

- **스트리밍 채팅** — 에이전트 응답이 WebSocket(`/sessions/agent/ws`)을 통해 타입이 지정된 청크(`text` / `reasoning` / `tool_start` / `tool_end` / `tool_result`)로 스트리밍되며, HITL(human-in-the-loop) 승인 카드를 지원합니다
- **오프라인 우선 히스토리** — Dexie.js(IndexedDB)가 대화 히스토리, 서브에이전트 실행 기록, 캐릭터 프로필, 채팅 배경 이미지를 캐시합니다. 히스토리 요청은 캐시 우선이며 누락된 턴만 가져옵니다
- **풍부한 도구 UI** — 스킬 매니저, cron 작업, 하트비트 편집기, 채널 설정, 로그 뷰어(서버 + 클라이언트 로그), 통계 차트, 서브에이전트 플로우 그래프, 지식 그래프 뷰어
- **다크/라이트 모드 + i18n** — `@nuxtjs/color-mode` 테마와 4개 로케일(`zh` / `en` / `ja` / `ko`)
- **듀얼 모드 브리지** — `bridge.ts`가 Tauri 데스크톱과 일반 브라우저를 자동 감지하여 적절한 전송 방식을 선택합니다

> **개발 상태**: 활발히 개발 중입니다. 브라우저 코드 경로(Python 백엔드로의 직접 HTTP/WS)는 완전히 구현되어 있습니다. `src-tauri/`의 Rust 측은 현재 플레이스홀더 모듈만 포함합니다([아키텍처](#아키텍처) 참조).

---

## 아키텍처

### 하이브리드 아키텍처

```
+--------------------------------------------------------------+
|                프런트엔드(Nuxt 4 SPA, app/)                   |
|  Vue 3 컴포넌트 + composables + Pinia + mitt 이벤트 버스       |
+---------------------------+----------------------------------+
                            |  bridge.ts(런타임 자동 감지)
              +-------------+--------------+
              |                            |
   [브라우저 모드]                [Tauri 모드]
   ofetch HTTP REST +            invoke() IPC 커맨드
   네이티브 WebSocket             + Tauri Events(agent:stream:*)
              |                            |
              |                 src-tauri/(Rust 셸, 현재는
              |                 플레이스홀더 모듈만 존재)
              |                            |
              +-------------+--------------+
                            |
              HTTP / WS  http://localhost:8080
              (VITE_API_BACK_URL, 개발 프록시 없음)
                            |
+---------------------------v----------------------------------+
|              Python 백엔드(server/, Robyn)                    |
|  REST 엔드포인트 + WebSocket 엔드포인트                        |
|  Agent Core (LangGraph) | Memory | Skills | Cron | Channels  |
+--------------------------------------------------------------+
```

### 데이터 흐름

```
사용자 상호작용(Vue 컴포넌트)
    -> bridge.ts(Tauri / 브라우저 모드 자동 감지)
        |
        |--> [브라우저 모드] fetchApi() REST + WebSocket /sessions/agent/ws
        |        (Base64 미디어는 먼저 POST /images|/audio|/video/upload로 업로드)
        |
        |--> [Tauri 모드] invoke() -> Rust IPC 커맨드 -> Tauri Events
        |
    -> 반응형 상태(composables + Pinia + mitt 이벤트 버스)
    -> 반응형 UI 업데이트
```

**Tauri 모드 참고 사항**: `bridge.ts`에는 Tauri IPC 코드 경로(`agent_chat`, `agent_stop`, `session_clear`, `session_history`, `system_prompt_*`, `memory_*`, `system_health`, `subagent_runs`, `subagent_run_delete`)가 `app/types/backend/*`(ts-rs 생성)와 대응되어 그대로 남아 있습니다. 그러나 `src-tauri/src/`는 현재 **빈 플레이스홀더 모듈만** 포함하며(`config/`, `core/`, `database/`, `prompts/`, `rag/`, `runtime/`, `sessions/`, `skills/`, `tools/`, `types/` — 각각 빈 `mod.rs`), Rust 엔트리 포인트와 IPC 커맨드 구현은 현재 코드 트리에 존재하지 않습니다. 또한 일부 흐름(하트비트, cron, 스킬, 채널, curator, 로그, `/env`, 서브에이전트 스티어)은 Rust를 완전히 우회하며 두 모드 모두에서 항상 `fetchApi`를 거칩니다.

---

## 디렉터리 구조

```
client/
├── .env.example                   # 환경변수 템플릿(VITE_APP_NAME, VITE_API_BACK_URL)
├── eslint.config.mjs              # ESLint flat 설정
├── nuxt.config.ts                 # Nuxt 4 설정(SPA, Tailwind v4, i18n, PrimeVue, Pinia, color-mode)
├── package.json                   # 의존성 매니페스트(스크립트: dev/build/generate/preview/typecheck/test/...)
├── playwright.config.ts           # Playwright repro 설정(./repros, Desktop Edge, localhost:3000)
├── pnpm-lock.yaml                 # pnpm 락파일
├── pnpm-workspace.yaml            # pnpm 워크스페이스 설정(allowBuilds)
├── prettier.config.mjs            # Prettier 설정
├── tsconfig.json                  # TypeScript 설정
├── vitest.config.ts               # Vitest 단위 테스트 설정(composables, happy-dom, v8 커버리지)
├── vitest.integration.config.ts   # Vitest 통합 테스트 설정(실제 .vue SFC, 목 백엔드)
├── app/                           # Nuxt 4 SPA 소스
│   ├── app.vue                    # 루트 컴포넌트 — 레이아웃, Toast 레이어, 연결 배너, 로케일 복원
│   ├── common.scss                # 300줄 이상의 전역 SCSS 믹스인 라이브러리(레이아웃, 형태, 스크롤바, ...)
│   ├── assets/css/
│   │   ├── main.css               # Tailwind v4 엔트리(@import 'tailwindcss') + @theme 토큰 + 리셋
│   │   └── main.scss              # 전역 SCSS 엔트리(nuxt.config css에서 로드)
│   ├── common/utils.ts            # 공유 유틸리티(dayjs 설정, formatCompactTimeString, ...)
│   ├── components/
│   │   ├── chat/inputBox.vue      # 채팅 입력 박스 컴포넌트(i18n 지원)
│   │   └── ImagePreviewOverlay.vue# 전체 화면 이미지 미리보기 오버레이(body로 Teleport)
│   ├── composables/               # Vue 3 컴포저블 로직(단위 테스트는 __tests__/ 아래)
│   │   ├── bridge.ts              # 통합 Tauri/Browser 브리지 — 채팅 스트리밍, 세션, 시스템 프롬프트,
│   │   │                          #   메모리, 하트비트, cron, 스킬, 채널, curator, 로그, env
│   │   ├── requestApi.ts          # fetchApi HTTP 래퍼(ofetch, baseURL은 VITE_API_BACK_URL)
│   │   ├── ws.ts                  # WebSocket 싱글턴: /sessions/ws + /subagents/ws(5초 자동 재연결)
│   │   ├── db.ts                  # Dexie(IndexedDB) 캐시: 메시지, 캐릭터 프로필, 배경, 실행 기록
│   │   ├── messages.ts            # 히스토리 API(캐시 우선 /get_history_by_turn_page) + 스트림 중단 이벤트
│   │   ├── connection.ts          # 백엔드 헬스 폴링(5초) + 온라인/오프라인 Toast
│   │   ├── toast.ts               # 전역 Toast 레이어(PrimeVue Toast 등록)
│   │   ├── clientLog.ts           # 클라이언트 console.* 캡처를 Dexie에 영속화(all/log/error)
│   │   ├── env.ts                 # 백엔드 .env 읽기/쓰기(GET/PUT /env)
│   │   ├── workspace.ts           # 시스템 프롬프트 핸들러(/system_prompt GET/POST/PATCH)
│   │   ├── defaultCharacter.ts    # 내장 기본 캐릭터(이름 + /avatar/*.jpg)
│   │   ├── sessionFilter.ts       # 클라이언트 측 세션 목록 필터(키워드 + 날짜 범위)
│   │   ├── useChatBackground.ts   # 전역 채팅 배경 이미지(Dexie 영속화 싱글턴)
│   │   ├── useImagePreview.ts     # 이미지 미리보기 오버레이 상태
│   │   ├── useSubagentTasks.ts    # 백그라운드 작업 상태 싱글턴(fetch + WS + Dexie)
│   │   ├── utils.ts               # max/min + 날짜/시간 유틸리티
│   │   ├── mitt.ts                # mitt 이벤트 버스 인스턴스
│   │   └── system.ts              # (빈 플레이스홀더)
│   ├── declare/declarations.d.ts  # 타입 선언
│   ├── i18n/locales/              # en.json / ja.json / ko.json / zh.json
│   ├── layouts/default.vue        # 기본 레이아웃 — 풀뷰 래퍼
│   ├── pages/
│   │   ├── index.vue              # ChatInputBox 렌더링(루트 / 는 routeRules에 의해 /home으로 301 리다이렉트)
│   │   ├── knowledge-graph/
│   │   │   └── index.vue          # 지식 그래프 뷰어(@antv/g6, 문서 업로드, 개발 중)
│   │   └── home/
│   │       ├── index.vue          # 메인 채팅 셸 — SessionSidebar + 툴바 + 중첩 NuxtPage
│   │       ├── config.ts          # 툴바(이미지/오디오/비디오 업로드)와 헤더 도구 정의
│   │       ├── type.ts            # SessionRecord / Tool / MessageItem 타입 정의
│   │       ├── index/[sid].vue    # 세션별 채팅 페이지(KeepAlive, HITL 카드, 작업 점프 바)
│   │       ├── index/tasks/[sid].vue  # 독립형 백그라운드 작업 페이지(/home/tasks/{sid})
│   │       └── components/        # 20개 페이지 컴포넌트:
│   │           ├── ChatBox.vue            # 메시지 목록(markdown-it + DOMPurify, 미디어는 /media 경유)
│   │           ├── SessionSidebar.vue     # 세션 목록 사이드바(생성/이름 변경/필터)
│   │           ├── HistoryItem.vue        # 사이드바 히스토리 세션 항목
│   │           ├── ModeSwitch.vue         # 다크/라이트 전환(PrimeVue ToggleSwitch)
│   │           ├── ExtendDialog.vue       # "Extend" 대화상자
│   │           ├── ConfigDialog.vue       # 시스템 설정(.env 편집기, 배경, 언어, ...)
│   │           ├── PersonaDialog.vue      # 시스템 프롬프트 / 페르소나 편집기
│   │           ├── MemoryDialog.vue       # 장기 메모리 편집기(workspace/memory/*)
│   │           ├── HeartbeatDialog.vue    # HEARTBEAT.md 편집기
│   │           ├── CronDialog.vue         # cron 작업 관리(/cron)
│   │           ├── SkillsDialog.vue       # 스킬 매니저(목록/업로드/토글/고정/삭제/curator)
│   │           ├── ChannelSettingsDialog.vue  # 채널 토글 및 채널별 설정
│   │           ├── LogsDialog.vue         # 로그 뷰어(서버 로그 + 클라이언트 로그, 실시간 스트림)
│   │           ├── NotificationDialog.vue # 서버 푸시 알림 목록
│   │           ├── StatsDialog.vue        # 사용 통계(@antv/g2, GChart.vue 경유)
│   │           ├── GChart.vue             # @antv/g2 차트 래퍼
│   │           ├── SubagentTasksView.vue  # 백그라운드 작업 뷰(목록/상세/플로우 그래프)
│   │           ├── SubagentRunDetail.vue  # 단일 서브에이전트 실행 상세
│   │           ├── SubagentFlowGraph.vue  # 서브에이전트 실행 트리 그래프(@antv/g6)
│   │           └── AvatarCropDialog.vue   # 아바타 업로드 + 크롭(cropperjs)
│   ├── stores/ui.ts               # Pinia UI 스토어(sidebarCollapsed를 localStorage에 영속화)
│   └── types/
│       ├── message.ts             # BaseMessage / AiMessage / MultiModalMessage, ...
│       ├── response.d.ts          # API 응답 타입 정의
│       └── backend/               # ts-rs 생성 백엔드 타입(ChatRequest, HealthStatus, ...)
├── docs/                          # VitePress 문서 사이트(guide/commands/events/types/zh)
├── public/                        # 정적 에셋(favicon.svg, robots.txt, avatar/)
├── repros/                        # Playwright repro 테스트(playwright.config.ts로 실행)
├── src-tauri/                     # Tauri 2 네이티브 셸
│   ├── capabilities/default.json  # 권한(core:default, shell:*, notification,
│   │                              #   global-shortcut:default, window-state:default)
│   ├── icons/                     # 앱 아이콘
│   ├── resources/                 # 번들 리소스 플레이스홀더(skills/, templates/)
│   ├── src/                       # 빈 플레이스홀더 모듈만 존재: config/ core/ database/
│   │                              #   prompts/ rag/ runtime/ sessions/ skills/ tools/ types/
│   ├── tests/                     # Rust 테스트 플레이스홀더(빈 mod.rs + .gitkeep 디렉터리)
│   ├── benches/                   # 벤치마크 플레이스홀더
│   ├── .env.example               # 백엔드 모델 설정 템플릿(MAIN_LLM_*, TAVILY_API_KEY, ...)
│   ├── Cargo.toml                 # Rust 매니페스트(tauri 2, reqwest, ts-rs, tracing, 플러그인, ...)
│   ├── Cargo.lock                 # Rust 락파일
│   ├── build.rs                   # Tauri 빌드 스크립트
│   └── tauri.conf.json            # Tauri 2 설정(beforeDev: pnpm dev, beforeBuild: pnpm build)
└── tests/integration/             # Vitest 통합 테스트(실제 SFC, 목 백엔드)
```

## 기술 스택

| 계층 | 기술 | 목적 |
|-------|-----------|---------|
| **크로스 플랫폼 셸** | [Tauri 2](https://v2.tauri.app/)(`2.0.0-rc.17`, `@tauri-apps/api` ^2.11.1, `@tauri-apps/cli` 2.11.4) | 네이티브 데스크톱 패키징 + 설정/케이퍼빌리티/아이콘 |
| **프런트엔드 프레임워크** | [Nuxt 4](https://nuxt.com/) ^4.5.2 + [Vue 3](https://vuejs.org/) ^3.5.41 | SPA 모드(`ssr: false`), Composition API + `<script setup lang="ts">` |
| **UI 컴포넌트** | [PrimeVue](https://primevue.org/) ^4.5.0 + PrimeIcons ^8.0.0(`@primevue/nuxt-module`) | Dialog, Button, Select, ToggleSwitch, Toast 등 |
| **상태 관리** | [Pinia](https://pinia.vuejs.org/) ^4.0.3(`@pinia/nuxt`) + `pinia-plugin-persistedstate`(localStorage) | 전역 UI 상태(사이드바, 테마 엔트리) |
| **스타일링** | [Tailwind CSS](https://tailwindcss.com/) v4(`@tailwindcss/vite` 경유) + SCSS(`sass`) | 유틸리티 퍼스트 CSS + `@theme` 토큰 + SCSS 믹스인 라이브러리 |
| **컬러 모드** | [@nuxtjs/color-mode](https://color-mode.nuxtjs.org/) | 다크/라이트 테마 전환(`.dark` 클래스) |
| **국제화** | [@nuxtjs/i18n](https://i18n.nuxtjs.org/) 10.6.0 | zh / en / ja / ko, `no_prefix` 전략 |
| **Markdown 렌더링** | [markdown-it](https://github.com/markdown-it/markdown-it) ^15 | 채팅 메시지 markdown → HTML(ChatBox.vue) |
| **XSS 방어** | [DOMPurify](https://github.com/cure53/DOMPurify) ^3.4 | 렌더링된 HTML 새니타이즈 |
| **날짜 포맷** | [dayjs](https://day.js.org/) | 컴팩트 타임스탬프(`YYYYMMDDHHmmss`) 파싱/포맷 |
| **오프라인 저장소** | [Dexie.js](https://dexie.org/) ^4.4.4 | IndexedDB 래퍼: 메시지, 캐릭터, 배경, 서브에이전트 실행, 클라이언트 로그 |
| **이벤트 버스** | [mitt](https://github.com/developit/mitt) ^3 | 경량 컴포넌트 간 통신 |
| **차트 / 그래프** | [@antv/g2](https://g2.antv.antgroup.com/) ^5, [@antv/g6](https://g6.antv.antgroup.com/) ^5 | 통계 차트, 서브에이전트 플로우 그래프, 지식 그래프 |
| **이미지 크롭** | [cropperjs](https://github.com/fengyuanchen/cropperjs) ^1.6 | 아바타 업로드 및 크롭 |
| **유틸리티** | [lodash-es](https://lodash.com/) ^4.18 | 범용 유틸리티 함수 |
| **단위 / 통합 테스트** | [Vitest](https://vitest.dev/) ^4 + happy-dom + @vue/test-utils + @vitest/coverage-v8 | 컴포저블 단위 테스트 + SFC 통합 테스트 |
| **E2E repro** | [@playwright/test](https://playwright.dev/) ^1.62 | 개발 서버 대상 repro 테스트(Desktop Edge) |
| **Lint / 포맷** | ESLint ^10(flat config) + Prettier | 코드 품질 |
| **타입 체크** | [vue-tsc](https://github.com/vuejs/language-tools) ^3.3.9 | `pnpm typecheck` |
| **백엔드 언어** | [Rust](https://www.rust-lang.org/) 2021 edition(MSRV 1.94) | Tauri 셸(src-tauri/, 현재는 플레이스홀더 모듈) |

### 주요 설정

- **Nuxt**(`nuxt.config.ts`): `ssr: false`(순수 SPA); `devtools` 비활성화; Vite에 `clearScreen: false`, `envPrefix: ['VITE_', 'TAURI_']`, `server.strictPort: true`(Tauri는 고정 포트 필요); CSS 엔트리 `~/assets/css/main.css` + `~/assets/css/main.scss`; 라우트 규칙 `/` → `/home`으로 301 리다이렉트; `src-tauri/`는 스캔 제외
- **Tauri**(`tauri.conf.json`): 제품명 "EMA AI Agent", 버전 0.1.0, 식별자 `com.ema-ai.agent`, `beforeDevCommand: pnpm dev`, `beforeBuildCommand: pnpm build`, 개발 URL `http://localhost:3000`, frontendDist `../dist`, CSP `null`, 메인 윈도우 800×600 리사이즈 가능
- **Tailwind**(v4): `@tailwindcss/vite`로 로드; 엔트리 `app/assets/css/main.css`가 `tailwindcss`와 PrimeIcons 임포트; `@custom-variant dark`는 `.dark` 클래스로 트리거; 커스텀 `@theme` 토큰(브레이크포인트 480/768/976/1440, 색상 `gray-dark`/`gray-light`/`theme-main`, 세리프 폰트 Merriweather, z-index 1–3)
- **i18n**: 전략 `no_prefix`, `defaultLocale: 'en'`, 로케일 `zh`/`en`/`ja`/`ko`, `detectBrowserLanguage: false`; `app.vue`가 마운트 시 로케일 복원(설정 쿠키 `i18n_redirected` → 브라우저 언어 → 폴백 `en`)
- **PrimeVue**: Aura 프리셋에서 파생된 커스텀 `NoirPreset`(slate 프라이머리 팔레트); 다크 모드는 `.dark` 셀렉터
- **Pinia 영속화**: `pinia-plugin-persistedstate`를 전역으로 `storage: 'localStorage'`로 설정

---

## 프런트엔드 아키텍처

### 컴포넌트 계층

```
app.vue(루트: Toast 레이어, 연결 배너, 로케일 복원)
  └─ NuxtLayout(layouts/default.vue)
       └─ NuxtPage
            ├─ /            → /home으로 301 리다이렉트(routeRules)
            ├─ /knowledge-graph  (knowledge-graph/index.vue, @antv/g6)
            └─ /home(home/index.vue: SessionSidebar + 툴바)
                 └─ NuxtPage(page-key = route.params.sid, KeepAlive)
                      ├─ /home/{sid}       (index/[sid].vue — 채팅 + HITL 카드)
                      └─ /home/tasks/{sid} (index/tasks/[sid].vue — SubagentTasksView)
```

### 통신 브리지(bridge.ts)

`bridge.ts`는 Tauri 데스크톱과 브라우저 두 모드 모두에서 작동하는 통합 API를 제공합니다. 모든 백엔드 접근은 이것(또는 `requestApi.ts`의 `fetchApi`)을 통해 이루어집니다:

| API | 설명 |
|-----|-------------|
| `streamChatMessage(request, onChunk, onHitl?, onDone?)` | 스트리밍 에이전트 채팅; `{ controller, promise }` 반환(Tauri Events 또는 `/sessions/agent/ws`) |
| `sendChatMessage(request, onChunk)` | `streamChatMessage`의 편의 래퍼 |
| `stopChatMessage(sessionId)` | 진행 중인 생성 중지(`agent_stop` IPC 또는 WS `stop` 프레임) |
| `resumeHitl(sessionId, decision, ...)` | 새 WebSocket으로 일시 중지된 HITL 에이전트 재개 |
| `clearSession(sessionId)` | 세션 상태 클리어(`session_clear` IPC 또는 `DELETE /sessions`) |
| `getHistory(sessionId, lastTurnCount)` | 히스토리 조회(`session_history` IPC 또는 `GET /n_turns_history_messages`) |
| `fetchSubagentRuns` / `fetchSubagentRunSubtree` / `deleteSubagentRunSubtree` / `steerSubagentRun` | 백그라운드 서브에이전트 작업 관리 |
| `readSystemPrompt` / `writeSystemPrompt` / `updateSystemPrompt` / `readSystemPromptTemplate` | 시스템 프롬프트 파일 CRUD |
| `readMemory` / `writeMemory` | 장기 메모리 파일(`workspace/memory/*`) |
| `readHeartbeat` / `writeHeartbeat` | `workspace/HEARTBEAT.md`(항상 직접 HTTP) |
| `listCronJobs` / `addCronJob` / `updateCronJob` / `runCronJob` / `enableCronJob` / `deleteCronJob` | cron 작업 관리 |
| `listSkills` / `readSkill` / `uploadSkill` / `setSkillActive` / `deleteSkill` / `pinSkill` | 스킬 관리 |
| `listChannels` / `updateChannel` / `getChannelConfig` / `updateChannelConfig` | 채널 설정 |
| `runCuratorReview` / `getCuratorSettings` / `setCuratorSettings` | 자동 스킬 curator 제어 |
| `listLogFiles` / `readLogFile` / `openLogStream` | 백엔드 로그 읽기 + 실시간 `/logs/ws` 스트림 |
| `readEnvConfig` / `writeEnvConfig`(`env.ts`) | 백엔드 `.env` 읽기/업데이트(`GET/PUT /env`) |
| `checkHealth()` | 백엔드 도달 가능성(`system_health` IPC 또는 `GET /system_prompt`) |

브라우저 모드 채팅 스트리밍 세부 사항:

- `ws(s)://{VITE_API_BACK_URL}/sessions/agent/ws`에 연결하여 `{ session_id, multi_modal_message }` 전송
- Base64 미디어는 먼저 `POST /images/upload`, `/audio/upload`, `/video/upload`로 업로드되고 URL로 참조됨
- 서버 프레임: `{ event: "chunk" | "done" | "error" | "stopped" | "hitl_request", ... }`; 청크는 `type`(`text`/`reasoning`/`tool_start`/`tool_end`/`tool_result`)과 도구 메타데이터를 포함
- 스트림 중단 시 지수 백오프로 재연결(1s/2s/4s, `WS_RECONNECT_MAX_ATTEMPTS`로 최대 3회); 스트림 중 손실은 `StreamInterruptedError`를 발생시키고 mitt를 통해 `ws:conn-loss` / `stream:reconnecting` / `stream:reconnected` / `stream:reconnect:failed` 이벤트를 발행
- HITL 인터럽트는 `HitlInterruptData`(도구 이름/인자/선택 가능한 결정)를 포함; 결정은 `hitl_response` 프레임(`approve` / `reject` / `edit`)으로 전송

### WebSocket 싱글턴(ws.ts)

서로 독립적인 2개의 모듈 수준 싱글턴 연결(둘 다 5초 후 자동 재연결):

| 연결 | 엔드포인트 | mitt로 발행되는 이벤트 |
|-----------|----------|-----------------|
| 세션 푸시 | `/sessions/ws?session_id=default` | `ws:connected`, `ws:notification`, `ws:message`, `ws:disconnected` |
| 서브에이전트 푸시 | `/subagents/ws` | `ws:subagents:connected`, `ws:subagents:ready`, `ws:subagent_spawned`, `ws:subagent_ended`, `ws:subagents:message`, `ws:subagents:disconnected` |

둘 다 `VITE_API_BACK_URL`에서 베이스 URL을 해석합니다(`http://` → `ws://`, `https://` → `wss://`).

### 클라이언트가 사용하는 백엔드 엔드포인트

REST(베이스 URL `VITE_API_BACK_URL`, 기본 `http://localhost:8080`):

| 엔드포인트 | 메서드 | 용도 |
|----------|-----------|---------|
| `/sessions` | DELETE | 세션 클리어 |
| `/n_turns_history_messages` | GET | 최근 N턴 히스토리 |
| `/get_history_by_turn_page` | GET | 페이지네이션된 히스토리(Dexie로 캐시 우선) |
| `/sessions/agent/ws` | WS | 채팅 스트리밍, 중지, HITL 재개 |
| `/sessions/ws` | WS | 서버 푸시 알림 |
| `/subagents/ws` | WS | 서브에이전트 생성/종료 푸시 |
| `/subagents/runs` | GET / DELETE | 서브에이전트 실행 기록(목록/서브트리/삭제) |
| `/subagents/steer` | POST | 서브에이전트 실행 스티어/재개 |
| `/system_prompt` | GET / POST / PATCH / PUT | 시스템 프롬프트 읽기/쓰기/업데이트 |
| `/system_prompt/template` | GET | 페르소나 템플릿 파일 |
| `/memory` | GET / PUT | 장기 메모리 파일 |
| `/heartbeat` | GET / PUT | HEARTBEAT.md |
| `/cron`, `/cron/trigger`, `/cron/enable` | GET/POST/PUT/DELETE | cron 작업 CRUD + 트리거 |
| `/skills`, `/skills/{path}`, `/skills/upload`, `/skills/toggle`, `/skills/delete`, `/skills/pin` | GET/POST | 스킬 관리 |
| `/curator/run`, `/curator/settings` | POST / GET / PUT | curator 리뷰 및 설정 |
| `/channels`, `/channels/{name}`, `/channels/{name}/config` | GET / PUT | 채널 토글 및 설정 |
| `/env` | GET / PUT | 백엔드 `.env` 읽기/업데이트 |
| `/logs/files`, `/logs` | GET | 로그 파일 목록 및 꼬리 읽기 |
| `/logs/ws` | WS | 실시간 로그 스트림 |
| `/images/upload`, `/audio/upload`, `/video/upload` | POST | Base64 미디어 업로드 → URL |
| `/media` | GET | 저장된 미디어 파일 렌더링 |

---

## 핵심 모듈 세부 사항

### SCSS 믹스인 라이브러리(`common.scss`)

300줄 이상의 SCSS 믹스인 라이브러리로, 레이아웃·형태·스크롤바·텍스트 오버플로우 유틸리티(`fullViewWindow`, `flexCenter`, `scrollBar`, `wordEllipsis` 등)를 제공하며 페이지, 컴포넌트, 레이아웃 전반에서 사용됩니다.

### 오프라인 캐시(db.ts, Dexie/IndexedDB)

- `CachedMessage` — 백엔드 메시지 테이블 행을 미러링(turn_num, images/audios/videos, tool 필드, 토큰 수); 히스토리 요청은 캐시 우선이며 캐시된 최대 턴보다 새로운 턴만 가져옴
- `CachedCharacter` — 세션별 아바타/이름 스냅샷(base64 data URL 또는 `public/avatar/`의 `/avatar/*.jpg`)
- `/subagents/ws` 푸시와 REST 조회에서 캐시된 서브에이전트 실행 기록
- 채팅 배경 이미지 설정(전역, `useChatBackground`의 모듈 수준 싱글턴)
- `clientLog.ts`가 캡처한 브라우저 `console.*` 출력을 `all` / `log` / `error` 버킷 구조로 영속화

### 상태와 이벤트

- **Pinia**(`stores/ui.ts`): 사이드바 접기(영속화), 설정 메뉴 플래그, 테마 쓰기 엔트리
- **mitt 이벤트 버스**: WS 이벤트, 스트림 재연결 이벤트, 세션 스트림 중단(`session:abort-stream`), 컴포넌트 간 알림
- **connection.ts**: 5초마다 `checkHealth()` 폴링; `isOnline` / `backendStatus`를 노출하고 `app.vue`의 전역 연결 배너를 구동

### 타입 생성(app/types/backend/)

[ts-rs](https://github.com/Aleph-Alpha/ts-rs)로 Rust 백엔드 구조체에서 생성된 TypeScript 인터페이스(`ChatRequest`, `HistoryMessage`, `HealthStatus`, `AgentStreamChunk`, `PromptFileResponse` 등). 파일에 "Do not edit manually" 헤더가 있습니다.

### 테스트

- **단위 테스트**(`pnpm test`): `app/**/*.{test,spec}.ts` — `app/composables/__tests__/` 아래 20개 이상의 컴포저블 테스트 스위트(happy-dom, `vue-i18n` 스텁)
- **통합 테스트**(`pnpm test:integration`): `tests/integration/` — 실제 `.vue` SFC 마운트(ChatBox, HistoryItem, 홈 페이지, ModeSwitch, inputBox, 이미지 렌더링), 백엔드는 목
- **Repro**(`repros/`): `localhost:3000`의 Nuxt 개발 서버 대상 Playwright 스펙(MS Edge 채널 설치됨)

---

## 개발 가이드

### 전제 조건

- [Node.js](https://nodejs.org/)(LTS)
- [pnpm](https://pnpm.io/) — 프로젝트는 `pnpm-lock.yaml` / `pnpm-workspace.yaml`를 사용합니다. 락파일을 정본으로 유지하려면 pnpm을 사용하세요
- [Rust](https://www.rust-lang.org/) 툴체인(MSRV 1.94) — `src-tauri/` 빌드에만 필요
- 프로젝트 루트의 Python 백엔드([루트 README](../README.md) 참조)

### 자주 사용하는 명령

```bash
# 의존성 설치(패키지 매니저는 pnpm. postinstall에서 `nuxt prepare` 실행)
pnpm install

# 개발 서버(브라우저 모드, Nuxt 개발 서버 http://localhost:3000)
pnpm dev

# 프로덕션 빌드(SPA 출력은 dist/ — tauri.conf.json의 frontendDist)
pnpm build

# 정적 생성 / 프리뷰
pnpm generate
pnpm preview

# 타입 체크(vue-tsc --noEmit)
pnpm typecheck

# 단위 테스트(Vitest, happy-dom)
pnpm test
pnpm test:watch

# 통합 테스트(실제 SFC, 목 백엔드)
pnpm test:integration
pnpm test:integration:watch

# Tauri 데스크톱 개발 모드(beforeDevCommand로 먼저 `pnpm dev` 실행)
pnpm tauri dev

# Tauri 데스크톱 빌드(beforeBuildCommand로 먼저 `pnpm build` 실행)
pnpm tauri build
```

`pnpm tauri dev` / `pnpm tauri build`는 `@tauri-apps/cli` 개발 의존성의 Tauri CLI를 호출합니다.

### 환경 변수

```bash
# client/.env.example
VITE_APP_NAME="sherry"                     # 앱 표시 이름(document <title>)
VITE_API_BACK_URL="http://localhost:8080"  # Python 백엔드 베이스 URL(REST + WS)
```

모든 백엔드 호출은 `VITE_API_BACK_URL`을 해석하며 하드코딩된 폴백 `http://localhost:8080`을 가집니다. 개발 서버 프록시가 없습니다 — 프런트엔드는 백엔드에 직접 접근하므로 백엔드가 크로스 오리진 요청을 허용해야 합니다(백엔드는 `Access-Control-Allow-Origin: *`를 반환).

`src-tauri/.env.example`에는 별도로 **백엔드** 모델 설정 템플릿(`MAIN_LLM_*`, `REASONER_*`, `SIMPLE_MAIN_LLM_*`, `ITTT_*`, `TTI_*`, `RERANKER_*`, `EMBEDDING_*`, `TAVILY_API_KEY`, `LANGSMITH_*`)이 있습니다.

### Python 백엔드 시작

프로젝트 루트에서(전체 설정은 루트 README 참조):

```bash
uv run python -m server
```

백엔드는 기본적으로 `http://127.0.0.1:8080`에서 수신 대기합니다(클라이언트 측은 `VITE_API_BACK_URL`로 변경 가능).

### 새 페이지 / 컴포넌트 추가

1. `app/pages/` 아래에 `.vue` 파일 생성 — Nuxt 4가 라우트를 자동 등록(`home/` 아래의 중첩 페이지는 `home/index.vue`의 내부 `<NuxtPage>`로 렌더링)
2. `app/components/` 또는 `app/pages/home/components/` 아래에 컴포넌트 생성
3. `app/composables/` 아래에 컴포저블 로직 추가(단위 테스트는 `app/composables/__tests__/`에)
4. `app/assets/css/main.css`의 `@theme` 블록에 커스텀 토큰 추가
5. 4개 로케일 파일 모두에 i18n 키 추가: `app/i18n/locales/{en,ja,ko,zh}.json`

### 새 백엔드 호출 추가

1. `app/composables/bridge.ts`에 엔드포인트 호출 추가(순수 REST라면 `requestApi.ts`) — Tauri IPC 경로, `fetchApi`, 둘 다 중 무엇을 사용할지 결정
2. 백엔드가 새 페이로드 형태를 보내는 경우 `app/types/backend/` 또는 `app/types/message.ts`의 해당 타입 확장
3. `app/composables/__tests__/`에 단위 테스트 추가

---

## 라이선스

MIT — EMA AI Agent 본 프로젝트와 동일(`src-tauri/Cargo.toml`의 `license` 필드 참조).

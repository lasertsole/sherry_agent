# client

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

## 개요

`client`는 EMA AI Agent의 프론트엔드로, **스트리밍 SPA 데스크톱 클라이언트**로 설계되었습니다.

**Tauri 2 + Nuxt 4** 기반으로, 다음과 같은 목표를 달성합니다:

- **더 부드러운 상호작용** — 전체 페이지 재실행 대신 Vue 3 반응형 부분 업데이트
- **오프라인 우선** — Dexie.js(IndexedDB)로 대화 기록을 로컬에 캐시
- **네이티브 데스크톱 기능** — Tauri 2는 시스템 트레이, 전역 단축키(Alt+Space), 파일 시스템 접근 등 Streamlit으로는 불가능한 기능 제공
- **컴포넌트 기반 아키텍처** — Vue 3 Composition API + composables로 확장 가능한 팀 협업 지원

> **개발 상태**: 활발히 개발 중; 핵심 채팅 UI와 Tauri IPC 브리지가 작동합니다.

---

## 아키텍처

### 하이브리드 아키텍처

```
+--------------------------------------------------------------------+
|                    Tauri 2 Desktop App (client/)              |
|                                                                    |
|  +-------------------+       invoke()       +--------------------+ |
|  |  Nuxt 4 Frontend  | ===================> |  Rust Backend      | |
|  |  (app/)           |                      |  (src-tauri/src/)  | |
|  |                   | <=================== |                    | |
|  |  bridge.ts        |   Tauri Events       |  commands/         | |
|  |  (dual-mode)      |   (streaming)        |  services/         | |
|  +-------------------+                      |  core/              | |
|                                             |  utils/             | |
|                                             +---------+----------+ |
|                                                       |            |
+-------------------------------------------------------|------------+
                                                         |
                                           reqwest HTTP (localhost:8080)
                                           SSE stream / JSON REST
                                                         |
+-------------------------------------------------------|------------+
|                    Python Backend (server/)             |            |
|                                                        v           |
|  Robyn HTTP + SSE + WebSocket                                      |
|  Agent Core (LangGraph) | RAG | Multi-channel     |
+--------------------------------------------------------------------+
```

**Rust 구현**: Rust 레이어는 다음과 같은 기능을 구현합니다:
1. 프론트엔드의 Tauri IPC 호출 수신
2. HTTP 요청으로 Python 백엔드(`http://127.0.0.1:8080`)에 전달
3. Python의 SSE 스트림을 Tauri Events로 변환하여 실시간 프론트엔드 업데이트
4. Python 백엔드 프로세스 수명주기 관리(`EMA_AUTO_START_BACKEND`로 선택적 자동 시작)
5. 시스템 트레이(표시/숨김/종료) 및 전역 단축키(Alt+Space 창 전환) 제공

### 데이터 흐름

```
User interaction (Vue component)
    -> bridge.ts (auto-detect Tauri vs browser mode)
        |
        |--> [Tauri mode] invoke() -> Rust IPC command
        |        -> PythonBridge (reqwest HTTP) -> Python backend
        |        -> SSE stream -> Tauri Events -> frontend listener
        |
        |--> [Browser mode] fetchApi() -> Python backend (direct SSE/REST)
        |
    -> Reactive state (composables + mitt event bus)
    -> Reactive UI update
```

---

## 디렉터리 구조

```
client/
├── .env.example                   # Environment variable template
├── .gitignore                     # Git ignore rules
├── eslint.config.mjs              # ESLint flat config
├── nuxt.config.ts                 # Nuxt 4 configuration (SSR=off, Vite, Tailwind, i18n, PrimeVue, color-mode)
├── package.json                   # Dependency manifest (pnpm workspace root)
├── pnpm-lock.yaml                 # pnpm lockfile
├── pnpm-workspace.yaml            # pnpm workspace definition
├── prettier.config.mjs            # Prettier code formatter config
├── tsconfig.json                  # TypeScript configuration
├── app/                           # Nuxt 4 SPA source
│   ├── app.vue                    # Root component entry
│   ├── common.scss                # Global SCSS mixin library (layout, shapes, scrollbar, etc.)
│   ├── assets/
│   │   ├── css/
│   │   │   ├── main.css           # Tailwind v4 entry (@import 'tailwindcss') + @theme tokens + global reset
│   │   │   └── main.scss          # (reserved)
│   │   └── images/                # (reserved) Static images
│   ├── common/
│   │   └── utils.ts               # Shared utilities (formatCompactTimeString via dayjs)
│   ├── components/
│   │   └── chat/
│   │       └── inputBox.vue       # Chat input box component (i18n-aware)
│   ├── composables/               # Vue 3 composable logic
│   │   ├── bridge.ts              # Unified Tauri/Browser communication bridge
│   │   ├── messages.ts            # Message API: history, clear session, SSE streaming
│   │   ├── requestApi.ts          # HTTP request wrapper (useFetch + retry logic)
│   │   ├── system.ts              # (reserved) System composable
│   │   ├── utils.ts               # Date/time utilities (comparison, formatting, UTC conversion)
│   │   ├── workspace.ts           # System prompt & character CRUD operations
│   │   ├── ws.ts                  # WebSocket singleton with auto-reconnect (5s)
│   │   └── mitt.ts                # mitt event bus instance
│   ├── declare/
│   │   └── declarations.d.ts      # Type declarations
│   ├── i18n/
│   │   └── locales/
│   │       ├── en.json            # English translations
│   │       └── zh.json            # Chinese translations
│   ├── layouts/
│   │   └── default.vue            # Default layout — Nuxt 4 layout entry
│   ├── pages/
│   │   ├── index.vue              # Root page (redirects to /home)
│   │   └── home/
│   │       ├── index.vue          # Main chat page — sidebar + chat area
│   │       ├── config.ts          # Toolbar & header tool configurations
│   │       ├── type.ts            # Session/Message/ChatRole type definitions
│   │       └── components/
│   │           ├── ChatBox.vue    # Message list with markdown rendering & XSS sanitization
│   │           ├── HistoryItem.vue# Sidebar history session item
│   │           └── ModeSwitch.vue # Dark/Light mode toggle (PrimeVue ToggleSwitch)
│   └── types/
│       ├── message.ts             # Message type definitions (BaseMessage, AiMessage, MultiModalMessage, etc.)
│       └── response.d.ts          # API response type definitions
├── src-tauri/                     # Tauri 2 native shell
│   ├── capabilities/
│   │   └── default.json           # Permission config (currently core:default only)
│   ├── icons/                     # App icons
│   ├── src/
│   │   ├── lib.rs                 # Tauri app entry — Builder setup, tray menu, global shortcut, Python process manager
│   │   ├── main.rs                # Windows subsystem entry + calls lib::run()
│   │   ├── commands/              # Tauri IPC command handlers
│   │   │   ├── mod.rs
│   │   │   ├── agent.rs           # agent_chat, agent_stop
│   │   │   ├── character.rs       # character_read/write/update
│   │   │   ├── events.rs          # Event type definitions
│   │   │   ├── session.rs         # session_clear, session_history
│   │   │   ├── system.rs          # system_info, system_health
│   │   │   └── system_prompt.rs   # system_prompt_read/write/update
│   │   ├── services/
│   │   │   ├── mod.rs
│   │   │   ├── python_bridge.rs   # HTTP bridge to Python backend (reqwest + SSE → Tauri Events)
│   │   │   └── python_process.rs  # Python backend process lifecycle manager
│   │   ├── core/                  # Core domain modules (stub/placeholder)
│   │   │   ├── mod.rs
│   │   │   ├── agent/
│   │   │   ├── bus/
│   │   │   ├── channel/
│   │   │   ├── cron/
│   │   │   ├── heartbeat/
│   │   │   ├── memory/
│   │   │   └── subagent/
│   │   ├── utils/
│   │   │   ├── mod.rs
│   │   │   ├── config.rs          # AppConfig (from environment variables)
│   │   │   ├── error.rs           # Error types
│   │   │   └── logger.rs          # Tracing setup
│   │   ├── config/
│   │   ├── database/
│   │   ├── models/
│   │   ├── prompts/
│   │   ├── rag/
│   │   ├── runtime/
│   │   ├── sessions/
│   │   ├── skills/
│   │   ├── tools/
│   │   └── types/
│   ├── Cargo.toml                 # Rust dependencies (tauri 2, serde, reqwest, tracing, ts-rs, etc.)
│   ├── tauri.conf.json            # Tauri 2 config — app name "EMA AI Agent", identifier "com.ema-ai.agent"
│   └── build.rs                   # Tauri build script
└── public/                        # Nuxt public static assets
```

---

## 기술 스택

| 레이어 | 기술 | 용도 |
|------|------|------|
| **크로스 플랫폼 셸** | [Tauri 2](https://v2.tauri.app/) | 웹 프론트엔드를 시스템 API 접근이 가능한 네이티브 데스크톱 앱으로 패키징 |
| **프론트엔드 프레임워크** | [Nuxt 4](https://nuxt.com/) + [Vue 3](https://vuejs.org/) | SPA 모드(`ssr: false`), Composition API + `<script setup lang="ts">` |
| **UI 컴포넌트** | [PrimeVue 5](https://primevue.org/) + [PrimeIcons](https://primevue.org/icons) | 사전 제작된 UI 컴포넌트(Button, Checkbox, Menu, ToggleSwitch 등) |
| **상태 관리** | [Vue 3 Composition API](https://vuejs.org/guide/extras/composition-api-faq)(composables + [mitt](https://github.com/developit/mitt) 이벤트 버스) | 전역 상태 + 반응형 UI 업데이트 |
| **스타일링** | [Tailwind CSS](https://tailwindcss.com/)(v4, `@tailwindcss/vite` 통해) + SCSS | 유틸리티 우선 CSS + 커스텀 mixin 라이브러리 |
| **색상 모드** | [@nuxtjs/color-mode](https://color-mode.nuxtjs.org/) | 다크/라이트 테마 전환 |
| **국제화** | [@nuxtjs/i18n](https://i18n.nuxtjs.org/) | 중국어(기본) / 영어 |
| **Markdown 렌더링** | [markdown-it](https://github.com/markdown-it/markdown-it) | 채팅 메시지 마크다운 → HTML |
| **XSS 보호** | [DOMPurify](https://github.com/cure53/DOMPurify) | HTML 출력 정화 |
| **날짜 포맷** | [dayjs](https://day.js.org/) | 컴팩트 타임스탬프(YYYYMMDDHHmmss) 파싱/포맷 |
| **오프라인 저장** | [Dexie.js](https://dexie.org/) | 대화 기록 캐싱을 위한 IndexedDB 래퍼 |
| **이벤트 버스** | [mitt](https://github.com/developit/mitt) | 경량 컴포넌트 간 통신 |
| **유틸리티** | [lodash-es](https://lodash.com/) | 깊은 복제, 중복 제거 및 기타 공통 함수 |
| **빌드 도구** | [Vite](https://vitejs.dev/) | 개발 서버 + 프로덕션 빌드 |
| **백엔드 언어** | [Rust](https://www.rust-lang.org/) 2021 edition(MSRV 1.94) | Tauri 네이티브 로직 |
| **로깅** | [tracing](https://docs.rs/tracing/) + [tauri-plugin-tracing](https://github.com/tauri-apps/tauri-plugin-tracing) | 구조화 로깅(Tauri 백엔드) |
| **타입 생성** | [ts-rs](https://github.com/Aleph-Alpha/ts-rs) | Rust 구조체에서 TypeScript 타입 자동 생성 |

### 핵심 구성

- **Nuxt**: `ssr: false`(순수 SPA); `pages/` 디렉터리 구조; `VITE_*` 및 `TAURI_*` 환경 변수 접두사 화이트리스트가 있는 Vite 구성; `/` 라우트는 `/home`으로 리다이렉트
- **Tauri**: 앱 식별자 `com.ema-ai.agent`, 제품명 "EMA AI Agent", 개발 URL `http://localhost:3000`, 인라인 스타일 허용을 위해 CSP를 `null`로 설정, 창 800×600 크기 조정 가능
- **Tailwind**(v4): `@tailwindcss/vite` Vite 플러그인으로 로드, `app/assets/css/main.css`(`@import 'tailwindcss'`)에 엔트리, 커스텀 토큰(색상, 브레이크포인트, z-index)은 `@theme` 블록에 정의; 동적 간격(예: `h-15`)은 `--spacing`로 자동 생성
- **i18n**: 기본 로케일 `zh`, 전략 `prefix_except_default`
- **PrimeVue**: Noir 프리셋(slate 색상 팔레트), `.dark` CSS 클래스 선택자로 다크 모드

---

## 아키텍처

### 아키텍처 개요

```
client/ (Tauri+Nuxt+Vue)
                         │
                         ├── Streaming SPA, partial refresh
                         ├── Native desktop app (Tauri 2)
                         ├── System tray + global shortcut (Alt+Space)
                         ├── Offline cache (Dexie/IndexedDB)
                         ├── WebSocket real-time updates
                         ├── Markdown rendering + XSS sanitization
                         ├── Dark/Light mode + i18n
                         └── Module-based state via Vue 3 composables
```

### 컴포넌트 계층

```
app.vue (root)
  └─ NuxtLayout (default.vue)
       └─ NuxtPage
            ├─ / (redirect → /home)
            └─ /home (home/index.vue)
                 ├─ HistoryItem (sidebar session list)
                 ├─ ModeSwitch (dark/light toggle)
                 └─ ChatBox (message area)
                      └─ Markdown rendering (markdown-it + DOMPurify)
```

### 통신 브리지 (bridge.ts)

`bridge.ts` composable은 Tauri 데스크톱과 브라우저 모드 모두에서 작동하는 통합 API를 제공합니다:

| API | 설명 |
|-----|------|
| `sendChatMessage(request, onChunk)` | 스트리밍 Agent 채팅(Tauri Events / SSE) |
| `stopChatMessage(sessionId)` | 진행 중인 생성 중지 |
| `clearSession(sessionId)` | 세션 상태 초기화 |
| `getHistory(sessionId, lastTurnCount)` | 대화 기록 조회 |
| `readSystemPrompt()` | 모든 시스템 프롬프트 파일 읽기 |
| `writeSystemPrompt(fileToContent)` | 시스템 프롬프트 파일 덮어쓰기 |
| `updateSystemPrompt(fileToContent)` | 시스템 프롬프트 파일 병합 업데이트 |
| `readCharacter()` | 캐릭터 설정 읽기 |
| `writeCharacter(data)` | 캐릭터 설정 덮어쓰기 |
| `updateCharacter(data)` | 캐릭터 설정 병합 업데이트 |
| `checkHealth()` | Python 백엔드 연결 가능 여부 확인 |

### WebSocket (ws.ts)

실시간 서버 푸시 알림을 위한 WebSocket 싱글턴:

- `{wsBase}/sessions/ws?session_id=default`에 연결
- 연결 끊김 시 자동 재연결(5초 지연)
- mitt로 이벤트 발행: `ws:connected`, `ws:disconnected`, `ws:notification`, `ws:message`
- `VITE_API_BACK_URL` 환경 변수에서 WS URL 해석

---

## 핵심 모듈 상세

### SCSS Mixin 라이브러리 (`common.scss`)

레이아웃, 모양, 스크롤바 및 텍스트 오버플로 유틸리티를 제공하는 300+ 라인 SCSS mixin 라이브러리:
- 크기 제한: `minWidth` / `maxWidth` / `fixedWidth` / `fullWidth` 등
- 모양: `fixedRoundedRectangle` / `fixedCircle` / `fixedCapsule` 등
- 레이아웃: `flexCenter` / `scrollBar` / `wordEllipsis` 등
- 이미지: `imgFullInParent` / `fullImg` 등

### Tauri 백엔드 (`src-tauri/`)

- **lib.rs**: `tauri::Builder` 시작, 시스템 트레이(표시/숨김/종료), 전역 단축키(Alt+Space 전환), Python 프로세스 관리자(`EMA_AUTO_START_BACKEND`로 자동 시작)
- **main.rs**: Windows 서브시스템 엔트리, `#![windows_subsystem = "windows"]`로 릴리스 빌드에서 콘솔 창 숨김
- **tauri.conf.json**: 앱 식별자 `com.ema-ai.agent`, 제품명 "EMA AI Agent", 빌드 명령 `pnpm build`, 개발 URL `http://localhost:3000`
- **Cargo.toml**: Rust 의존성 — tauri 2.x(tray-icon feature), serde + serde_json, reqwest(rustls-tls, streaming), tracing + tauri-plugin-tracing, ts-rs, thiserror, anyhow, tokio, uuid, 플러그인: shell, notification, global-shortcut, single-instance, window-state

### Rust 모듈 구조

```
src-tauri/src/
├── commands/          # IPC command handlers
│   ├── agent.rs       # agent_chat, agent_stop
│   ├── character.rs   # character_read/write/update
│   ├── session.rs     # session_clear, session_history
│   ├── system.rs      # system_info, system_health
│   └── system_prompt.rs
├── services/
│   ├── python_bridge.rs   # HTTP bridge (reqwest + SSE → Tauri Events)
│   └── python_process.rs  # Python backend process manager
├── core/              # Domain modules (stubs)
├── utils/
│   ├── config.rs      # AppConfig from env vars
│   ├── error.rs       # Error types
│   └── logger.rs      # Tracing setup
├── config/
├── database/
├── models/
├── prompts/
├── rag/
├── runtime/
├── sessions/
├── skills/
├── tools/
└── types/
```

---

## 개발 가이드

### 사전 요구사항

- [Node.js](https://nodejs.org/) >= 18
- [pnpm](https://pnpm.io/)(권장) 또는 npm
- [Rust](https://www.rust-lang.org/) >= 1.94(MSRV)
- [Tauri CLI v2](https://v2.tauri.app/start/cli/)

### 일반 명령어

```bash
# Install dependencies
pnpm install

# Dev mode (Web browser)
pnpm dev

# Tauri desktop dev mode
pnpm tauri dev

# Production build
pnpm build

# Tauri desktop build
pnpm tauri build

# Rust compilation check
cd src-tauri && cargo check

# Rust tests
cd src-tauri && cargo test
```

### 환경 변수

```bash
# .env.example
VITE_API_BACK_URL=http://localhost:8080  # Python backend URL
VITE_APP_NAME=EMA AI Agent               # App display name
EMA_PROJECT_ROOT=..                       # Project root for Python backend auto-spawn
EMA_AUTO_START_BACKEND=true               # Auto-start Python backend with Tauri app
```

### 새 페이지 / 컴포넌트 추가

1. `app/pages/` 아래에 `.vue` 파일 생성 — Nuxt 4가 라우트 자동 등록
2. `app/components/` 아래에 컴포넌트 생성 — 전역에서 자동 사용 가능
3. `app/composables/` 아래에 composable 로직 생성
4. `app/assets/css/main.css`의 `@theme` 블록에 커스텀 토큰 추가
5. `app/i18n/locales/zh.json`과 `en.json`에 i18n 키 추가

### Python 백엔드 시작

```bash
# From the project root (EMA_AI_agent/)
python -m server
```

백엔드는 기본적으로 `http://127.0.0.1:8080`에서 시작됩니다(`VITE_API_BACK_URL`로 구성 가능).

### 새 IPC 명령 추가

1. `src-tauri/src/commands/<module>.rs`에서 `#[derive(TS)]`로 요청/응답 타입 정의
2. `PythonBridge` 메서드를 사용해 `#[tauri::command]` 함수 구현
3. `lib.rs`의 `.invoke_handler(tauri::generate_handler![...])`에 명령 등록
4. `cargo test`를 실행해 `app/types/backend/`에 TypeScript 타입 재생성
5. `app/composables/bridge.ts`에 해당 래퍼 추가

---

## 라이선스

EMA AI Agent 메인 프로젝트 라이선스와 동일합니다.

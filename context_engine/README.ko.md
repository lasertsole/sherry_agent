# MesMemory — 세션 메시지 메모리 시스템

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **MesMemory**는 EMA AI Agent의 단기 대화 메모리 엔진(`context_engine` 패키지)입니다. SQLite 기반 세션 메시지 영속화, 히스토리 조회, FTS5 전문 검색을 담당합니다. 이 패키지에는 백그라운드 스킬 유지보수를 담당하는 **Curator** 서브패키지도 포함되어 있습니다 — [Curator(스킬 유지보수 서브패키지)](#curator스킬-유지보수-서브패키지) 참조.

---

## 목차

- [개요](#개요)
- [패키지 구조](#패키지-구조)
- [데이터 모델](#데이터-모델)
- [핵심 기능](#핵심-기능)
- [통합 지점](#통합-지점)
- [Curator(스킬 유지보수 서브패키지)](#curator스킬-유지보수-서브패키지)
- [API 레퍼런스](#api-레퍼런스)
- [FAQ](#faq)
- [기술 스택](#기술-스택)

---

## 개요

### 설계 위치

MesMemory는 **세션 단위의 단기 메시지 저장소**이며, 의도적으로 단순하게 설계되었습니다. 모든 저장·조회는 SQL/FTS5 기반이며, 이 패키지에는 벡터 임베딩, 그래프 알고리즘, 리랭커(reranker)가 전혀 없습니다.

| | MesMemory |
|---|-----------|
| 범위 | 각 세션의 원시 `human` / `ai` / `tool` 메시지 |
| 저장소 | 공유 단일 SQLite 데이터베이스(`src/store/mes_memory/mes_memory.db`) |
| 조회 | 최근 N 턴, 턴 범위 쿼리, 페이지네이션 히스토리, FTS5 전문 검색 |
| 쓰기 | `await add_messages(...)` — 호출 1회당 1턴 영속화 |

에이전트가 생성한 스킬의 장기 유지보수(라이프사이클 전이, 통합, 정리)는 `context_engine/` 내부의 별도 [Curator](#curator스킬-유지보수-서브패키지) 서브패키지가 담당합니다 — 메시지 데이터에는 **전혀 접근하지 않습니다**.

### 핵심 기능

1. **메시지 영속화** — 각 대화 턴의 `human`/`ai`/`tool` 메시지를 SQLite에 기록
2. **히스토리 조회** — 최근 N 턴, 턴 범위, 페이지 단위로 히스토리 조회
3. **전문 검색** — FTS5 기반 대화 검색. CJK 쿼리용 trigram 경로, LIKE 폴백, 컨텍스트 미리보기 제공
4. **세션 관리** — 최상위 세션 목록(파생 제목 포함). 세션의 전체 메시지 삭제

---

## 패키지 구조

```
context_engine/
├── __init__.py          # 패키지 익스포트 (store와 core의 API 재익스포트)
├── core.py              # 비즈니스 레이어: 히스토리 포맷팅, FTS5 검색
├── store/
│   ├── __init__.py      # 스토어 레이어 익스포트
│   ├── db.py            # SQLite 연결, WAL 모드, 버전 관리된 마이그레이션(테이블, 인덱스, FTS5 트리거)
│   └── core.py          # 메시지 CRUD: 추가/조회/삭제 + 세션 목록
└── curator/             # 백그라운드 스킬 유지보수 오케스트레이터 (별도 README 보유)
```

```
┌──────────────────────────────────────────────────────┐
│                    context_engine                    │
├──────────────────────┬───────────────────────────────┤
│  store/  (데이터)     │     core.py  (비즈니스)        │
├──────────────────────┼───────────────────────────────┤
│ • db.py              │ • retrieve_history_by_last_   │
│   - SQLite 연결      │   n_prompt() → 포맷된 대화     │
│   - WAL + 마이그레이션│ • search_messages() → FTS5 /  │
│ • core.py            │   trigram / LIKE 라우팅       │
│   - add_messages     │ • _sanitize_fts5_query()      │
│   - 턴 범위 쿼리      │   쿼리 정화                    │
│   - 페이지네이션 히스토리│ • _decode_content()          │
│   - 세션 목록         │   JSON 콘텐츠 디코딩           │
└──────────────────────┴───────────────────────────────┘
```

### 패키지 익스포트 (`__init__.py`)

```python
# context_engine/__init__.py
from .store import *   # get_db, add_messages, get_messages_by_lastest_n_turns,
                       # get_turns_by_turn_num_scope, get_history_by_turn_page,
                       # get_session_ids, delete_messages_by_session
from .core import retrieve_history_by_last_n_prompt, search_messages
```

---

## 데이터 모델

### 데이터베이스 스키마

```sql
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_num      INTEGER NOT NULL,   -- 턴 번호 (add_messages 호출 1회 = 1턴)
    session_id    TEXT NOT NULL,      -- 세션 ID
    role          TEXT NOT NULL,      -- human / ai / tool
    content       TEXT,               -- 메시지 내용 (json.dumps, ensure_ascii=False)
    tool_call_id  TEXT,               -- 툴 호출 ID (tool 메시지)
    tool_calls    TEXT,               -- 툴 호출 상세, JSON (AI 메시지)
    tool_status   TEXT,               -- 툴 실행 상태 (기본 "success")
    tool_name     TEXT,               -- 툴 이름
    timestamp     TEXT NOT NULL,      -- 타임스탬프 YYYYMMDDHHmmss (동일 배치가 공유)
    finish_reason TEXT,               -- AI 응답 종료 사유
    reasoning     TEXT,               -- 사고 연쇄 (additional_kwargs["reasoning_content"])
    reasoning_content TEXT,           -- 추론 과정
    images        TEXT,               -- 이미지 경로 JSON 목록 (human 멀티모달 입력)
    audios        TEXT,               -- 오디오 경로/참조 JSON 목록
    videos        TEXT,               -- 비디오 경로/참조 JSON 목록
    model_name    TEXT,               -- AI 메시지: 응답을 생성한 모델
    input_tokens  INTEGER,            -- AI 메시지: usage_metadata 입력 토큰
    output_tokens INTEGER             -- AI 메시지: usage_metadata 출력 토큰
);
```

**인덱스:**

- `idx_messages_timestamp` — `(session_id, timestamp)`
- `idx_messages_turn_num` — `(session_id, turn_num)`

**FTS5 테이블** (둘 다 `content`, `tool_name`, `tool_calls`를 연결한 텍스트를 인덱싱):

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
```

**FTS5 트리거:** 각 FTS 테이블은 `messages` 테이블에 `AFTER INSERT` / `AFTER UPDATE` / `AFTER DELETE` 트리거를 두어 인덱스를 자동 동기화합니다. 따라서 행 삭제 시(예: `delete_messages_by_session`) 별도의 FTS 정리가 필요 없습니다.

**마이그레이션:** 스키마 생성은 `_migrations` 테이블로 버전 관리됩니다. 순서는 다음과 같습니다:
`build_messages_tb` → `build_messages_fts_tb` → `build_messages_fts_trigram_tb` → `add_images_column` → `add_audio_video_columns` → `add_model_token_columns`.

---

## 핵심 기능

### 1. 메시지 영속화

```python
from context_engine.store import add_messages

# 한 대화 턴을 영속화 (turn_num은 호출마다 자동 증가)
await add_messages("session_001", [user_msg, ai_msg])
```

- `add_messages` 호출 1회 = 1턴: 동일 배치의 메시지들은 같은 `turn_num`과 같은 `YYYYMMDDHHmmss` 타임스탬프를 공유합니다
- 요약 압축으로 생성된 `human` 메시지(`additional_kwargs["lc_source"] == "summarization"`으로 식별)는 필터링됩니다
- `ai` 메시지는 `tool_calls`(JSON), `additional_kwargs["reasoning_content"]`의 사고 연쇄(`reasoning` 컬럼에 저장), 응답/사용량 메타데이터의 `model_name` / `input_tokens` / `output_tokens`를 영속화합니다 (모두 선택적이며 없으면 `None`)
- `human` 메시지는 `additional_kwargs`의 멀티모달 파일 참조를 `images` / `audios` / `videos` 컬럼에 영속화합니다 (JSON 목록, 비어 있으면 `None`)
- `tool` 메시지는 `tool_call_id`, `tool_name`, `tool_status`(기본 `"success"`)를 영속화합니다

### 2. 히스토리 조회

```python
from context_engine import retrieve_history_by_last_n_prompt

# 최근 5턴을 가져와 prompt 문자열로 포맷
history = retrieve_history_by_last_n_prompt("session_001", n=5)
```

**출력 형식** (`core.py`에서 그대로 가져옴; 턴 본문에는 타임스탬프가 없음):

```
===== The following is the content of the last 5 turns (from oldest to newest, timestamp format: YYYYMMDDHHmmss) =====

<turn>
user: User message

agent: AI response
</turn>

...

===== The above is the content of the last 5 turns =====

```

`human` 메시지의 내용이 멀티모달 목록이면 첫 번째 `{"type": "text"}` 부분만 사용됩니다.

턴 범위 조회도 지원합니다:

```python
from context_engine.store import get_turns_by_turn_num_scope

# target_turn_num 전후 5턴씩 조회
rows = get_turns_by_turn_num_scope("session_001", target_turn_num=10, half_scope=5)
```

페이지네이션 히스토리 조회 (1페이지가 가장 최근 페이지):

```python
from context_engine.store import get_history_by_turn_page

# 1페이지를 페이지당 10턴으로 조회
rows = get_history_by_turn_page("session_001", min_turn_num=1, turn_page_size=10, turn_page_num=1)
```

턴 범위 조회와 페이지 조회 모두 턴이 최신순(새 것 먼저)으로 반환되며, JSON 인코딩된 `content`, `tool_calls`, `images`, `audios`, `videos` 컬럼은 Python 객체로 디코딩됩니다.

### 3. 전문 검색

```python
from context_engine import search_messages

# "Docker"가 포함된 메시지 검색 (컨텍스트 미리보기 포함)
results = search_messages(
    query="Docker",
    session_id="session_001",
    role_filter=["human", "ai"],
    limit=20,
    offset=0,
)

for r in results:
    print(r["snippet"])        # 하이라이트된 스니펫 (마커: >>> match <<<)
    print(r["context"])        # 최대 3개: 이전 메시지, 일치 메시지, 다음 메시지
```

**검색 특성:**

- **FTS5 테이블 2종**: `messages_fts`(기본 unicode61 토크나이저)와 `messages_fts_trigram`(trigram 토크나이저, CJK 부분 일치 지원)
- **자동 라우팅**: 비 CJK 쿼리는 `messages_fts`로. CJK 문자 총합이 3 이상이고 3자 미만 CJK 토큰이 없는 CJK 쿼리는 trigram 테이블로. 그 외는 LIKE로 폴백
- **토큰 단위 CJK 검사**: `广西 OR 桂林 OR 漓江` 같은 다중어 쿼리는 토큰 단위로 검사 — CJK 토큰 하나라도 3자 미만의 CJK 문자를 가지면 쿼리 전체가 LIKE로 라우팅됨 (trigram은 토큰당 3자 이상의 CJK 문자를 요구)
- **LIKE 폴백**: 연산자가 아닌 각 토큰에 대해 `content`, `tool_name`, `tool_calls`에 LIKE 조건을 하나씩 생성(`ESCAPE '\'` 사용), `timestamp DESC` 순 정렬. 스니펫은 첫 번째 토큰 출현 위치를 중심으로 한 120자 윈도우
- **쿼리 정화** (`_sanitize_fts5_query`): 쌍을 이루는 인용구는 유지하고, 쌍이 맞지 않는 FTS5 특수 문자를 제거하고, 연속된 `*`를 하나로 합치고, 불완전한 `AND`/`OR`/`NOT`을 제거하며, 하이픈/점/언더스코어가 포함된 단어(예: `my-app.config.ts`)는 인용부호로 감싸 FTS5가 구문으로 취급하게 합니다
- **Trigram 토큰 인용부호**: trigram 경로에서는 연산자가 아닌 각 토큰을 큰따옴표로 감싸고 부울 연산자(`AND`, `OR`, `NOT`)는 유지합니다
- **컨텍스트 확장**: 각 일치 항목에는 최대 3개의 컨텍스트 항목이 붙습니다 — 이전 메시지, 일치 메시지 자신, 다음 메시지(`timestamp`, 그다음 `id` 순 정렬). 각 항목은 `{"role": ..., "content": preview}`로 렌더링되며 preview는 200자로 잘립니다. 멀티모달 목록 콘텐츠는 텍스트 부분을 이어서 표시하고 텍스트가 없으면 `[multimodal content]`를 표시합니다
- **결과 축소**: 전체 `content` 필드는 결과에서 제거됩니다 (snippet과 context만 유지). 토큰 절약을 위함입니다
- **오류 내성**: 빈 쿼리/정화 후 빈 쿼리는 `[]`를 반환합니다. MATCH에서 발생한 FTS5 `sqlite3.OperationalError`는 무시되고 `[]`를 반환합니다
- **스레드 안전**: 모든 DB 접근은 모듈 수준 `threading.Lock`으로 보호됩니다
- **정렬**: FTS5 경로는 관련도 순(`ORDER BY rank`), LIKE 경로는 `timestamp DESC` 순

---

## 통합 지점

검증된 `context_engine` 패키지 사용처:

| 진입점 | 임포트 | 용도 |
|--------|--------|------|
| `agent/middlewares/context_engine/core.py` → `ContextEngineHook` | `add_messages` | 에이전트 미들웨어 (`agent/core.py`의 메인 에이전트에 등록). `aafter_agent`에서 마지막 턴을 잘라내고(`slice_last_turn`) 정화한 뒤(`sanitize_tool_use_result_pairing`) `add_messages()`로 영속화. 또한 시스템 프롬프트를 주입하고(`wrap_model_call`/`awrap_model_call`) 메모리/스킬 nudge 카운터(임계값 10)와 nudge 서브에이전트를 실행. 자세한 내용은 `agent/middlewares/README.md` 참조. |
| `agent/tools/message_search.py` → `message_search` 도구 | `get_db`, `search_messages`, `get_turns_by_turn_num_scope` | 세션 간 회상 도구: FTS5 검색(limit 50) → 일치 항목별 턴 범위 조회 → LLM 세션 요약. query가 없으면 최근 세션 메타데이터를 대신 반환 |
| `server/service/messages.py` | `get_session_ids`, `get_history_by_turn_page`, 그리고 (`context_engine.curator`의) `reset_idle_for_seconds` | 클라이언트용 세션 목록(최상위 세션 + 파생 제목), 페이지네이션 히스토리, 사용자 턴마다 curator 유휴 타이머 리셋 |
| `server/DAO/messages.py` | `delete_messages_by_session` | "세션 비우기" 작업 |
| `server/trigger/http/stats.py` | `get_db` (`context_engine.store.db`에서) | messages 테이블 기반 사용 통계 |
| `server/__main__.py` | `import context_engine.curator` | curator 패키지 임포트가 백그라운드 데몬 스레드를 시작 |

---

## Curator (스킬 유지보수 서브패키지)

`context_engine/curator/`는 **백그라운드 스킬 유지보수 오케스트레이터**로, 메시지 저장과는 무관합니다. 검증된 동작 요약:

- **범위**: `skills/auto/` 아래의 에이전트 생성 스킬만 대상. 내장 스킬은 절대 건드리지 않음
- **트리거**: `context_engine.curator`를 임포트하면 데몬 스레드(`curator-timer`)가 시작되어 3600초마다 `maybe_run_curator()`를 호출합니다. 실행은 `should_run_now()`가 참(활성화됨, 일시정지 아님, `interval_hours` 경과)이고 에이전트가 충분히 유휴 상태(`min_idle_hours`)일 때만 수행됩니다. 사용자 턴마다 `reset_idle_for_seconds()`가 호출됩니다 (`server/service/messages.py`)
- **라이프사이클**: `active → stale` (`stale_after_days`, 기본 30일간 활동 없음). `archive_after_days`(기본 90일)를 초과한 스킬은 디스크에서 제거됩니다. stale 구간 내에서 한 번도 사용되지 않은 스킬은 재활성화됩니다. pinned 스킬은 모든 전이를 우회합니다
- **LLM 통합** (`curator.yaml`로 옵트인, 기본 `consolidate: false`): 겹치는 좁은 스킬들을 LLM이 생성한 umbrella 스킬로 병합합니다
- **상태와 보고서**: 실행 상태는 `skills/.curator_state`에 저장. 보고서는 `logs/curator/{timestamp}/` 아래에 위치 (`run.json` + `REPORT.md`)

공개 API에는 `run_curator_review(on_summary=None, dry_run=False, consolidate=None)`, `maybe_run_curator(*, idle_for_seconds=None, on_summary=None)`, `reset_idle_for_seconds()`, `pin_skill(name)`, `unpin_skill(name)`, `delete_skill(name, absorbed_into="")`, `apply_automatic_transitions(now=None)`, `should_run_now(now=None)`이 포함됩니다.

▶️ 전체 문서: [curator/README.md](curator/README.md) · [中文](curator/README.zh.md) · [한국어](curator/README.ko.md) · [日本語](curator/README.ja.md)

---

## API 레퍼런스

아래 시그니처는 소스에서 그대로 복사한 것이며, 레이어별 임포트 경로를 함께 표기했습니다.

### 비즈니스 레이어 (`context_engine.core`, 패키지 수준에서 재익스포트)

#### `retrieve_history_by_last_n_prompt(session_id: str, n: int = 5) -> str`
최근 `n`턴을 prompt 문자열로 포맷합니다 (출력 형식은 위 참조).

| 매개변수 | 타입 | 설명 |
|----------|------|------|
| `session_id` | `str` | 세션 ID |
| `n` | `int` | 턴 수 (기본 5) |

**반환:** `str` — 포맷된 대화 히스토리

---

#### `search_messages(query: str, session_id: str, role_filter: list[str] = None, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]`
메시지를 전문 검색합니다.

| 매개변수 | 타입 | 설명 |
|----------|------|------|
| `query` | `str` | 검색 쿼리 (빈 값 → `[]`) |
| `session_id` | `str` | 세션 ID |
| `role_filter` | `list[str]` | 역할 필터 (예: `["human", "ai"]`; 기본 `None`) |
| `limit` | `int` | 최대 결과 수 (기본 20) |
| `offset` | `int` | 오프셋 (기본 0) |

**반환:** `list[dict[str, Any]]` — 각 결과는 `id`, `session_id`, `turn_num`, `role`, `snippet`, `timestamp`, `tool_name`, `context`를 포함합니다 (전체 `content` 필드는 제거됨)

---

#### `_sanitize_fts5_query(query: str) -> str` (내부)
사용자 입력을 안전한 FTS5 MATCH 쿼리로 정화합니다.

#### `_decode_content(content: Any) -> Any` (내부)
`\x00json:` 접두사가 붙은 메시지 콘텐츠 문자열을 디코딩합니다. 다른 값은 그대로 반환합니다.

---

### 스토어 레이어 (`context_engine.store`)

#### `async add_messages(session_id: str, messages: list[BaseMessage]) -> None`
LangChain 메시지 배치를 새 턴 하나로 영속화합니다.

| 매개변수 | 타입 | 설명 |
|----------|------|------|
| `session_id` | `str` | 세션 ID |
| `messages` | `list[BaseMessage]` | LangChain `BaseMessage` 목록 (`human` / `ai` / `tool`) |

---

#### `get_messages_by_lastest_n_turns(session_id: str, last_n: int = 5) -> list[dict]`
최근 `last_n`턴의 메시지 행을 가져옵니다 (내부적으로 `get_history_by_turn_page`의 1페이지에 위임).

| 매개변수 | 타입 | 설명 |
|----------|------|------|
| `session_id` | `str` | 세션 ID |
| `last_n` | `int` | 턴 수 (기본 5) |

**반환:** `list[dict]` — 메시지 행, 턴 최신순, JSON 컬럼 디코딩됨

---

#### `get_turns_by_turn_num_scope(session_id: str, target_turn_num: int, half_scope: int = 5) -> list[dict]`
대상 턴 번호 주변의 턴 범위에 있는 메시지를 가져옵니다 (범위는 `[1, max_turn_num]`으로 클램프됨).

| 매개변수 | 타입 | 설명 |
|----------|------|------|
| `session_id` | `str` | 세션 ID |
| `target_turn_num` | `int` | 대상 턴 번호 |
| `half_scope` | `int` | 앞뒤 각각의 턴 수 (기본 5) |

**반환:** `list[dict]` — 메시지 행, 턴 최신순, JSON 컬럼 디코딩됨

---

#### `get_history_by_turn_page(session_id: str, min_turn_num: Annotated[int, Field(ge=1)] = 1, turn_page_size: Annotated[int, Field(ge=1)] = 10, turn_page_num: Annotated[int, Field(ge=1)] = 1) -> list[dict]`
최신 턴부터 거꾸로 턴 번호 단위로 페이지네이션된 히스토리를 가져옵니다 (`@validate_call` 데코레이터 적용).

| 매개변수 | 타입 | 설명 |
|----------|------|------|
| `session_id` | `str` | 세션 ID |
| `min_turn_num` | `int` | `turn_num`의 포함 하한 (≥1, 기본 1) |
| `turn_page_size` | `int` | 페이지당 턴 수 (≥1, 기본 10) |
| `turn_page_num` | `int` | 최신 턴부터 거꾸로 센 1 시작 페이지 번호 (≥1, 기본 1) |

**반환:** `list[dict]` — 메시지 행, 턴 최신순, JSON 컬럼 디코딩됨

---

#### `get_max_turn_num(session_id: str) -> int`
세션의 최대 `turn_num`. 메시지가 없으면 `0`. `context_engine/store/core.py`에 정의되어 있습니다 (`context_engine.store`에서 재익스포트되지 않음).

---

#### `delete_messages_by_session(session_id: str) -> int`
세션의 모든 메시지를 삭제합니다. FTS5 인덱스는 트리거가 자동으로 정리합니다.

**반환:** `int` — 삭제된 행 수

---

#### `get_session_ids() -> list[dict]`
서로 다른 최상위 세션을 모두 나열합니다 (`:subagent:`가 포함된 서브에이전트 세션은 제외). 최근 활동순 정렬.

**반환:** `list[dict]` — 각 항목은 `{"session_id": str, "last_time": str, "title": str}`. `last_time`은 최신 `YYYYMMDDHHmmss` 타임스탬프이고, `title`은 최신 `human` 메시지에서 파생됨 (빈 문자열일 수 있음)

---

#### `get_db()` (`context_engine.store.db`)
공유 `sqlite3.Connection`을 반환합니다 (첫 호출 시 생성. `check_same_thread=False`, `timeout=1.0`, `isolation_level=None`, `row_factory=sqlite3.Row`, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`).

---

## FAQ

### Q1: MesMemory와 Curator의 관계는?

같은 패키지에 있지만 런타임에서는 서로 무관합니다: MesMemory는 원시 세션 메시지의 저장·조회(단기 메모리)를 담당하고, Curator는 `skills/auto/` 아래 에이전트 생성 스킬의 유지보수(라이프사이클 전이, 통합, 정리)를 담당합니다. Curator는 `messages` 테이블을 읽거나 쓰지 않습니다.

---

### Q2: FTS5 테이블이 두 개인 이유는?

`messages_fts`는 기본 unicode61 토크나이저를 사용하여 영어식 토큰 매칭에 적합합니다. `messages_fts_trigram`은 trigram 토크나이저를 사용하여 텍스트를 3-gram 부분 문자열로 분할하므로 CJK 부분 일치가 가능합니다 (unicode61은 CJK 텍스트를 글자 단위로 분리하여 오탐을 유발합니다). 라우터는 쿼리의 CJK 내용과 토큰 길이에 따라 테이블을 선택합니다.

---

### Q3: 검색 결과의 `snippet`과 `content`의 차이는?

FTS5 경로에서 `snippet`은 일치 부분을 `>>>` / `<<<` 하이라이트 마커로 감싼 FTS5 발췌(40토큰 윈도우)입니다. LIKE 경로에서 `snippet`은 첫 번째 토큰 출현 위치를 중심으로 한 `content`의 120자 슬라이스(마커 없음)입니다. 토큰 절약을 위해 전체 `content` 필드는 모든 결과에서 제거됩니다. 전체 내용이 필요하면 `get_messages_by_lastest_n_turns` / `get_history_by_turn_page`를 사용하세요.

---

### Q4: 토큰 단위 CJK 라우팅은 어떻게 작동하나요?

CJK 쿼리의 경우 연산자가 아닌 각 토큰이 개별적으로 검사됩니다. CJK 토큰 하나라도 3자 미만의 CJK 문자를 가지면 trigram FTS5가 매칭할 수 없어(토큰당 3자 이상의 CJK 문자 요구) 쿼리 전체가 LIKE 검색으로 폴백합니다. 이를 통해 `"广西 OR 桂林 OR 漓江"`처럼 각 단어가 CJK 2자뿐인 경우(CJK 문자 총합은 6)도 올바르게 처리됩니다.

---

## 기술 스택

| 구성 요소 | 기술 |
|-----------|------|
| **데이터베이스** | SQLite 3 — WAL 모드, `foreign_keys=ON`, 단일 공유 연결 (`check_same_thread=False`, `timeout=1.0`) |
| **전문 검색** | FTS5 (unicode61) + FTS5 (trigram 토크나이저) |
| **메시지 모델** | LangChain `BaseMessage` |
| **유효성 검사** | Pydantic `@validate_call` (`get_history_by_turn_page`에 사용) |
| **동시성 제어** | 모든 DB 접근을 `threading.Lock`으로 보호 |
| **저장 경로** | `src/store/mes_memory/mes_memory.db` (`config.path.SRC_DIR / "store/mes_memory/mes_memory.db"`) |

---

## 라이선스

이 프로젝트는 EMA AI Agent의 오픈소스 라이선스를 따릅니다.

---

**마지막 업데이트:** 2026-08-29

# MesMemory — 세션 메시지 메모리 시스템

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **MesMemory**는 EMA AI Agent의 단기 대화 메모리 엔진으로, 메시지 영속화(지속 저장), 히스토리 검색, 전문(full-text) 검색을 담당합니다.

---

## 목차

- [개요](#개요)
- [아키텍처](#아키텍처)
- [데이터 모델](#데이터-모델)
- [핵심 기능](#핵심-기능)
- [API 참조](#api-참조)
- [FAQ](#faq)

---

## 개요

### 설계 위치

MesMemory는 [Skill Memory](../skill_memory/README.md)를 보완합니다:

| Skill Memory | MesMemory |
|-------------|-----------|
| 장기 지식 그래프 (TASK/SKILL/EVENT) | 단기 세션 메시지 저장 |
| 구조화된 삼중항, 세션 간 재사용 | 원시 메시지 시퀀스, 세션별 격리 |
| 그래프 커뮤니티 + PageRank 회수 | FTS5 전문 검색 + 턴 범위 쿼리 |
| 비동기 백그라운드 추출 | 동기 쓰기, 즉시 영속화 |

### 핵심 기능

1. **메시지 영속화** — 각 대화 턴의 human/ai/tool 메시지를 SQLite에 기록
2. **히스토리 검색** — 최근 N턴, 페이지네이션 히스토리 또는 특정 턴 범위를 포맷된 컨텍스트로 가져오기
3. **전문 검색** — FTS5 기반 대화 검색, 중국어 지원(trigram) 및 컨텍스트 미리보기

---

## 아키텍처

```
┌────────────────────────────────────────────────────┐
│                   context_engine                     │
├───────────────────┬────────────────────────────────┤
│    store/         │          core.py                │
│   (데이터 계층)    │      (비즈니스 로직)            │
├───────────────────┼────────────────────────────────┤
│ • db.py           │ • retrieve_history_by_last_n   │
│   - SQLite 연결    │   _prompt() → 포맷된            │
│   - 마이그레이션   │   대화 문자열                    │
│ • core.py         │ • search_messages() → FTS5     │
│   - CRUD 연산      │   검색 + 컨텍스트               │
│   - 메시지 쓰기    │ • _sanitize_fts5_query()     │
│   - 턴 쿼리        │   쿼리 정화(sanitization)      │
│   - 페이지네이션   │ • _decode_content()          │
│     히스토리       │   JSON 콘텐츠 디코딩            │
└───────────────────┴────────────────────────────────┘
```

### 저장 계층 (`store/`)

| 파일 | 책임 |
|------|------|
| `store/db.py` | SQLite 연결 관리, WAL 모드, 자동 마이그레이션 (테이블, 인덱스, FTS5 트리거) |
| `store/core.py` | 메시지 CRUD: `add_messages`, `get_messages_by_lastest_n_turns`, `get_turns_by_turn_num_scope`, `get_history_by_page`, `get_max_turn_num` |

### 비즈니스 계층 (`core.py`)

| 함수 | 책임 |
|------|------|
| `retrieve_history_by_last_n_prompt(session_id, n)` | 최근 N턴을 가져와 프롬프트 컨텍스트로 포맷 |
| `search_messages(query, session_id, ...)` | FTS5 전문 검색, 중국어 trigram 지원 및 컨텍스트 확장 |
| `_sanitize_fts5_query(query)` | 안전한 FTS5 MATCH 쿼리를 위한 사용자 입력 정화 (내부) |
| `_decode_content(content)` | JSON 인코딩된 메시지 콘텐츠 역변환 (내부) |

### 패키지 내보내기 (`__init__.py`)

```python
# context_engine/__init__.py
from .store import *                                              # get_db, add_messages, get_messages_by_lastest_n_turns, get_turns_by_turn_num_scope, get_history_by_page
from .core import retrieve_history_by_last_n_prompt, search_messages
```

---

## 데이터 모델

### 데이터베이스 스키마

```sql
-- Messages 테이블
CREATE TABLE messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_num      INTEGER NOT NULL,       -- 턴 시퀀스 번호
    session_id    TEXT NOT NULL,           -- 세션 ID
    role          TEXT NOT NULL,           -- human / ai / tool
    content       TEXT,                   -- 메시지 콘텐츠 (JSON 인코딩)
    tool_call_id  TEXT,                   -- 툴 호출 ID
    tool_calls    TEXT,                   -- 툴 호출 상세 (JSON)
    tool_status   TEXT,                   -- 툴 실행 상태
    tool_name     TEXT,                   -- 툴 이름
    timestamp     TEXT NOT NULL,          -- 타임스탬프 (YYYYMMDDHHmmss)
    finish_reason TEXT,                   -- AI 응답 종료 사유
    reasoning     TEXT,                   -- 추론 콘텐츠
    reasoning_content TEXT                -- 추론 과정
);

-- FTS5 전문 검색 (영어 우선)
CREATE VIRTUAL TABLE messages_fts USING fts5(content);

-- FTS5 중국어 trigram 검색
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
```

**인덱스:**
- `idx_messages_timestamp` — `(session_id, timestamp)` 빠른 시간 범위 쿼리용
- `idx_messages_turn_num` — `(session_id, turn_num)` 턴 기반 쿼리용

**FTS5 트리거:** `messages` 테이블의 INSERT/UPDATE/DELETE 시 FTS5 인덱스를 자동 동기화합니다. 인덱스된 필드는 `content`, `tool_name`, `tool_calls`를 포함합니다.

---

## 핵심 기능

### 1. 메시지 영속화

```python
from context_engine.store import add_messages

# 대화 턴 기록 (turn_num 자동 증가)
await add_messages("session_001", [user_msg, ai_msg])
```

- 세 가지 역할(human/ai/tool)의 메시지가 모두 영속화됩니다
- 압축에서 비롯된 human 메시지(`lc_source == "summarization"`로 식별)는 필터링됩니다
- 각 메시지는 `YYYYMMDDHHmmss` 타임스탬프를 갖습니다
- 콘텐츠는 구조화 데이터를 위한 `\x00json:` 접두사로 JSON 인코딩됩니다

---

### 2. 히스토리 검색

```python
from context_engine import retrieve_history_by_last_n_prompt

# 최근 5턴을 가져와 프롬프트 문자열로 포맷
history = retrieve_history_by_last_n_prompt("session_001", n=5)
```

**출력 형식:**

```
===== The following is the content of the last 5 turns (from oldest to newest, timestamp format: YYYYMMDDHHmmss) =====

<turn>
User: User message

Assistant: AI response
</turn>

...

===== The above is the content of the last 5 turns =====
```

턴 범위 쿼리도 지원됩니다:

```python
from context_engine.store import get_turns_by_turn_num_scope

# target_turn_num 전후 각 5턴 가져오기
rows = get_turns_by_turn_num_scope("session_001", target_turn_num=10, half_scope=5)
```

페이지네이션 히스토리 검색:

```python
from context_engine.store import get_history_by_page

# 페이지당 10턴, 1페이지 가져오기
rows = get_history_by_page("session_001", min_turn_num=1, turn_page_size=10, turn_page_num=1)
```

---

### 3. 전문 검색

```python
from context_engine import search_messages

# "Docker" 포함 메시지 검색, 컨텍스트 미리보기 포함
results = search_messages(
    query="Docker",
    session_id="session_001",
    role_filter=["human", "ai"],
    limit=20,
    offset=0,
)

for r in results:
    print(r["snippet"])        # 하이라이트 스니펫
    print(r["context"])        # 전후 1개 메시지 컨텍스트
```

**검색 기능:**

- **듀얼 FTS5 테이블**: `messages_fts` (기본 unicode61 토크나이저) 및 `messages_fts_trigram` (trigram 토크나이저, 중국어 지원)
- **자동 라우팅**: 중국어 쿼리 감지(토큰당 CJK 3자 이상) → trigram 경로; 그 외 → 기본 FTS5
- **우아한 대체**: 짧은 중국어 쿼리(토큰당 CJK 3자 미만)는 LIKE 검색으로 대체
- **토큰별 CJK 검사**: "广西 OR 桂林 OR 漓江" 같은 다중 용어 쿼리는 토큰별로 검사 — CJK 토큰이 3자 미만이면 전체 쿼리가 LIKE로 경로 지정
- **쿼리 정화**: FTS5 특수 문자, 인용 부호 균형, 불리언 연산자 정리, 하이픈/점 용어 인용 자동 처리
- **컨텍스트 확장**: 각 결과는 전후 1개 메시지 컨텍스트를 포함
- **멀티모달 친화적**: 비텍스트 콘텐츠(예: 이미지)는 `[multimodal content]`로 표시
- **토큰 효율성**: 결과는 전체 `content` 필드를 생략(snippet + context만)
- **스레드 안전성**: 모든 DB 연산은 threading lock으로 보호

---

## API 참조

### `retrieve_history_by_last_n_prompt(session_id, n=5)`
최근 N턴을 가져와 프롬프트 문자열로 포맷합니다.

| 매개변수 | 타입 | 설명 |
|-----------|------|-------------|
| `session_id` | `str` | 세션 ID |
| `n` | `int` | 턴 수 (기본값: 5) |

**반환:** `str` — 포맷된 대화 히스토리

---

### `search_messages(query, session_id, role_filter=None, limit=20, offset=0)`
메시지 전문 검색.

| 매개변수 | 타입 | 설명 |
|-----------|------|-------------|
| `query` | `str` | 검색 쿼리 |
| `session_id` | `str` | 세션 ID |
| `role_filter` | `list[str]` | 역할 필터 (예: `["human", "ai"]`) |
| `limit` | `int` | 최대 결과 수 (기본값: 20) |
| `offset` | `int` | 오프셋 (기본값: 0) |

**반환:** `list[dict]` — 각 결과는 `id`, `session_id`, `turn_num`, `role`, `snippet`, `timestamp`, `tool_name`, `context`를 포함

---

### `add_messages(session_id, messages)`
(저장 계층) 데이터베이스에 메시지 기록.

| 매개변수 | 타입 | 설명 |
|-----------|------|-------------|
| `session_id` | `str` | 세션 ID |
| `messages` | `list[BaseMessage]` | LangChain BaseMessage 목록 |

---

### `get_messages_by_lastest_n_turns(session_id, last_n=5)`
저장 계층에서 최근 N턴의 원시 메시지 레코드를 가져옵니다.

| 매개변수 | 타입 | 설명 |
|-----------|------|-------------|
| `session_id` | `str` | 세션 ID |
| `last_n` | `int` | 턴 수 (기본값: 5) |

**반환:** `list[dict]` — 각 레코드는 모든 메시지 필드를 포함

---

### `get_turns_by_turn_num_scope(session_id, target_turn_num, half_scope=5)`
대상 턴 번호 주변의 턴 범위 내 메시지를 가져옵니다.

| 매개변수 | 타입 | 설명 |
|-----------|------|-------------|
| `session_id` | `str` | 세션 ID |
| `target_turn_num` | `int` | 대상 턴 번호 |
| `half_scope` | `int` | 각 측 턴 수 (기본값: 5) |

**반환:** `list[dict]` — 각 레코드는 디코딩된 JSON을 포함한 모든 메시지 필드를 포함

---

### `get_history_by_page(session_id, min_turn_num=1, turn_page_size=10, turn_page_num=1)`
페이지네이션된 히스토리 메시지 가져오기.

| 매개변수 | 타입 | 설명 |
|-----------|------|-------------|
| `session_id` | `str` | 세션 ID |
| `min_turn_num` | `int` | 최소 턴 번호 (≥1, 기본값: 1) |
| `turn_page_size` | `int` | 페이지당 턴 수 (≥1, 기본값: 10) |
| `turn_page_num` | `int` | 페이지 번호 (≥1, 기본값: 1) |

**반환:** `list[dict]` — 각 레코드는 디코딩된 JSON을 포함한 모든 메시지 필드를 포함

---

### `get_max_turn_num(session_id)`
세션의 최대 턴 번호를 가져옵니다.

| 매개변수 | 타입 | 설명 |
|-----------|------|-------------|
| `session_id` | `str` | 세션 ID |

**반환:** `int` — 최대 턴 번호, 메시지가 없으면 0

---

## FAQ

### Q1: MesMemory와 Skill Memory의 관계는 무엇인가요?

MesMemory는 **원시 메시지 저장 및 검색**(단기 기억)을 처리합니다. Skill Memory는 **지식 추출 및 그래프 구축**(장기 기억)을 처리합니다. MesMemory는 "말한 내용"을 저장하고, Skill Memory는 말한 내용에서 추출된 구조화된 지식을 저장합니다.

---

### Q2: FTS5 테이블이 두 개인 이유는 무엇인가요?

`messages_fts`는 기본 unicode61 토크나이저를 사용하여 영어 및 병음 검색에 적합합니다. `messages_fts_trigram`은 trigram 토크나이저를 사용하여 텍스트를 3-gram 부분 문자열로 분할하므로 중국어 퍼지 매칭과 부분 문자열 검색을 자연스럽게 지원합니다. 시스템은 쿼리 언어에 따라 자동 선택합니다.

---

### Q3: 검색 결과의 `snippet`과 `content`의 차이는 무엇인가요?

`snippet`은 FTS5가 제공하는 짧은 발췌문으로, 하이라이트 마커가 있으며(각 측 약 40자) 매칭 위치의 빠른 미리보기에 사용됩니다. `content`는 전체 메시지 본문이지만 토큰 절약을 위해 검색 결과에서 생략됩니다. 전체 콘텐츠가 필요하면 `get_messages_by_lastest_n_turns`를 대신 사용하세요.

---

### Q4: 토큰별 CJK 라우팅은 어떻게 동작하나요?

CJK 쿼리의 경우 시스템은 각 비연산자 토큰을 개별적으로 검사합니다. 어떤 CJK 토큰이라도 CJK 문자가 3자 미만이면 trigram FTS5가 매칭할 수 없으므로(토큰당 CJK 3자 이상 필요) 전체 쿼리가 LIKE 검색으로 대체됩니다. 이는 각 용어가 CJK 문자 2자뿐인 `"广西 OR 桂林 OR 漓江"` 같은 경우를 처리합니다.

---

## 기술 스택

| 구성 요소 | 기술 |
|-----------|-----------|
| **데이터베이스** | SQLite 3 + WAL 모드 |
| **전문 검색** | FTS5 + Trigram 토크나이저 |
| **프레임워크** | LangChain BaseMessage |
| **검증** | Pydantic `@validate_call` |
| **저장 경로** | `store/mes_memory/mes_memory.db` |

---

## 라이선스

이 프로젝트는 EMA AI Agent의 오픈소스 라이선스를 따릅니다.

---

**마지막 업데이트:** 2026-07-09

# 🧠 LLM Wiki 그래프 계층

[**English**](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> `llm_wiki` 스킬의 설계 문서: 명시적 그래프, 멀티모달 ingest, 그래프 알고리즘
> 쿼리가 markdown 지식 베이스를 바꾸지 않고 어떻게 공존하는지.

정보 출처: `skills/builtin/llm_wiki/scripts/{core,ingest,search,graph,graph_query}.py`,
`skills/builtin/llm_wiki/SKILL.md`,
`config/features/infra_side/skills_tooling.py`.

## markdown이 유일한 정보 출처

wiki는 상호 링크된 markdown 파일 집합 그대로입니다. 그래프는 **파생**됩니다:
`scripts/graph.py`가 `src/data/wiki/.wiki_graph.db`(버려도 되고 markdown에서
재구축)에 `wiki_nodes`, `wiki_edges`, `wiki_graph_meta` 세 테이블을 씁니다.
`save_source`, `search_wiki`, `init_wiki`, `lint_wiki`의 시그니처는 그대로이며(L1)
그래프는 추가 계층입니다.

| 간선 출처 | `edge_type` | confidence |
|-----------|-------------|------------|
| `[[Wikilink]]` | `wikilink` | `EXTRACTED` |
| `[[Target|INFERRED]]` | `wikilink` | `INFERRED` |
| `[inline link](...)` | `inline_link` | `EXTRACTED` |
| `` `code span` `` 언급 | `mention` | `EXTRACTED` |
| frontmatter `extracted_relations` | 해당 관계의 `type` | 해당 관계의 `confidence` |

`build_wiki_graph(force=False)`는 파일 `mtime` 기준으로 증분합니다: 변경되지 않은
파일은 건너뛰고, 변경된 파일의 간선은 교체하며, 삭제된 파일은 노드와 인접한 모든
간선을 제거합니다. `|EXTRACTED`는 명시할 수 있고 접미사는 선택입니다(일반
wikilink는 EXTRACTED). 선택적 `extracted_entities`는 노드를 추가하고
`extracted_relations`는 타입이 있는 간선을 추가합니다.

## 멀티모달 ingest

`scripts/ingest.py::save_binary_source(content_bytes, content_type, ...)`는
`save_source`(텍스트 전용, 그대로) 옆의 추가 바이너리 경로입니다:

| `content_type` | 추출기 | `raw/` 범주 |
|----------------|--------|-------------|
| `pdf` | pypdf → pdftotext → mineru | `papers` |
| `image` / `png` / `jpg` | `image_to_text`(`itt`) | `assets` |
| `audio` / `mp3` / `wav` | `speech_to_text`(`stt`) | `transcripts` |
| `video` / `mp4` / `mov` | `video_text_to_text`(`vtt`) | `transcripts` |
| `docx` | 표준 라이브러리 ZIP + `word/document.xml` | `articles` |
| `xlsx` | 표준 라이브러리 ZIP + sharedStrings/sheet XML | `articles` |
| `md` / `txt` | 디코드 | `articles` |

auxiliary-LLM 의미 추출이 entities/relations를 뽑고(fail-open——provider 실패 시
빈 목록), 추가 frontmatter가 붙은 markdown 소스를 쓴 뒤 그래프를 갱신합니다.
`pypdf` / `python-docx` / `openpyxl`이 없어도 호출은 실패하지 않습니다: 표준
라이브러리 리더와 pdftotext/mineru 체인이 대신 퇴화합니다(L5).

## 그래프 쿼리

`scripts/graph_query.py`가 읽기 쪽을 노출합니다:

| 함수 | 반환 |
|------|------|
| `path(a, b, max_depth=5)` | 최단 무방향 경로 + 각 홉의 간선 유형/confidence |
| `god_nodes(limit=10)` | 차수 순 노드(순수 SQL, 선택 의존성 없음) |
| `explain(name)` | 이웃, 방향, 간선 유형, confidence, 출처 파일 |
| `communities()` | networkx Louvain 클러스터; networkx가 없으면 연결 요소 |
| `query_graph(question)` | ASCII 단어 + CJK 바이그램 → 노드 검색 → 1홉 부분 그래프 |

`search_wiki(keyword)`는 여전히 grep 경로이며 리터럴 문자열에 적합합니다. 관계를
묻는 질문에는 `query_graph`를 권장합니다. 토큰화는 의존성이 없고(jieba 불필요),
wiki 그래프는 code_intel / graph_rag와 의도적으로 합치지 않습니다——각 그래프는
자기 저장소와 쿼리 언어에 최적화되어 있습니다.

## lint 그래프 지표

`lint_wiki()`는 기존 보고서를 유지하고 `graph` 블록을 덧붙입니다: 연결성
(`components`, `largest_component`, `isolated_nodes`), `EXTRACTED` / `INFERRED`
비율, `low_confidence_pages`(추론 간선이 `wiki_lint_low_confidence_ratio` 이상인
페이지), `god_nodes` 상위 10. 그래프 절은 fail-open이라 구축 실패 시
`available: false`를 보고하고 기존 10개 검사를 깨지 않습니다(L7).

## 테스트

| 범위 | 테스트 |
|------|--------|
| 그래프 구축, confidence 태그, 증분 재구축 | `tests/skills/builtin/llm_wiki/test_graph_build.py` |
| path, god nodes, explain, communities, 자연어 쿼리 | `tests/skills/builtin/llm_wiki/test_graph_query.py` |
| 바이너리 ingest와 office 리더 | `tests/skills/builtin/llm_wiki/test_binary_ingest.py` |
| lint 그래프 지표 | `tests/skills/builtin/llm_wiki/test_lint_graph.py` |

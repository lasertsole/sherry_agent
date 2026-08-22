# 🌐 멀티모달 RAG 스킬

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> EMA 내장 스킬: 멀티모달 파일/폴더를 개인 지식 그래프에 인덱싱하고, **멀티홉 그래프 검색**으로 질문에 답합니다.

## ✨ 개요

`multimodal_rag`는 [EMA AI Agent](https://github.com/your-repo/EMA_AI_agent)([최상위 README](../../../../README.md) 참조)의 내장 핵심 스킬입니다. 자체 호스팅되는 개인 RAG 지식 베이스를 제공합니다:

- **멀티모달 수집**: 다양한 형식의 문서(TXT / Markdown / PDF 등)를 활성화된 파서로 인덱싱합니다.
- **지식 그래프**: 플랫한 벡터 청크 대신 문서로부터 엔티티–관계 그래프를 구축합니다.
- **멀티홉 검색**: 여러 문서/엔티티를 오가는 추론이 필요한 복잡한 질문에 답합니다.
- **네이티브 스토리지**: 전체 검색 스택은 완전히 벤더되어 있습니다 — RAG-Anything 파이프라인과 그 스토리지 계층이 [SNKV 백엔드와 완전히 융합](scripts/graph_rag/vendored_lightrag/README.md)된 LightRAG 엔진입니다.

---

## 🔧 설치

이 스킬은 EMA 에이전트와 함께 배포되므로 별도 설치는 필요 없습니다. RAG 엔진(`graph_rag`)은 `scripts/graph_rag/` 아래에 완전히 벤더되어 두 부분으로 구성됩니다 — RAG-Anything 파이프라인인 `vendored_raganything`과 저장소가 SNKV와 융합된 LightRAG 엔진인 `vendored_lightrag` — 두 부분 모두 `graph_rag/__init__.py`에 의해 짧은 임포트 이름으로 별칭 처리되므로 PIP 설치가 필요 없습니다. 나머지 런타임 의존성(SNKV, mineru 파서, 임베딩 모델)은 EMA 프로젝트 환경에서 해석됩니다.

---

## ▶️ 사용법

### Python API

```python
import asyncio
from skills.builtin.core.multimodal_rag.scripts import folder_index, file_index, query

async def main():
    # 1) 전체 폴더를 지식 그래프 카테고리로 인덱싱
    await folder_index("/path/to/documents", "my_docs")

    # 2) 또는 단일 파일 인덱싱
    # await file_index("/path/to/a/paper.pdf", "papers")

    # 3) 그래프 질의 (멀티홉 검색)
    answer = await query("거학력과 원야한나 사이의 관계는 무엇인가요?")
    print(answer)

asyncio.run(main())
```

### 커맨드라인

```bash
# 폴더 인덱싱
python rag_index.py "/path/to/documents" my_docs

# 단일 파일 인덱싱
python rag_index.py "/path/to/a/paper.pdf" papers

# 지식 그래프 질의
python rag_query.py "거학력과 원야한나 사이의 관계는 무엇인가요?"

# 다중 형식 샘플 문서(txt/md/pdf)를 가져와 지식 그래프 페이지 검증
python rag_import_test.py
```

### RAG 파이프라인 관련 참고

- `get_rag_anything()`은 벤더한 LightRAG 엔진 기반의 `RAGAnything` 인스턴스를 생성합니다.
- 인덱스 출력은 `src/rag/graph_rag/<classify_folder>/output/`에 기록됩니다.
- `parse_method="auto"`는 엔진이 파일 유형별 최적 파서(예: PDF의 `mineru`)를 자동 선택하게 합니다. `parser="mineru"`는 PDF 파이프라인을 강제하고, `backend="pipeline"`은 CPU에서 VLM 콜드 로드를 피합니다.

---

## 📝 공개 API

| 함수 | 시그니처 | 설명 |
| :--- | :--- | :--- |
| `folder_index` | `(input_folder_path: str, classify_folder: str) -> str` | 폴더의 모든 파일을 카테고리로 인덱싱 |
| `file_index` | `(input_file_path: str, classify_folder: str) -> None` | 단일 파일을 카테고리로 인덱싱 |
| `query` | `(question: str) -> str` | 지식 그래프에 질문 (멀티홉) |

세 함수 모두 `scripts/__init__.py`에서 내보냅니다.

---

## 🧪 테스트

```bash
pytest skills/builtin/core/multimodal_rag/tests/ -q
```

테스트 스위트는 벤더한 `graph_rag` 컴포넌트가 SNKV 스토리지 백엔드로 초기화되고, 벤더 패키지가 짧은 표준 임포트 식별자 `graph_rag`으로 올바르게 로드되는지 검증합니다.

---

## 🗂️ 프로젝트 구조

```text
multimodal_rag/
├── SKILL.md                    # 스킬 매니페스트 (이름, 설명, 사용 예시)
├── README.md                   # English 문서 (이 파일)
├── README.zh.md                # 中文
├── README.ko.md                # 한국어
├── README.ja.md                # 日本語
├── scripts/
│   ├── __init__.py             # query, folder_index, file_index 내보내기
│   ├── rag_index.py            # folder_index / file_index
│   ├── rag_query.py            # query
│   ├── rag_import_test.py      # 다중 형식 샘플 가져오기
│   ├── rag_import_pdf_test.py  # mineru 파서를 통한 실제 PDF 가져오기
│   └── graph_rag/           # 완전히 벤더된 RAG 엔진
│       ├── vendored_raganything/  # 벤더된 RAG-Anything 파이프라인
│       ├── vendored_lightrag/  # 벤더된 LightRAG, SNKV와 융합된 스토리지
│       └── ...
└── tests/
    └── test_rag_anything_vendored.py
```

---

## 📄 라이선스

MIT — [프로젝트 최상위 라이선스](../../../../LICENSE) 참조.

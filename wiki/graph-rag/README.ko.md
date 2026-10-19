# GraphRAG 경로 쿼리

`multimodal_rag` 스킬의 LightRAG 엔진은 엔티티와 관계를 벡터 유사도로 검색한 뒤
멀티홉 연결을 LLM에 맡깁니다. 이 페이지는 그 검색 위에 추가한 명시적 경로 계층,
함께 적용한 검색 임계값 조정, 그리고 스파스 검색에 대한 조사 결론(변경 전후
실측 수치 포함)을 기록합니다.

## 빠져 있던 것

`vendored_lightrag`에는 경로 알고리즘이 없었습니다:
`query_nodes_by_vector`와 관계 검색은 각각 한 번의 벡터 검색(기본
`top_k=40`)만 수행하고 반환합니다. 중간 연결 엔티티가 top-k 밖으로 밀리면
그래프에 체인이 존재해도 "A와 B의 관계는?"을 탐색으로 답할 수 없습니다. 이른바
"멀티홉"은 후보 집합 위에서 LLM이 추론하는 확률적 사건이었습니다.

엔티티 벡터는 순수하게 밀집형(`BGE-M3`, 1024 차원)입니다. vendored 엔진에는
스파스 채널도, BM25 폴백도 없습니다.

## 경로 계층

`skills/builtin/core/multimodal_rag/scripts/graph_rag/path_search.py`는 스킬
계층에 있습니다(vendored 트리에는 `operate.py`의 디스패치 훅만 추가):

- **끝점 해석** — 비용이 낮은 순서로 3단계: 그래프 노드 정확한 이름 → 그래프
  레이블 포함 일치(`Kepler-7 launch pad` → `Kepler-7 Launch Pad`) →
  `compute_mdhash_id(..., prefix="ent-")`를 키로 하는 엔티티 벡터 검색.
- **BFS** — 해석된 끝점 사이의 모든 단순 경로를 최대 `max_depth=3` 홉까지
  열거하고 `(hops, -average edge weight)`로 정렬해 `top_k_paths=10`으로
  자릅니다. 경로 안에서 노드는 반복하지 않습니다. 전역 `max_visit_nodes`
  예산과 계층별 `max_frontier` 상한이 밀집 그래프의 연산량을 제한합니다.
- **Beam 폴백** — BFS가 아무것도 찾지 못할 때만 실행: 양방향 embedding 유도
  beam(`beam_width=5`, `beam_max_hops=5`)이 각 이웃을 "반대쪽 끝점과의 코사인
  유사도 + `0.2 × edge weight`"로 점수화하고 첫 합류 노드에서 멈춥니다. 벡터는
  `SNKVVectorStorage.get_vectors_by_ids`에서 가져옵니다.
- **포매팅** — LLM 프롬프트는 명시적 체인과 각 노드·엣지 설명, 통과한 엣지의
  원본 chunk를 받습니다. 구조화 페이로드(`entities` / `relationships` /
  `chunks` / `references`)는 `path_info` 메타데이터와 함께 일반 검색 데이터에
  병합됩니다.

사용 지점은 두 곳입니다:

- `mode="path"`는 경로 검색만 수행하고 경로가 없으면 `hybrid` 검색으로
  폴백하므로 빈 컨텍스트로 답하지 않습니다.
- 관계형 질문으로 읽히는 `mix` / `hybrid` 쿼리(영·중·일·한 패턴;
  `is_relationship_query`)는 경로 절을 컨텍스트에 자동 병합합니다.

모든 수치는
`config/features/agent_side/graph_rag_path.py::GRAPH_RAG_PATH`에 있습니다.

## 검색 임계값

LightRAG 기본값은 거의 모든 후보를 통과시켰습니다: `TOP_K=40`,
`CHUNK_TOP_K=20`, `COSINE_THRESHOLD=0.2`, 그리고 `MIN_RERANK_SCORE`는 꺼짐.
조정된 기본값은 `TOP_K=20`, `CHUNK_TOP_K=12`, `COSINE_THRESHOLD=0.4`,
`MIN_RERANK_SCORE=0.5`이며, 그래프 규모 상한
`MAX_SOURCE_IDS_PER_ENTITY=50`, `MAX_SOURCE_IDS_PER_RELATION=50`,
`SOURCE_IDS_LIMIT_METHOD=FIFO`, `RELATED_CHUNK_NUMBER=5`도 함께 설정합니다.

이 값들은 vendored 패키지를 임포트하기 **전에** `graph_rag/__init__.py`에서
설정합니다: `QueryParam`과 LightRAG dataclass는 임포트 시 환경 변수를 클래스
기본값으로 읽으므로, 예전 위치(`base.py` 자체의 LightRAG 임포트 이후)에서는
실제로 한 번도 적용되지 않았습니다.

## 스파스 검색: 구현하지 않은 이유

계획의 스파스 경로는 embedding 계층에서 막힙니다.
`models/embed_model/text/core.py::CustomEmbedding`은 밀집 벡터 하나만
반환합니다 — 로컬은 llama.cpp `create_embedding`, 원격은 OpenAI 호환
`/embeddings` 엔드포인트입니다. `SNKVVectorStorage`는 단일 HNSW 코사인
인덱스만 두고, `EmbeddingFunc`도 `ndarray` 하나를 반환합니다. BGE-M3의 네이티브
스파스 출력에는 `FlagEmbedding`(PyTorch와 수백 MiB) 또는 스파스 지원
엔드포인트가 필요하고 둘 다 데스크톱 배포에 부적합합니다. 따라서 희귀 ID/버전
문자열 검색은 미해결로 남고, SQLite FTS5/BM25가 이중 스파스 인덱스가 아닌
향후 선택지로 기록되어 있습니다.

## 실측 효과

`evals/graph_rag/multihop_bench.py`는 비슷한 이름의 방해 엔티티가 섞인 20개
문서 코퍼스(102개 노드 / 106개 엣지)를 색인하고 6개 질문을 평가합니다: 최단
체인이 2-5홉인 관계 질문 4개와 단일 홉 대조 질문 2개입니다. 두 실행은 **같은**
인덱스를 쿼리합니다.

| 지표(멀티홉 질문)   | 변경 전 `mix` | 변경 후 `mix` | 변경 후 `mode="path"` |
| :------------------ | :------------ | :------------ | :-------------------- |
| 컨텍스트 연결 검색  | 1.000         | 1.000         | 1.000                 |
| 답변 연결 검색      | 0.875         | 1.000         | 1.000                 |
| 모든 연결 포함 답변 | 0.750         | 1.000         | 1.000                 |
| 컨텍스트 총 문자 수 | 155,720       | 87,351        | 23,834                |
| 단일 홉 대조 정확도 | 1.000         | 1.000         | 1.000                 |

컨텍스트 크기는 `mix`에서 `44%`, `mode="path"`에서 `85%` 줄었고 답변 연결
검색은 `0.875`에서 `1.000`으로 올랐습니다. 같은 그래프에서 경로 지연은 쿼리당
`2-13 ms`(BFS `2-5 ms`, beam `11-13 ms`)로 BFS `500 ms`, beam `2 s` 목표를
크게 밑돕니다. beam은 3홉 BFS가 찾지 못한 4·5홉 체인을 찾았기에 폴백으로
기본 활성화되어 있습니다.

단일 홉 RAGAS 스위트(`evals/results/graph_rag/20261019_233415`)는
faithfulness, context recall, context precision을 `1.000`으로 유지했습니다.
answer relevancy는 `0.916`였고 변경 전 두 번의 실행은 `0.963`과 `0.934`——
실행 간 변동 범위 안이며 정확성 지표는 그대로입니다.

결론: 변경 전 파이프라인보다 우수하므로 GraphRAG 계획 문서 두 개를 삭제했습니다.

## 테스트

```bash
uv run --no-sync pytest tests/skills/builtin/core/multimodal_rag/test_path_search.py \
  tests/skills/builtin/core/multimodal_rag/test_path_query_integration.py -q
uv run --no-sync python evals/graph_rag/multihop_bench.py --modes mix,path
uv run --group evals python evals/evals.py graph_rag
```

단위 테스트는 홉 보장, 사이클 방지, 경로 상한, 레이블·벡터 끝점 해석, beam
폴백을 고정합니다. 통합 테스트는 실제 SNKV 저장소에서 LLM 호출 없이
`mode="path"`와 `mix` 자동 병합을 실행합니다. RAGAS 스위트는 단일 홉 답변
품질의 회귀 게이트입니다.

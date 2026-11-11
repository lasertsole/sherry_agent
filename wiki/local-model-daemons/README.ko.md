# 🧠 로컬 모델 daemon: Embed, Reranker, 로컬 Chat

[**English**](README.md) · [中文](README.zh.md) · **한국어** · [日本語](README.ja.md)

> 로컬 GGUF 모델(bge-m3 벡터, bge-reranker, 보조 채팅 모델)은 더 이상 오케스트레이터 안에서 로드되지 않습니다. 모델 패밀리마다 독립된 자식 프로세스가 가중치를 한 번만 로드하고 loopback HTTP JSON으로 응답하며, 유휴 시간이 지나면 스스로 종료합니다. 메인 프로세스는 요청/응답 데이터만 보관합니다.

**사실 출처:** `models/daemon/base.py`, `models/embed_model/text/daemon.py`, `models/embed_model/text/core.py`, `models/reranker_model/daemon.py`, `models/reranker_model/__init__.py`, `models/LLMs/local_daemon/server.py`, `models/LLMs/local_daemon/client.py`, `models/LLMs/auxiliary_llm/core.py`, `config/features/infra_side/local_daemons.py`, `packaging/backend_entry.py`, `skills/builtin/core/speech_to_text/scripts/core.py`. 아래의 모든 주장은 해당 코드와 "실측 베이스라인"의 수치로 확인했습니다.

## 🎯 왜 daemon인가

백엔드는 **하나의** Python 프로세스입니다(`server/__main__.py`가 `app.config.processes = 1`을 고정). 모든 세션, 미들웨어 체인, WebSocket 전송, 도구 샌드박스가 여기에 있습니다. 600 MB 벡터 모델이나 5.5 GB 채팅 모델을 이 프로세스에 두는 것은, 가장 응답성이 필요한 프로세스에 가장 OOM되기 쉬운 비용을 지우는 일입니다.

기존 인프로세스 embed 경로의 실측 문제:

| 지표 (bge-m3 q8_0, 이 호스트) | 인프로세스 (이전) | daemon (이후) |
| :--- | :--- | :--- |
| embed 호출마다 핫 로드 `Llama(...)` | 약 1.27–1.30초 | 0 (이미 상주) |
| 배치당 추론 | 약 0.32초 | 약 0.49초 (loopback HTTP 포함) |
| 호출 중 메인 프로세스 RSS | 피크 약 1017–1055 MB (`ru_maxrss`) | 46.5 → 47.3 MB (`VmRSS`) |
| 모델 RSS | 메인 프로세스 내부 | 약 1045 MB (daemon 내부) |
| 인프로세스 벡터와 cosine | — | 1.0 (완전 동일) |

**이것은 메모리 격리이지 총량 감소가 아닙니다.** 모든 패밀리가 동시에 활성화되면 총 메모리(메인 + daemon)는 거의 같거나 조금 늘어납니다. 이점은 메인 프로세스에 큰 피크가 생기지 않고, 꺼진 기능은 spawn되지 않아 메모리를 쓰지 않으며, 핫 호출이 더 빠르다는 것입니다(GGUF를 매번 다시 매핑하지 않음).

## 🏗️ 아키텍처

```text
메인 프로세스 (오케스트레이터)                  daemon 자식 프로세스 (detached)
  _LocalEmbeddingBackend.embed(texts)  ─HTTP──▶  POST /embeddings
  _LocalGgufBackend.rank(...)          ─HTTP──▶  POST /rerank | /filter | /predict
  DaemonChatClient.create_chat_...     ─HTTP──▶  POST /v1/chat/completions
                                                 하나의 GGUF, 한 번만 로드
                                                 추론은 락으로 직렬화
                                                 idle watchdog이 만료 시 종료
```

클라이언트 절반은 공유됩니다. `DaemonSpec`이 엔드포인트, pid 파일, import 대상을 담고 `models/daemon/base.py`가 하나의 라이프사이클을 구현합니다:

- **준비 판정은 엄격합니다.** `/healthy`는 `{"ready": true}`를 반환해야 합니다. 프로세스가 살아 있어도 모델이 로드 중이면 준비된 것이 아닙니다.
- **spawn은 중복되지 않습니다.** 기록된 pid가 살아 있으면 다시 spawn하지 않습니다. 오래된 pid 파일은 교체합니다.
- **ReadyState는 세 값입니다.** `READY`, `WARMING`(프로세스는 있으나 대기), `UNAVAILABLE`(프로세스가 없고 시작도 불가). 이 구분 덕분에 클라이언트는 중복 로더 없이 폴백할 수 있습니다.
- **kill**은 SIGTERM, 최대 5초 대기, SIGKILL, 그리고 자신의 자식 프로세스를 회수합니다. atexit kill은 실제로 spawn한 프로세스만 등록합니다.
- **유휴 종료**는 `IdleWatchdog`: `*_idle_timeout_s` 동안 요청이 없으면 `os._exit(0)`. 다음 호출에서 다시 콜드 스타트합니다.

### 엔드포인트, 포트, pid 파일

| 패밀리 | 포트 | pid 파일 | 엔드포인트 |
| :--- | :--- | :--- | :--- |
| Embed | 9012 | `.embed_daemon.pid` | `GET /healthy`, `POST /embeddings` |
| Reranker | 9013 | `.reranker_daemon.pid` | `GET /healthy`, `POST /rerank`, `/filter`, `/predict` |
| 로컬 Chat | 9014 | `.llm_daemon.pid` | `GET /healthy`, `POST /v1/chat/completions` |
| 비전 Chat | 9015 | `.vision_daemon.pid` | `GET /healthy`, `POST /v1/chat/completions` (mmproj) |
| STT (리팩터 완료) | 9011 | `.stt_daemon.pid` | `GET /healthy`, `POST /transcribe` |

pid 파일과 daemon 로그는 `APP_HOME`(패키지 빌드에서는 쓰기 가능한 앱 데이터 루트, 소스 체크아웃에서는 저장소 루트)에 있으며, 읽기 전용 코드 트리에는 쓰지 않습니다. 모든 daemon은 `127.0.0.1`에만 바인딩하고 JSON, 인증 없이 응답합니다. 기존 STT daemon과 같은 로컬 캐시이며 네트워크 서비스가 아닙니다.

### 패키징

소스 체크아웃은 `python -c "from <module> import main; main()"`으로 시작합니다. PyInstaller 동결 빌드는 `-c`를 실행할 수 없으므로 `--sherry-daemon <module>`로 패키징된 백엔드를 다시 실행합니다. `packaging/backend_entry.py`는 `server.__main__`을 임포트하기 **전에** 해당 모듈의 `main()`으로 분기하므로, daemon 자식 프로세스는 로깅, `nest_asyncio`, LangSmith를 초기화하지 않습니다. `--sherry-home`과 `--sherry-desktop`도 전달되어 자식이 같은 쓰기 가능한 홈을 해석합니다.

## 🔌 세 클라이언트

**Embedding** (`models/embed_model/text/core.py`). daemon은 원시 벡터를 반환하고 클라이언트는 L2 정규화와 순서 검사를 유지합니다. 호출부가 이미 만드는 배치(`memory_embeddings`는 배치당 32개, 메시지 인덱서는 64개)를 그대로 보내므로 인덱스 경로에 두 번째 배치 계층이 필요 없습니다. daemon 오류 후 한 번 재시도(유휴 종료나 크래시 흡수)하고, 두 번째 실패는 호출자에게 전달됩니다(의미 검색은 fail-open).

**Reranker** (`models/reranker_model/__init__.py`). `rank`, `filter`, `predict`가 각각 하나의 엔드포인트에 대응합니다. `predict_scores`는 쌍마다 왕복하지 않고 쌍 전체를 한 요청으로 보냅니다. `RERANKER_MODEL_LOCAL=false`이면 클라우드 백엔드는 전혀 바뀌지 않습니다.

**로컬 Chat과 비전** (`models/LLMs/auxiliary_llm/core.py` / `models/LLMs/base_local_llama.py`). 로컬 GGUF 경로는 네이티브 tool call을 **사용하지 않습니다**: `LocalToolBinder`가 JSON 지시를 주입하고 `LocalStructuredOutput`이 instructor로 `create_chat_completion_openai_v1`을 감쌉니다. 따라서 daemon은 llama-cpp 자체의 `create_chat_completion` 형태를 노출하고, `models/LLMs/local_daemon/client.py`는 그 두 메서드만 구현하는 작은 shim입니다. `create_chat_completion_openai_v1`은 응답을 OpenAI `ChatCompletion`으로 검증하고(instructor가 요구하는 id/object/created/model 보완) instructor의 제어 인자는 네트워크로 보내지 않습니다. daemon을 시작할 수 없거나 예산을 넘겨도 warm이 아니면 `_LocalAuxiliaryChatModel`은 인프로세스 `Llama`로 폴백합니다 — 기능 우선, 격리는 그다음. 멀티모달 베이스는 같은 클라이언트를 포트 9015 daemon에 사용하며, `image_url` 블록은 llama-cpp의 Qwen2.5-VL handler로 그대로 전달됩니다.

## 🛟 실패 동작과 스위치

| 상황 | 동작 |
| :--- | :--- |
| 호출 사이에 daemon이 유휴 종료 | 클라이언트가 한 번 재시도, `ensure_ready`가 다시 spawn |
| daemon이 warm 중(모델 로드 중) | `WARMING`; embed/reranker는 재시도 가능한 오류, 로컬 chat은 인프로세스 폴백 |
| daemon을 전혀 시작할 수 없음 | `UNAVAILABLE`; embed/reranker는 해당 프로세스의 남은 기간 동안 기존 인프로세스 로더 사용, 로컬/비전 chat은 인프로세스 `Llama` |
| 요청 중 daemon 크래시 | 해당 요청은 실패; 기억 검색은 cosine 순서를 유지하고 인덱스 구축은 오류를 기록 |
| 기능 비활성화 | `EMBEDDING_DAEMON_ENABLED=false`, `RERANKER_DAEMON_ENABLED=false`, `AUXILIARY_LLM_DAEMON_ENABLED=false`로 daemon 없는 기존 경로 복귀 |

모든 설정은 `LOCAL_DAEMONS`(`config/features/infra_side/local_daemons.py`)에 있고 환경 변수로 덮어쓸 수 있습니다: 포트(`*_DAEMON_PORT`), 준비 예산(`*_READY_WAIT_S`), 유휴 창(`*_DAEMON_IDLE_TIMEOUT_S`), 요청 타임아웃(`*_HTTP_TIMEOUT_S`), `AUXILIARY_LLM_N_CTX`, `VISION_LLM_DAEMON_*`. `auxiliary_n_ctx`의 기본값은 기존 40 960입니다. 낮추면 로컬 `max_tokens`도 창의 절반으로 제한되어 작은 KV cache와 과대한 완성 요청이 결합되지 않습니다.

## 📏 실측 베이스라인 (2026-11-11, 이 호스트)

| 측정 항목 | 결과 |
| :--- | :--- |
| `import server.__main__` | 피크 RSS 약 26.7 MB, 약 0.86초 |
| `agent.core` + `server.trigger.http` | 피크 RSS 약 153.9 MB, 약 5.2초 |
| 부팅 import 집합 | `torch`, `transformers`, `cv2`, `mineru_vl_utils`, `leidenalg`, `snkv`, `tree_sitter`, `gguf`, `llama_cpp` 모두 없음 |
| Embed daemon 첫 호출 (spawn + 로드) | 3.8초 |
| Embed 핫 호출 | 0.49초 (인프로세스 약 2.88초) |
| embed 중 메인 프로세스 RSS | 46.5 → 47.3 MB |
| daemon RSS | 1045.6 MB |
| 벡터 cosine (daemon vs 인프로세스) | 1.0 |
| Reranker daemon 첫 호출 (spawn + 606 MB GGUF 로드) | 32.8초 |
| Reranker 핫 랭크 (문서 4개) | 1.33초 (인프로세스는 호출마다 약 31.5초, 재로드 포함) |
| rerank 중 메인 프로세스 RSS | 46.6 → 46.9 MB; daemon 898.4 MB |
| Reranker 점수/순서 (daemon vs 인프로세스) | 완전 동일 |
| 로컬 Chat 첫 호출 (Qwen2.5-0.5B-Instruct Q4, spawn + 로드) | 3.33초 |
| 로컬 Chat 핫 호출 | 0.77초 |
| 로컬 Chat 중 메인 프로세스 RSS | 65.3 → 65.9 MB; daemon 600.4 MB |
| 로컬 Chat tool call + 구조화 출력 | `add(a=2,b=2)` 파싱; `Answer(answer='Paris')` |
| 같은 모델을 인프로세스로 로드 | 메인 프로세스 피크 +537.9 MB |
| 비전 첫 호출 (Qwen2.5-VL-3B Q4 + mmproj, 96 px 이미지) | 13.61초 (CPU: 이미지 인코딩 + prompt) |
| 비전 핫 호출 | 7.44초 |
| 비전 중 메인 프로세스 RSS | 67.3 → 20.2 MB (피크 68.2); daemon 3 571 MB |
| 비전 응답 | `Red` (정답), 포트 9015 |

**"무거운 의존성 지연 로드" 방향은 실측된 Kill criteria로 건너뛰었습니다:** 전체 부팅 import 체인(모듈 부트 + agent core + curator/cron + trigger + services)은 `torch` / `transformers` / `cv2` / `mineru_vl_utils` / `leidenalg` / `snkv` / `tree_sitter` / `gguf` / `llama_cpp` 중 어느 것도 로드하지 않습니다. 의존성 자체는 작지 않아 단독 import에 `torch` 약 490 MB / 5.9초, `transformers` 약 41 MB / 9.9초가 들고 나머지는 기본 환경에 없습니다. 즉 이미 최적화가 원하는 위치(각 기능 함수 내부)에 있습니다. `tests/perf/test_boot_import_set.py`가 하나라도 부팅 집합에 다시 들어오면 실패합니다. 남은 부팅 비용은 `openai` / `langchain` / `langgraph`와 로깅·설정 부트에서 오며, 모든 라우트가 필요로 합니다. (`PIL`은 미디어 압축 모듈을 통해 부팅 시 import되지만 실측 약 49 ms이고 RSS 피크 증가가 측정되지 않아 변경할 가치가 없습니다.)

**비전 동형.** ITTT/VTTT는 **같은** 로컬 chat daemon(포트 9015)을 지납니다: daemon은 `SHERRY_LOCAL_LLM_MMPROJ_PATH`를 읽어 `Qwen25VLChatHandler`를 추가하고 `n_batch = n_ubatch = n_ctx`로 인프로세스 `LocalMultimodalLlamaChatBase`를 정확히 모방합니다. 두 패밀리는 같은 Qwen2.5-VL 가중치 + mmproj를 해석하므로 하나의 비전 daemon을 공유합니다. 다른 비전 모델은 별도 포트가 필요합니다. `LocalMultimodalLlamaChatBase`는 daemon을 먼저 시도하고 시작할 수 없으면 인프로세스 클라이언트로 폴백합니다. 안전 검토는 여전히 보조 클라이언트를 공유합니다. 긴 생성 뒤에 대기한 검토 호출은 20초 fail-open 예산에 도달할 수 있으며, 이는 현재와 같은 결과입니다.

## 🧪 테스트

- `tests/models/daemon/test_base.py` — pid 파일, 준비 3상태, spawn 명령(소스 vs 동결), atexit, kill/회수, 요청 디코딩, 유휴 watchdog.
- `tests/models/embed_model/text/test_embed_daemon.py` — 가짜 모델 위의 실제 loopback server: 순서, 배치, 검증, health; 클라이언트: 페이로드, 재시도, warming, 폴백, 정규화.
- `tests/models/reranker_model/test_daemon.py` — rank/filter/predict 와이어 형태, 검증, 클라이언트 재시도와 폴백.
- `tests/models/LLMs/local_daemon/test_local_daemon.py` — llama-cpp 인자 전달, instructor 호환 `ChatCompletion`, 보조 모델 daemon/폴백 배선, `n_ctx`/`max_tokens` 클램프, 비전 daemon(mmproj handler, 포트 9015)과 그 폴백.
- `tests/packaging/test_backend_entry.py` — `--sherry-daemon` 파싱과 분기.
- `tests/skills/builtin/core/speech_to_text/test_stt_daemon_pid.py` — STT 리팩터 후에도 공유 라이프사이클 위에서 같은 pid 계약.
- `tests/config/test_features_infra_side.py` — `LOCAL_DAEMONS` 기본값, 환경 덮어쓰기, 잘못된 값의 폴백.

## ⚠️ 제한

- 포트당 하나의 모델 인스턴스. 두 개의 서로 다른 보조 GGUF(두 번째 로컬 프로필)는 daemon을 공유할 수 없습니다. 경로는 spawn 시 해석되고 daemon은 처음 로드한 모델만 제공합니다.
- 요청 큐 공정성은 없습니다: `ThreadingHTTPServer`와 추론 락 하나로, 대기 스레드 순서는 OS 스케줄러에 따릅니다. 긴 배치는 한 번의 검색을 지연시키지만 로컬 캐시로서는 허용됩니다.
- 인증, TLS, 크로스 호스트 접근은 없습니다. daemon은 STT daemon과 마찬가지로 구조상 `127.0.0.1`에만 바인딩합니다.
- 로컬 chat daemon은 스트리밍을 지원하지 않습니다. 로컬 보조 경로는 절대 스트리밍하지 않으므로(호출은 모두 `invoke`/`ainvoke`) `stream: true`는 조용히 강등되지 않고 명시적으로 거부됩니다.

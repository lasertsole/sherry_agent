# NanoJev 本地判别器接入计划 — TODO 意图识别 + STEP_JUDGE

> 目标：用本地 NanoJev（Qwen3-0.6B backbone + 结构化决策头，Hugging Face `C-Tianyu/NanoJev`，596M 参数，MIT 参考实现）替换两个判别器的判定路径：
> ① **TODO 意图识别**：`agent/middlewares/task_intent/core.py:110` 的 `_detect_task_intent`（现为纯正则启发式，boolean 命题判定）。
> ② **STEP_JUDGE 步骤判官**：`agent/tools/taskflow/step_judge.py:142` 的 `judge_step_result`（现为 auxiliary LLM，pass/retry/block 三选一裁决）。
> 项目启动时后台线程从 Hugging Face 自动下载；**下载失败不阻塞、原判别器立即生效**；下载 + 加载 + 冒烟完成后**原子热替换**原判别器。模型**同时支持 GPU 加速与纯 CPU 运行**（device/dtype 自动解析）。

## TL;DR (For humans)

**What you'll get**：服务器启动后 NanoJev 权重在后台自动下载并加载，就绪后两个判别器零文本解码地切换到本地模型（意图识别用 boolean 头、步骤裁决用 choice 头），网络差/无 GPU/下载失败时自动保持现有 auxiliary-LLM 与正则路径，启动速度与稳定性不受任何影响。

**Why this approach**：两处判别器都是单函数缝，NanoJev 作为 fast path 插在函数内部、原判别器降级为 fallback，是改动面最小、fail-open 语义不变的做法；bootstrap 走纯守护线程（`agent/core.py::init()` 是同步函数），规避 asyncio.Semaphore 事件循环绑定坑（AGENTS.md Known Pitfalls）。

**What it will NOT do**：不改四语言 README（parity gate）；不把上游训练管线加为 pip 依赖（只 vendor 最小推理子集）；不改 STEP_JUDGE 的 fail-open 语义与 max_retries 预算；不做 GGUF/llama.cpp 转换；不做训练/微调；不新增 .env 变量（配置走 TypedDict）。

**Effort**：8 个实现 todo + 4 个终验，约 2-3 个工作日（vendor 适配为最大不确定项）。

**Risk**：上游仓库 `TianyuCodings/NanoJev` 的文件布局未逐文件核实（HF 卡片已确认仓库存在、MIT、含参考实现）；vendor todo 的验收以真实权重冒烟为准。CPU 单次前向约 0.1-1s，意图路径每回合一次，`inference_timeout_s=10` 兜底且可配置关闭。根检查点 fp32 约 2.4GB 下载体积。

**Decisions**：D1 vendor 而非 pip 依赖上游（MIT + provenance 头）；D2 纯线程 bootstrap 而非 asyncio task；D3 显式 `device="cuda"` 但不可用 → FAILED 不静默降级，默认 `auto` 保证 CPU 兜底；D4 judge 低置信度回退 auxiliary 判官而非直接 PASS；D5 意图阈值 0.6 起步可配置，假阴性只损失 steering（与现状等价）；D6 权重缓存 `.cache/nanojev/`（gitignore），不复用 GGUF 语义的 `models/*/model_weight/`。

## Scope

**IN**：
- 新配置 `NANOJEV`（TypedDict，`config/features/infra_side/`）与缓存路径（`config/path.py`）。
- 新包 `models/nanojev/`：registry（状态机/热替换）、downloader（HF 下载）、runtime（GPU/CPU 推理）、vendor（上游 MIT 最小推理子集）、bootstrap（后台线程）。
- 两个判别器缝接入：`step_judge.py` choice fast path、`task_intent/core.py` boolean fast path，均带回退链。
- `agent/core.py::init()` 启动钩子；`GET /nanojev-status` 观测端点；pyproject 显式声明 `huggingface_hub`。
- 全部新测试（torch-free 单测 + 现有套件扩展）。

**OUT / Must-NOT-Have**：
- 不改动任何 README（四语言 parity gate 红线）。
- 不引入上游 NanoJev 训练管线、不引入 llama.cpp、不做权重格式转换。
- 不改动 `judge_step_result` 的对外签名、`JudgeResult` 结构、`STEP_JUDGE` 既有字段语义、`max_retries` 消费逻辑。
- 不改动 `_detect_task_intent` 本体（正则保留为 fallback，现有测试不动）。
- 不在 `config/**` 引入任何新依赖（config 依赖禁区）；`models/**` 不 import `agent/**`/`server/**`。
- bootstrap 任何路径不得向调用方抛异常、不得阻塞 `init()` 返回。
- 不新增 .env 变量；HF 镜像沿用 huggingface_hub 原生 `HF_ENDPOINT` 约定（中国用户可设 `https://hf-mirror.com`）。

## 关键设计

### 架构

```
agent.core.init()  (server 启动, 同步)
  └─ models.nanojev.start_bootstrap()        # 幂等, 立即返回
       └─ threading.Thread(daemon)           # 不碰事件循环
            DOWNLOADING → snapshot_download(C-Tianyu/NanoJev, allow_patterns=[best.safetensors, config.json, tokenizer/*, backbone_config/*])
            LOADING     → NanoJevRuntime.load()   # lazy import torch/transformers; device/dtype 解析
            冒烟        → 1×ask_boolean + 1×ask_choice
            READY       → registry.publish(READY, runtime)   # 原子热替换点
            任一步失败   → logger.warning + publish(FAILED, error)   # 永不上抛

judge_step_result():  NanoJev READY? ──否→ auxiliary LLM (现状不变) ──失败→ PASS (fail-open, 现状不变)
        │是→ ask_choice(["pass","retry","block"]) → argmax
        │     conf ≥ judge_confidence_floor → JudgeResult(verdict, "nanojev p=…", "")
        │     conf < floor 或异常 ──────────────→ auxiliary LLM (同上)
_adetect_task_intent(): NanoJev READY? ──否→ _detect_task_intent (正则, 现状不变)
        │是→ ask_boolean(意图命题) → p ≥ intent_threshold?
        │     异常 ───────────────────────────→ _detect_task_intent (同上)
```

### NANOJEV 配置（`config/features/infra_side/nanojev.py`）

```python
class NanoJevConfig(TypedDict):
    enabled: bool                          # 总开关; False 时全程不下载不加载
    repo_id: str                           # "C-Tianyu/NanoJev"
    revision: str                          # "main"(可钉 commit SHA)
    allow_patterns: list[str]              # ["best.safetensors", "config.json", "tokenizer/*", "backbone_config/*"]
    device: str                            # "auto" | "cuda" | "cpu"   ← GPU/纯CPU 双模
    dtype: str                             # "auto" | "fp32" | "bf16"  ← auto: cuda→bf16, cpu→fp32
    inference_timeout_s: float             # 10.0 (asyncio.wait_for 兜底)
    max_concurrent: int                    # 1 (threading.BoundedSemaphore 串行化)
    download_max_attempts: int             # 3
    download_retry_base_delay_s: float     # 2.0 (指数退避 + jitter)
    smoke_test_timeout_s: float            # 60.0 (CPU 首次加载留余量)
    intent_enabled: bool                   # NanoJev 接管 TODO 意图识别
    intent_threshold: float                # 0.6
    judge_enabled: bool                    # NanoJev 接管 STEP_JUDGE
    judge_confidence_floor: float          # 0.6
    demote_after_consecutive_failures: int # 3 (运行期连续失败自动降级回 fallback)
```

### 状态机（`models/nanojev/registry.py`）

`NanoJevState` StrEnum：`DISABLED / UNAVAILABLE / DOWNLOADING / LOADING / READY / FAILED`。`threading.Lock` 保护；`publish(state, runtime=None, error=None)` 单锁内赋值（READY 即热替换点）；`provider()` 仅 READY 返回 runtime（调用方每 call 读一次 → 天然热替换）；`record_inference_result(ok)` 连续失败 ≥ `demote_after_consecutive_failures` → FAILED（回退链自动接管）；`state_snapshot()` 供观测端点。

## Verification strategy

- 每 todo 附带 hermetic 测试（torch-free：`runtime.py` 的 torch/transformers **只在函数体内**延迟导入，registry/downloader/bootstrap 测试全部注入 fake），证据为 pytest 退出码。
- 现有测试零改动全绿是硬门槛：`tests/agent/tools/taskflow/test_step_judge.py`、`tests/agent/middlewares/test_task_intent.py`、`tests/config/test_features_infra_side.py`。
- 终验含真实启动 QA：本机为 CPU-only（已验证 `torch 2.12.0+cu130`、`cuda.is_available()=False`）→ CPU 路径是已验证基线；GPU 路径在有卡机器上以 `device=cuda` + bf16 冒烟验收。

## Execution strategy

按 Todos 顺序串行执行（1→2→3 可并行度低，4 依赖 1，5 依赖 2/3/4，6/7 依赖 5，8 独立可提前）。每个 todo 一次原子 commit（angular conventional，pathspec-only）。实现者无面试上下文：所有路径、字段、默认值、验收命令均已写死在下方。

## Todos

- [ ] 1. NANOJEV 配置 + 缓存路径 + gitignore
  - **References**: `config/features/agent_side/step_judge.py`（TypedDict+实例+docstring 的镜像模板）；`config/features/__init__.py` 与 `config/features/infra_side/__init__.py`（re-export 方式，对照 `STEP_JUDGE` 的导出行）；`config/path.py`（ROOT_DIR 与路径常量命名风格）；`.gitignore:55-61`（权重扩展名已忽略，缺目录级忽略）；`tests/config/test_features_infra_side.py`（现有断言风格）。
  - **Do**：新建 `config/features/infra_side/nanojev.py`，`NanoJevConfig` TypedDict + `NANOJEV` 实例，字段与默认值**逐字采用上文「关键设计」中的表**；在两个 `__init__.py` re-export；`config/path.py` 增加 `NANOJEV_CACHE_DIR = ROOT_DIR / ".cache" / "nanojev"`（跟随该文件现有命名/注释风格）；`.gitignore` 增加 `/.cache/` 段（注明 NanoJev 权重缓存）。
  - **Acceptance**：`uv run --no-sync python -c "from config.features import NANOJEV, NANOJEV_CACHE_DIR; assert NANOJEV['device'] in ('auto','cuda','cpu') and NANOJEV['dtype'] in ('auto','fp32','bf16')"` 通过；`tests/config/test_features_infra_side.py` 追加断言：键集合恰为上文 15 个、`enabled=True`、`device="auto"`、阈值 0.6。
  - **QA**：happy — `uv run --no-sync pytest tests/config/ -q` 全绿；failure — 临时删掉 `NANOJEV` 的 re-export 行，`import config.features` 报 AttributeError（回归后删除该临改）。
  - **Evidence path**: pytest 输出（粘贴至 commit message body 或 PR 描述）。
  - **Commit**: `feat(nanojev): add NANOJEV feature config and cache path`

- [ ] 2. registry 状态机（热替换 + 降级）
  - **References**: `runtime/lane/core.py`（模块级单例 + 锁的既有风格）；`config/features/infra_side/nanojev.py`（Todo 1 产物）；`models/utils.py:13-38`（LazyInstance 的线程安全纪律参考）。
  - **Do**：新建 `models/nanojev/registry.py` 与 `models/nanojev/__init__.py`。registry 实现「关键设计·状态机」一节的全部行为；`__init__.py` 公共 API：`provider() -> Any | None`、`state_snapshot() -> dict`（键：state/since_ts/error/device/dtype/detail）、`start_bootstrap()`（Todo 5 前先抛 NotImplementedError 的占位不行——直接 re-export bootstrap 的惰性入口：`def start_bootstrap(): from models.nanojev.bootstrap import start; start()`）、`record_inference_result(ok: bool)`。
  - **Acceptance**：未 bootstrap 时 `provider() is None` 且 `state_snapshot()["state"] == "unavailable"`；publish(READY, fake) 后 `provider() is fake`；连续 3 次失败后 state 回 FAILED 且 provider() 为 None。
  - **QA**：happy — `tests/models/nanojev/test_registry.py`：全状态迁移表、provider 门、demote 计数边界（2 次不降级、3 次降级）、snapshot 字段完整性，`uv run --no-sync pytest tests/models/nanojev/test_registry.py -q` 绿；failure — 多线程并发 publish 50 轮无死锁（`threading.Barrier` + join timeout）。
  - **Evidence path**: pytest 输出。
  - **Commit**: `feat(nanojev): atomic runtime registry with hot-swap and demotion`

- [ ] 3. HF 下载器（重试 + 离线短路）
  - **References**: `models/utils.py:72-116`（`resolve_gguf_path` 的 lazy import + ImportError 处理模式）；`config/features/infra_side/nanojev.py`；`tests/models/test_base_local_llama.py:196-242`（mock 下载的测试写法）。
  - **Do**：新建 `models/nanojev/downloader.py`：`class NanoJevDownloadError(RuntimeError)`；`def download_checkpoint(cfg: NanoJevConfig, local_dir: Path) -> Path`。短路：`local_dir/"best.safetensors"` 与 `local_dir/"config.json"` 同时存在 → 直接返回（二次启动零网络）。否则函数体内 `from huggingface_hub import snapshot_download`，参数 `repo_id=cfg["repo_id"], revision=cfg["revision"], allow_patterns=cfg["allow_patterns"], local_dir=str(local_dir)`；最多 `download_max_attempts` 次，间隔 `base_delay * 2**(n-1)` + uniform(0, 0.5) jitter（本地实现，≤15 行，不引新依赖）；最终失败抛 `NanoJevDownloadError`。HF_ENDPOINT 由 huggingface_hub 原生读取，代码不得覆盖该环境变量。
  - **Acceptance**：mock `snapshot_download` 下三类行为可断言：命中短路不调用网络函数；前 2 次抛错第 3 次成功 → 返回路径且重试 3 次；3 次全抛 → `NanoJevDownloadError`。
  - **QA**：happy/failure 场景均入 `tests/models/nanojev/test_downloader.py`（unit，monkeypatch）；另断言重试间隔单调不减。
  - **Evidence path**: pytest 输出。
  - **Commit**: `feat(nanojev): HF checkpoint downloader with retry and offline short-circuit`

- [ ] 4. 推理运行时 + vendor（GPU/纯 CPU 双模）
  - **References**: HF 卡片 https://huggingface.co/C-Tianyu/NanoJev （checkpoint 详情：backbone `Qwen/Qwen3-0.6B` rev `c1899de289a04d12100db370d81485cdf75e47ca`，权重 `best.safetensors`，SHA256 `fff62d…597c28`，键前缀 `backbone.*` + 决策头；目录 `tokenizer/`、`backbone_config/`；语言 en+zh）；上游参考实现 github.com/TianyuCodings/NanoJev（MIT）；`models/extract_model/core.py:96-125`（transformers 延迟加载先例）。
  - **Do**：
    (a) 获取上游仓库 pinned commit（记录 SHA），把**最小推理子集**复制到 `models/nanojev/vendor/`：模型定义 + 决策头 forward + checkpoint/tokenizer 加载；每个文件头加 provenance 注释（上游 commit SHA、仓库 URL、MIT 许可、本仓库适配说明）。禁止复制训练脚本/数据管线。
    (b) 新建 `models/nanojev/runtime.py`：`class NanoJevRuntime`，同步 API：
      - `load(checkpoint_dir: Path) -> None`：**函数体内**延迟 `import torch`、transformers 导入（无 torch 的进程可安全 import 本模块——registry/bootstrap 测试依赖此性质）；按 `NANOJEV["device"]` 解析设备：`auto` → `cuda if torch.cuda.is_available() else cpu`；显式 `cuda` 但不可用 → 抛错（bootstrap 会置 FAILED，不静默降级）；显式 `cpu` 直接用。按 `NANOJEV["dtype"]` 解析：`auto` → cuda 用 `torch.bfloat16`、cpu 用 `torch.float32`。加载 `best.safetensors`（键 `backbone.*` + heads）+ tokenizer + backbone_config，`model.eval()`。
      - `ask_boolean(text: str, proposition: str) -> float`：返回 P(true)，float ∈ [0,1]。
      - `ask_choice(text: str, candidates: list[str]) -> dict[str, float]`：返回 {candidate: prob} 完整分布（softmax 归一，sum≈1）。
      - 内部 `threading.BoundedSemaphore(NANOJEV["max_concurrent"])` 包裹两次 forward。
  - **Acceptance**：`uv run --no-sync python -c "import models.nanojev.runtime"` 在**不装 torch 的假设下可导入**（用 `sys.modules` 桩验证，见 QA）；设备/dtype 解析为纯函数 `_resolve_device(cfg) -> str`、`_resolve_dtype(cfg, device) -> torch.dtype` 可单测（auto+无cuda→cpu+fp32；auto+cuda→cuda+bf16；显式 cuda 无卡→RuntimeError）。
  - **QA**：`tests/models/nanojev/test_runtime.py`（torch-free，torch/transformers 以 `sys.modules` 桩注入）：解析表驱动、semaphore 串行、vendor forward 用桩模型断言分布归一；真实权重加载冒烟不进 hermetic 套件（归 F3）。failure 场景：device 显式 cuda 无卡 → 抛错路径。
  - **Evidence path**: pytest 输出 + vendor 文件头 provenance 摘录。
  - **Commit**: `feat(nanojev): vendored inference runtime with cuda/cpu auto device`

- [ ] 5. bootstrap 后台线程 + `init()` 钩子
  - **References**: `agent/core.py:68-95`（`init()` 全文，钩子插在 `_initialized = True` 之后）；`server/service/lane_lifecycle.py:33-48`（启动装配风格参考）；AGENTS.md Known Pitfalls（asyncio.Semaphore loop 绑定——本 todo 用纯线程规避）；Todo 2/3/4 产物。
  - **Do**：新建 `models/nanojev/bootstrap.py`：`def start()` 幂等（模块级 flag + threading.Lock）；`not NANOJEV["enabled"]` → `publish(DISABLED)` 后返回；否则 `threading.Thread(target=_run, name="nanojev-bootstrap", daemon=True, timeout 无).start()` 并**立即返回**。`_run()` 顺序：publish(DOWNLOADING) → `download_checkpoint(...)` → publish(LOADING) → `NanoJevRuntime().load(local_dir)` → 冒烟（`ask_boolean("Help me implement feature X.", "This is a work request.")` 结果 ∈ [0,1]；`ask_choice("The step failed with a missing dependency.", ["pass","retry","block"])` 概率和 ∈ [0.98, 1.02]）→ publish(READY, runtime) + `logger.info("NanoJev ready (device={}, dtype={}); judge/intent swapped", ...)`。全程 `try/except Exception` → `logger.warning("NanoJev bootstrap failed (fallback discriminators active): {}", exc)` + `publish(FAILED, error=repr(exc))`——**任何分支不得向 `init()` 传播异常**。`agent/core.py::init()` 末尾追加：
    ```python
    if NANOJEV["enabled"]:
        try:
            from models.nanojev import start_bootstrap
            start_bootstrap()
        except Exception:  # noqa: BLE001 - bootstrap 是启动旁路, 失败只告警
            logger.warning("NanoJev bootstrap failed to start (ignored)")
    ```
    （`from config.features import NANOJEV` 加到 core.py 顶部 import 区。）
  - **Acceptance**：`start()` 调用耗时 < 50ms（下载在后台）；disabled 路径零线程创建；download 抛错 → state=FAILED 且进程存活；成功路径（fake downloader/runtime monkeypatch）→ READY。
  - **QA**：`tests/models/nanojev/test_bootstrap.py`（unit）：四场景 + 重复调用幂等；计时断言用 `time.monotonic`。failure 场景：monkeypatch download 抛 `NanoJevDownloadError`，断言无异常外溢。
  - **Evidence path**: pytest 输出。
  - **Commit**: `feat(nanojev): background bootstrap thread wired into agent init`

- [ ] 6. STEP_JUDGE 接入（choice 头 fast path）
  - **References**: `agent/tools/taskflow/step_judge.py:142-175`（现函数全文；`enabled` 门、`_build_judge_prompt`、auxiliary 路径、fail-open 全保留）；`agent/tools/taskflow/tools/taskflow_resume.py`（14 个调用点，零改动）；`tests/agent/tools/taskflow/test_step_judge.py`（现有 monkeypatch 风格）；AGENTS.md（`_dispatch.dispatch_child` 可 monkeypatch seam 惯例）。
  - **Do**：`step_judge.py` 顶部 `from models.nanojev import provider as _nanojev_provider`（模块属性，测试可替换）+ `from config.features import NANOJEV`。`judge_step_result` 重构为：`enabled` 门 → 构建 prompt（原 156-161 行逻辑提前，auxiliary 分支复用同一 prompt）→ NanoJev 分支：`runtime = _nanojev_provider()`；`runtime is not None and NANOJEV["judge_enabled"]` 时 `dist = await asyncio.wait_for(asyncio.to_thread(runtime.ask_choice, prompt, ["pass", "retry", "block"]), timeout=NANOJEV["inference_timeout_s"])`；`verdict = max(dist, key=dist.get)` 映射 `StepVerdict(verdict)`；`conf = dist[verdict]`；`conf >= NANOJEV["judge_confidence_floor"]` → `return JudgeResult(verdict, f"nanojev p={conf:.2f}", "")`（RETRY 的 feedback 置空——`_parse_judge_response` 同样可能产出空 feedback，合法）；`conf < floor` → `logger.debug` 后落入 auxiliary 路径；`except Exception` → `logger.warning("NanoJev judge failed; falling back to auxiliary: {}", exc)` 落入 auxiliary 路径。auxiliary 路径与最外层 fail-open except **一字不改**。模块 docstring 补一行 fast-path 说明。
  - **Acceptance**：provider=None 时函数行为与 git HEAD 逐语义一致（现有测试全绿即证）；READY 假 runtime 返回分布 `{"pass":0.9,...}` → PASS 且 reason 带 `nanojev p=0.90`；分布 `{"retry":0.4,...}`（< floor）→ auxiliary 被调用；runtime 抛错 → auxiliary 被调用。
  - **QA**：追加到 `tests/agent/tools/taskflow/test_step_judge.py`（monkeypatch `step_judge._nanojev_provider`）：上述四场景 + timeout 场景（`asyncio.wait_for` 超时 → auxiliary 兜底）。命令：`uv run pytest tests/agent/tools/taskflow/test_step_judge.py tests/agent/tools/taskflow/test_resume_with_judge.py -q`。
  - **Evidence path**: pytest 输出。
  - **Commit**: `feat(taskflow): nanojev choice head as step-judge fast path`

- [ ] 7. TODO 意图识别接入（boolean 头 fast path）
  - **References**: `agent/middlewares/task_intent/core.py:110-134`（`_detect_task_intent` 全文，**不改动**）、`:286-343`（`abefore_model`，327 行调用点）、`:345-356`（`before_model` 同步路径，`asyncio.run` 包裹 async 主路径，零改动）；`tests/agent/middlewares/test_task_intent.py`；Todo 2/4 产物。
  - **Do**：`task_intent/core.py` 增加模块常量 `TASK_INTENT_PROPOSITION = "The user message is a work request that implies creating or updating tasks/todos (implementation, fixing, building, planning), not a casual greeting or a pure informational question."`（模型卡声明 en+zh 双语训练，中文消息同样可用此英文命题）。新增：
    ```python
    async def _adetect_task_intent(content: str) -> bool:
        runtime = _nanojev_provider()  # 同 Todo 6 的模块属性 seam
        if runtime is not None and NANOJEV["intent_enabled"]:
            try:
                p = await asyncio.wait_for(
                    asyncio.to_thread(runtime.ask_boolean, content, TASK_INTENT_PROPOSITION),
                    timeout=NANOJEV["inference_timeout_s"],
                )
                return bool(p >= NANOJEV["intent_threshold"])
            except Exception:
                logger.debug("NanoJev intent failed; falling back to heuristic")
        return _detect_task_intent(content)
    ```
    `abefore_model` 中 `if not _detect_task_intent(content):`（327 行）改为 `if not await _adetect_task_intent(content):`。其余一行不动。
  - **Acceptance**：provider=None → 与现状一致（现有测试全绿）；READY + p=0.9 → 注入 steering；READY + p=0.2 → 返回 None；runtime 抛错 → 正则兜底路径生效（用使正则命中的消息验证）。
  - **QA**：追加到 `tests/agent/middlewares/test_task_intent.py`：上述四场景 + 超时回退。命令：`uv run pytest tests/agent/middlewares/test_task_intent.py -q`。
  - **Evidence path**: pytest 输出。
  - **Commit**: `feat(middleware): nanojev boolean head for task-intent detection`

- [ ] 8. `/nanojev-status` 观测端点 + 显式依赖声明
  - **References**: `server/trigger/http/lane.py`（`@app.get("/lane-status")` 全文——注册方式、返回 dict 的 handler 写法的直接镜像）；`server/trigger/http/` 的模块注册聚合点（lane.py 被导入的位置）；`models/nanojev/__init__.py::state_snapshot`（Todo 2）；`pyproject.toml`（`[project.dependencies]` 列表）。
  - **Do**：新建 `server/trigger/http/nanojev.py`，按 lane.py 同样方式注册 `GET /nanojev-status`，handler 内延迟 `from models.nanojev import state_snapshot` 并返回其 dict（server → models 方向合法；若 lane.py 的聚合是显式 import，则在同处加一行）。`pyproject.toml` 的 `[project.dependencies]` 按字母序插入 `"huggingface_hub>=0.36"`（0.36.2 已在环境验证，仅补直接依赖声明，不改 uv.lock 手工内容——执行 `uv lock` 让 lock 自然收敛）。torch/transformers **不**新增 pin（传递依赖已验证：torch 2.12.0+cu130、transformers 4.57.6）。
  - **Acceptance**：`uv run python -m server` 启动后 `curl -s http://127.0.0.1:8080/nanojev-status` 返回含 `state` 键的 JSON（enabled=False 时为 `{"state": "disabled"}`）；`uv run --no-sync python -c "import transformers, torch, huggingface_hub"` 通过。
  - **QA**：`tests/server/` 下新建端点测试（Robyn 测试模式参照 lane-status 既有测试；若无先例则用 `uv run --no-sync pytest tests/server -q` 保证全绿 + F3 手动 curl）。
  - **Evidence path**: curl 输出 + pytest 输出。
  - **Commit**: `feat(server): nanojev-status endpoint and explicit hf_hub dependency`

## Final verification wave

- [ ] F1. 计划合规审计：对照本计划逐条核对 8 个 todo 的 Do/Acceptance 已实现；`## Todos` 行全部 column-zero `- [ ] N.` 语法；无越范围文件（`git diff --stat` 仅含 Scope 列出的路径）。
- [ ] F2. 代码质量门：`uv run --with ruff ruff check . && uv run --with ruff ruff format --check .`；`uv run --no-sync basedpyright agent/ models/nanojev/` 0 error；`uv run --no-sync lint-imports` 通过（重点：agent→models 合法、config 零新依赖、server→models 合法）。
- [ ] F3. 真实启动 QA（本机 CPU-only 基线）：`uv run python -m server` 启动，日志依次出现 nanojev DOWNLOADING→LOADING→READY（或断网时 FAILED + warning）；`curl -s http://127.0.0.1:8080/nanojev-status` 显示 ready 与 device=cpu/dtype=float32；发一条含"帮我实现…"的消息验证 steering 注入日志（task_intent）；构造含失败子结果的 TaskFlow 步骤验证 judge 日志出现 `nanojev p=`；二次启动（已缓存）验证零网络短路；如另有 GPU 机器：设 `device="cuda"` 验证 bf16 加载与冒烟通过。
- [ ] F4. 范围保真：Must-NOT-Have 逐条核对——四语言 README 零 diff；无上游 pip 依赖；`judge_step_result` 对外签名/`JudgeResult`/`STEP_JUDGE` 旧字段语义未变；`_detect_task_intent` 本体未变；bootstrap 无异常外溢路径；`.env.example` 未改动。

## Commit strategy

每 todo 一个原子 commit，angular conventional + pathspec-only（`git commit -- <files>`）：todo 标题行即 commit message（见各 todo 的 Commit 字段）。禁止把 TODO/ 计划文件与实现混入同一 commit。全部完成后不 squash。

## Success criteria

1. 启动后台自动下载，就绪后 `GET /nanojev-status` 显示 READY；两个判别器在 READY 后自动走 NanoJev（日志/返回 reason 可证）。
2. 断网/下载失败启动：state=FAILED，auxiliary judge 与正则意图识别行为与改动前完全一致（现有测试零改动全绿）。
3. 回退链有效：低置信度、推理超时、运行期连续失败三种情形均自动落回原判别器。
4. GPU/纯 CPU 双模：默认 `auto` 在 CPU-only 机器（本机已验证基线）全流程可用；CUDA 机器自动 bf16 加速；显式 cuda 无卡时明确 FAILED 不静默降级。
5. F1-F4 全部 APPROVE；`tests/run_tests_split.py` 全绿。

## Risks

- R1 上游仓库文件布局未逐文件核实（HF 卡片已确认仓库、MIT、参考实现与 checkpoint 结构）；Todo 4 的 vendor 边界以 F3 真实冒烟为最终验收。若上游不可达，退化方案为按 HF 卡片文档化架构（Qwen3-0.6B 28L/h1024 + decision heads）自实现 forward——此为风险预案而非默认路径。
- R2 CPU 前向延迟（~0.1-1s/次）加在每回合意图判定上：`inference_timeout_s=10` 兜底，`intent_enabled=false` 可单独关闭。
- R3 首次下载体积约 2.4GB（fp32 根检查点）；HF_ENDPOINT 镜像可用；下载中断由 huggingface_hub 断点续传。

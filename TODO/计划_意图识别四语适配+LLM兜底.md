# 计划：意图识别四语适配 + LLM 兜底层

- 建档日期：2026-10-09
- 状态：**计划（未开工）**
- 触发问题：`TaskIntentMiddleware`（`agent/middlewares/task_intent/core.py`）的意图检测只有纯规则层，且只覆盖英文 + 中文。日语/韩语短消息（< 100 字符、不含英文/中文关键词）漏检 → 不注入 steering → LLM 可能不自发编排。无规则不确定时的 LLM 兜底。

## 现状

```
用户消息
  ↓
_detect_task_intent()（纯规则，0ms，0 token）
  ├─ 英文关键词命中 → 是任务 ✅
  ├─ 中文关键词命中 → 是任务 ✅
  ├─ 英文/中文问题模式 → 不是任务 ✅
  ├─ 英文/中文闲聊模式 → 不是任务 ✅
  ├─ >100 字符 → 是任务（语言无关兜底）✅
  └─ 日语/韩语短消息、无关键词命中 → ❌ 漏检
```

### 四语言覆盖

| 语言 | 任务关键词 | 问题模式 | 闲聊模式 | 长消息兜底 |
|---|---|---|---|---|
| English | ✅ 20 个 | ✅ | ✅ | ✅ |
| 中文 | ✅ 20 个 | ✅ | ✅ | ✅ |
| 日本語 | ❌ | ❌ | ❌ | ✅（>100 字符） |
| 한국어 | ❌ | ❌ | ❌ | ✅（>100 字符） |

## 设计

### 方案一：规则层四语适配（~40 行改动）

`agent/middlewares/task_intent/core.py` 扩展三组列表：

#### 1a. 任务关键词

```python
_TASK_KEYWORDS = {
    # English
    "implement", "fix", "create", "add", "refactor", "build", "deploy",
    "test", "update", "migrate", "write", "setup", "configure",
    "integrate", "optimize", "debug", "resolve", "enhance", "rewrite", "convert",
    # 中文
    "实现", "修复", "创建", "添加", "重构", "构建", "部署",
    "测试", "更新", "迁移", "编写", "设置", "配置",
    "集成", "优化", "调试", "解决", "增强", "重写", "转换",
    # 日本語（漢字 + カタカナ + ひらがな）
    "実装", "修正", "作成", "追加", "リファクタ", "構築", "デプロイ",
    "テスト", "更新", "移行", "記述", "設定", "最適化",
    "デバッグ", "解決", "強化", "書き直し", "変換", "統合",
    # 한국어
    "구현", "수정", "생성", "추가", "리팩토링", "빌드", "배포",
    "테스트", "업데이트", "마이그레이션", "작성", "설정",
    "통합", "최적화", "디버그", "해결", "개선", "재작성", "변환",
}
```

日语关键词选择依据：
- 漢字词（`実装`/`修正`/`作成`/`構築`/`更新`/`移行`/`記述`/`設定`/`最適化`/`解決`/`強化`/`書き直`/`変換`/`統合`）——日语技术文档最常用的动词名词
- カタカナ（`リファクタ`/`デプロイ`/`テスト`/`デバッグ`）——外来语，日语开发者必用
- 用 `リファクタ` 而非 `リファクタリング`：子串匹配，`リファクタリング` 包含 `リファクタ`

韩语关键词选择依据：
- 漢字词对应韩文（`구현`/`수정`/`생성`/`추가`/`리팩토링`/`빌드`/`배포`/`테스트`/`업데이트`/`마이그레이션`/`작성`/`설정`/`통합`/`최적화`/`디버그`/`해결`/`개선`/`재작성`/`변환`）——韩语技术社区标准用词

#### 1b. 问题模式

```python
_QUESTION_PATTERNS = [
    # English
    r"^(what|how|why|where|who|when|can you|is it|are you|do you)\b",
    r"^(explain|describe|tell me about)",
    # 中文
    r"^(什么是|怎么|为什么|哪里|谁|什么时候|能否|是否|是不是|能不能)",
    r"^(解释|说明|介绍一下)",
    # 日本語
    r"^(何が|どうやって|なぜ|どこに|誰が|いつ|できますか|ですか|教えて)",
    r"^(説明|解説|紹介して)",
    # 한국어
    r"^(무엇이|어떻게|왜|어디에|누가|언제|할 수 있|인가요|알려주세요)",
    r"^(설명|소개해)",
]
```

#### 1c. 闲聊模式

```python
_CHAT_PATTERNS = [
    # English
    r"^(hello|hi|hey|thanks|thank you|ok|good|great|bye)\b",
    # 中文
    r"^(你好|谢谢|好的|再见|嗯|哦)",
    # 日本語
    r"^(こんにちは|ありがとう|お疲れ様|はい|さようなら|うん|ええ|おはよう)",
    # 한국어
    r"^(안녕하세요|감사합니다|네|좋아요|안녕히|응)",
]
```

#### 1d. `is_long` 阈值保持 100

日语/韩语的信息密度比英文高（一个汉字 ≈ 多个英文字母的语义），100 字符的日语/韩语消息已经是很详细的工作请求。不需要调阈值。

### 方案二：LLM 兜底层（~80 行新增）

#### 2a. 触发条件

规则层判 False 时，如果消息**看起来像工作但没命中关键词**，调辅助 LLM 做一次意图分类。

```python
def _should_try_llm(content: str) -> bool:
    """规则没判定为任务，但消息看起来不像问题/闲聊 → 值得花一次 LLM 调用。"""
    text = content.strip()
    if len(text) < 20:
        return False  # 太短，规则已经判了（问题/闲聊/关键词）
    if len(text) > 100:
        return False  # 长消息已经走了 is_long 兜底
    # 20-100 字符、没命中关键词、没命中问题/闲聊模式 → 不确定区
    # 检查是否含动词性 token（像工作请求）
    return _looks_like_request(text)

def _looks_like_request(text: str) -> bool:
    """消息含命令式/请求式语气（~して/~해줘/please/帮我）→ 值得 LLM 判。"""
    request_markers = [
        "please", "帮我", "帮忙", "帮忙看", "看一下", "处理一下",
        # 日本語
        "して", "してください", "お願い", "やって", "直して", "してほしい",
        # 한국어
        "해줘", "해주세요", "부탁", "처리", "봐줘",
    ]
    text_lower = text.lower()
    return any(marker in text_lower for marker in request_markers)
```

只在 20-100 字符区间、没命中关键词、但含请求语气词时触发。太短（< 20）或太长（> 100）都不调 LLM——太短规则已判完，太长 `is_long` 兜底了。

#### 2b. LLM 意图分类

```python
async def _llm_classify_intent(content: str) -> bool:
    """调辅助 LLM 判定意图：work_request / question / chat。

    返回 True = 是工作请求（应注入 steering）。
    Fail-open：任何异常返回 False（不阻断 turn）。
    """
    try:
        from models import auxiliary_llm
        llm = auxiliary_llm()
        prompt = (
            "Classify the user's message into one category. "
            "Respond with ONLY the category name, nothing else:\n"
            "- work_request: asks to implement, fix, build, configure, or perform a development task\n"
            "- question: asks for explanation or information\n"
            "- chat: greeting, thanks, or casual conversation\n\n"
            f"Message: {content[:500]}"
        )
        response = await llm.ainvoke(prompt)
        result = str(response.content).strip().lower()
        return "work_request" in result
    except Exception:
        logger.debug("LLM intent classification failed; treating as non-task", exc_info=True)
        return False  # fail-open
```

#### 2c. 接入 `_detect_task_intent`

把纯函数改成 async + 带 LLM 兜底：

```python
async def _detect_task_intent_with_llm(content: str) -> bool:
    """规则优先 → 规则不确定时 LLM 兜底。"""
    # 第一层：规则
    rule_result = _detect_task_intent(content)
    if rule_result:
        return True  # 规则判是任务 → 直接是，不调 LLM

    # 规则判不是任务 → 检查是否值得调 LLM
    if not _should_try_llm(content):
        return False  # 规则已确定（太短/问题/闲聊）→ 不调 LLM

    # 不确定区 → LLM 兜底
    return await _llm_classify_intent(content)
```

中间件 `abefore_model` 改为调 async 版：

```python
async def abefore_model(self, state, runtime=None):
    ...
    if not await _detect_task_intent_with_llm(content):
        return None
    ...
```

#### 2d. 性能影响

| 场景 | 调 LLM？ | 延迟 | 成本 |
|---|---|---|---|
| 短消息 + 命中关键词 | ❌ 规则直接判 True | 0ms | 0 token |
| 短消息 + 问题/闲聊模式 | ❌ 规则直接判 False | 0ms | 0 token |
| 长消息（>100 字符） | ❌ `is_long` 直接判 True | 0ms | 0 token |
| 20-100 字符 + 含请求语气 + 无关键词 | ✅ 调辅助 LLM | 0.3-1s | ~50 token |
| 20-100 字符 + 不含请求语气 | ❌ `_should_try_llm` 返回 False | 0ms | 0 token |

**绝大多数消息不调 LLM**——只在"看起来像工作请求但规则没命中"的窄区间才花一次调用。每次调用 ~50 token（prompt + response），辅助 LLM（auxiliary_llm，通常是轻量模型）成本可忽略。

### 方案三：防抖（避免每条消息都调 LLM）

LLM 兜底只在**session 未 armed**时触发。一旦 session 已 armed（注入过 steering），后续消息不再调 LLM——用 short reminder 即可：

```python
async def abefore_model(self, state, runtime=None):
    ...
    is_armed = session_id in _armed_sessions

    if is_armed:
        # 已 armed → 不检测意图，直接 short reminder
        if await _detect_task_intent_with_llm(content):  # 只在 armed 后仍检测
            return {"messages": [_task_intent_message(_TASK_STEERING_REMINDER)]}
        return None
    else:
        # 未 armed → 检测意图（规则 + LLM 兜底）
        if not await _detect_task_intent_with_llm(content):
            return None
        _armed_sessions.add(session_id)
        return {"messages": [_task_intent_message(_TASK_STEERING_PROMPT)]}
```

实际上，一旦 armed 后，规则层的 short reminder 已经够用（`_TASK_STEERING_REMINDER` 只说"orchestrator mode is active, re-read it"）。不需要 LLM 再判——已 armed 的 session 默认后续消息都是工作请求。

**简化**：LLM 兜底只在**首次 arming 前**触发。armed 后只走规则层（关键词 + is_long）。

## 改动清单

| 文件 | 改动 | 行数 |
|---|---|---|
| `agent/middlewares/task_intent/core.py` | 扩展关键词/模式（四语）+ LLM 兜底函数 + 接入 | ~120 改动 |
| `tests/agent/middlewares/test_task_intent.py` | 新增四语关键词测试 + LLM 兜底测试 | ~80 |

**合计 ~200 行**

## 验收

### 规则层四语

- "fix the bug"（EN 短消息）→ 命中 `fix` → 是任务
- "修复这个bug"（ZH 短消息）→ 命中 `修复` → 是任务
- `バグを修正して`（JA 短消息）→ 命中 `修正` → 是任务
- `버그 수정해줘`（KO 短消息）→ 命中 `수정` → 是任务
- `実装をお願いします`（JA）→ 命中 `実装` + `お願い` → 是任务
- `구현해주세요`（KO）→ 命中 `구현` + `해주세요` → 是任务
- "what is X"（EN）→ 命中问题模式 → 不是任务
- `何が起きてるの`（JA）→ 命中 `何が` → 不是任务
- `무엇이 문제인가요`（KO）→ 命中 `무엇이` → 不是任务
- `こんにちは`（JA）→ 命中闲聊模式 → 不是任务
- `안녕하세요`（KO）→ 命中闲聊模式 → 不是任务

### LLM 兜底层

- 20-100 字符 + 含请求语气 + 无关键词命中 → 调辅助 LLM → LLM 判 work_request → 注入 steering
- 20-100 字符 + 不含请求语气 → 不调 LLM → 不注入
- < 20 字符 → 不调 LLM（规则已判）
- > 100 字符 → 不调 LLM（is_long 兜底）
- 已 armed 的 session → 不调 LLM（规则 short reminder）
- LLM 调用失败 → fail-open 不注入（不阻断 turn）

## 风险

- **LLM 兜底的延迟**：20-100 字符的不确定区消息会多 0.3-1s。只在首次 arming 前触发，armed 后不再调。可接受。
- **LLM 误判**：辅助 LLM 可能把闲聊判成 work_request → 注入不必要的 steering → LLM 自己忽略（steering prompt 里写了"If this is a question: respond directly — ignore this directive"）。可接受。
- **关键词不全**：四语关键词是人工选的 20 个高频词，不可能覆盖所有工作动词。LLM 兜底覆盖了关键词漏检的窄区间（含请求语气但无关键词命中）。长消息走 is_long 兜底。三层覆盖。
- **日语/韩语正则**：`re` 模块对 Unicode 字符的正则匹配是字符级的，不会切碎多字节字符。安全。

## 不做的事

- **不做 NLU/语义理解**——不把意图分成"开发/研究/测试/部署"等子类。只判"是/不是工作请求"。
- **不做 per-session 意图模型微调**——规则 + 一次 LLM 调用足够，不引入 ML 模型。
- **不做意图历史**——不记录"这个 session 的意图是什么"用于后续决策。armed flag 是唯一的 session 状态。
- **不做规则的热重载**——关键词是硬编码的，改需要改代码。不引入配置文件管理关键词列表（40+20 词不值得一个配置体系）。

## 关键文件路径

| 文件 | 角色 |
|---|---|
| `agent/middlewares/task_intent/core.py` | 修改：四语关键词/模式 + LLM 兜底函数 + 接入 |
| `models/LLMs/auxiliary_llm.py` | 复用：辅助 LLM 实例 |
| `tests/agent/middlewares/test_task_intent.py` | 修改：四语测试 + LLM 兜底测试 |

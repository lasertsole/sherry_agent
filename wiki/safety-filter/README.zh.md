# 直播安全过滤器

> 输入/输出关键词规则、可选的 LLM 审核，以及"先扫描再发送"的流式截断。

## 🎯 概述

本特性之前，模型与观众之间没有任何文本过滤。过滤器按开关（`sherry.jsonc`
`safety_filter.enabled`，默认关闭）叠加三层能力：

1. **输入关键词规则** —— 命中 `block` 时直接拒绝，**不调用模型**
   （`agent/middlewares/safety_filter/middlewares.py::InputSafetyFilter`）。
2. **LLM 审核（可选）** —— 由配置的审核模型判定；必须同时满足总开关打开、
   策略不是 `off`、以及填写了模型名，审核才会真正执行。
3. **输出关键词规则** —— 完整回答被整体替换或部分替换（`OutputSafetyFilter`），
   流式回答则由 `agent/middlewares/safety_filter/stream_guard.py` 逐块清洗。

关键词表由使用者自己维护：应用**不内置任何词表**（这属于法律判断，不由本项目替你决定），
规则文件位于工作目录下、文件名由 `safety_filter.keyword_file` 指定。

## ⚙️ 配置

`sherry.jsonc`，**实时读取**（按 mtime 缓存，无需重启）：

- `safety_filter.enabled` —— 总开关。
- `safety_filter.llm_moderation_mode` —— `off` / `on_keyword_hit` / `always`。
- `safety_filter.llm_moderation_model` —— 审核模型名；留空表示无论策略如何都不启用 LLM 审核。
- `safety_filter.keyword_file` —— 工作目录内的规则文件名。

设置界面在 **系统配置 → 直播安全**（`SafetyFilterPanel.vue`）：总开关、LLM 开关
（未填模型名时禁用）、审核策略选择、带试扫的规则编辑器，以及最近的过滤记录。

## 🧩 三层规则

- 规则格式：`{"rules": [{"pattern": "…", "type": "exact|contains|regex",
  "action": "block|replace|warn", "scope": "input|output|both",
  "replacement": "…"}]}`。保存前会校验规则条数、pattern 长度、替换文本长度和正则合法性。
- `block` 拒绝/整体替换；`replace` 替换命中片段；`warn` 仅上报。输入端永远不改写用户原文。
- 每次判定记录一条事件（有上限的环形缓冲，最新在前）并推送 `safety_filter` WS 帧，
  前端弹 toast（block 为错误级）。
- LLM 审核**失败即放行**：超时、请求失败或无法解析的答复都会让本轮继续并记警告——
  provider 自身的对齐仍在链路中。

## 🚂 流式清洗

`StreamTextGuard` 保留最后 `stream_buffer_window` 个字符（默认 20），只放出已经扫过的文本：

- 缓冲内任意位置命中 `block` 即闩锁，丢弃缓冲文本并只发一次替换提示；
- 结束位置落在可放行前缀内的 `replace` 命中会先替换；被放行点切开的命中会把放行点回退到它的起点；
- 长于窗口的模式只能在尾扫描（flush）时完成匹配，因此最多 `len(match) - 1` 个字符会在截断前可见。

流式回答不经过 `OutputSafetyFilter`：清洗发生在帧生成之前，客户端与持久化行因此一致。

## 🌐 HTTP 接口

| 方法 | 路径 | Body / query |
| :--- | :--- | :--- |
| `GET` | `/safety-filter/rules` | — |
| `PUT` | `/safety-filter/rules` | `{rules: [...]}` |
| `POST` | `/safety-filter/test` | `{text, scope}` |
| `GET` | `/safety-filter/log` | `?session_id=<sid>&limit=50` |

各开关与审核模型是 `sherry.jsonc` 的键，通过 `PUT /sherry-config` 写入，没有单独的端点。

## 🧪 测试

`tests/agent/middlewares/safety_filter/test_rules.py`（校验、作用域、热重载、字面替换）、
`test_middlewares.py`（输入拦截不调模型、warn/replace 放行、LLM 门控与失败放行、输出替换）、
`test_stream_guard.py`（保留窗口、闩锁、发送前替换、尾扫描）。设置界面由
`client/app/pages/home/components/__tests__/SafetyFilterPanel.integration.test.ts` 固定。

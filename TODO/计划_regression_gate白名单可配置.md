# 计划:regression_gate 白名单可配置

## 现状

`regression_gate.py:43-72` 的 `_ALLOWED_EXECUTABLES` 是代码里的 `frozenset`,覆盖 5 种语言
生态(Python / JS-TS / Rust / Go / C-C++)。要加一个新语言(Java/gradle、Ruby/rspec、
C#/dotnet 等)必须改代码。`_safe_argv`(line 97-140)在 4 处引用它(line 116/128/134/138)
做安全检查——只允许白名单内的可执行文件在 finish 回放时自动执行。

## 先例

YOLO deny list(`path_utils.py:275-291`)已是同一模式的 list setting:

```python
def _get_yolo_deny_paths() -> list[str]:
    for pattern in (
        *HITL_DEFAULTS.get("yolo_deny_paths", []),    # 代码默认
        *load_sherry_list_setting("yolo_deny_paths"),  # 用户从 sherry.jsonc 加
    ):
        ...
```

`load_sherry_list_setting`(sherry_settings.py:144-162)的文档明确写了:list setting
**不在** `SHERRY_SETTING_DEFAULTS` 里注册——caller 自带默认,合并用户条目。缺失 key /
非 list / 解析失败都返回空列表。

## 改动

### 1. regression_gate.py 加 `_get_allowed_executables()`

```python
def _get_allowed_executables() -> frozenset[str]:
    """代码默认 + sherry.jsonc 用户条目合并(同 YOLO deny list 模式)。"""
    from config.sherry_settings import load_sherry_list_setting

    merged: set[str] = set(_ALLOWED_EXECUTABLES)
    for name in load_sherry_list_setting("regression_gate_allowed_executables"):
        if isinstance(name, str) and name.strip():
            merged.add(name.strip().lower())
    return frozenset(merged)
```

### 2. `_safe_argv` 4 处引用改为 `_get_allowed_executables()`

| 行 | 原 | 改后 |
| --- | --- | --- |
| 116 | `executable not in _ALLOWED_EXECUTABLES` | `executable not in _get_allowed_executables()` |
| 128 | `target not in _ALLOWED_EXECUTABLES` | `target not in _get_allowed_executables()` |
| 134 | `_executable_name(argv[2]) not in _ALLOWED_EXECUTABLES` | `... not in _get_allowed_executables()` |
| 138 | `_executable_name(argv[1]) not in _ALLOWED_EXECUTABLES` | `... not in _get_allowed_executables()` |

### 3. sherry.jsonc 加示例

```json5
// 回放门白名单扩展:在代码默认 5 种语言之外追加的可执行文件名(小写,
// 如 gradle / maven / dotnet / rspec / swift)。只影响 finish 回放时自动重放
// 哪些验证命令,不影响子代理能记录哪些命令。
"regression_gate_allowed_executables": [],
```

### 4. 后端:list-settings API(前端可配的前提)

现有 `/sherry-config` 只支持标量(`dict[str, str]`):`read_sherry_config` 只 yield
`SHERRY_SETTING_DEFAULTS` 里的 key(sherry_config.py:60),`write_sherry_config` 只接受
`SHERRY_SETTING_KEYS`(line 115)且值为 `str`(line 120)。list settings 被故意排除在
scalar registry 外(`load_sherry_list_setting` 文档:"intentionally outside the typed
scalar registry")——前端无法通过现有 API 读写。

加通用的 list-settings 端点(不碰现有标量 API):

- `server/service/sherry_config.py` 加 `read_sherry_list(key) -> list[str]`(复用
  `load_sherry_list_setting` 逻辑)和 `write_sherry_list(key, values: list[str])`
  (复用 `_scan_lines` 的 line-wise 编辑模式,保留注释/顺序,值写为 JSON array)。
- `server/trigger/` 加路由:
  - `GET /sherry-config/list/<key>` → `{"key": "...", "values": [...]}`
  - `PUT /sherry-config/list/<key>`(body `{"values": [...]}`)→ 写入 sherry.jsonc
- key 白名单:只接受预定义的 list-setting key 名(`regression_gate_allowed_executables`、
  `yolo_deny_paths`),拒绝任意 key——避免前端写出意外键。
- 通用端点:未来 `yolo_deny_paths` 等其他 list settings 也能用同一个 API + 同一个 UI 组件。

### 5. 前端:系统配置面板加白名单编辑器

- `client/app/composables/sherryConfig.ts` 加 `readSherryList(key)` /
  `writeSherryList(key, values)`(调上面的 API)。
- 系统配置面板加一个"回放门白名单"编辑器(XUI tag input / chip list):
  - 用户加/删可执行文件名,保存时调
    `writeSherryList("regression_gate_allowed_executables", values)`。
- 编辑的是用户配置部分(sherry.jsonc 里的 list);代码默认的 `_ALLOWED_EXECUTABLES`
  不可删(不在 sherry.jsonc 里)。前端不需要展示默认条目——用户只需要知道"要追加什么"。
- 输入校验:小写、不含 shell 元字符(`;&|<>$```)、长度 ≤ 32。

## 不需要改的

- **`_PYTHON_MODULES` / `_PACKAGE_RUNNERS` 子分类**:这些是 `_safe_argv` 的解析逻辑
  (`python -m <module>` 要查 module 名、`npm test` 要查 argv[1] 是 test/run/exec),
  不是配置项。大多数新语言(gradle/maven/dotnet/rspec)是简单 `argv[0]` 匹配,走
  `_safe_argv` 的 `return argv` 终态,不需要子分类。只有带子命令的 runner(如
  `gradle :app:test`)如果需要校验子命令才加——但 `_SHELL_META` 已经拒绝了带 `:`
  之外的 shell 元字符的命令,`gradle test` 不含元字符,直接放行。
- **`_SHELL_META` regex**:安全边界(拒绝 `;&|<>` 等),不配置化。
- **cap / timeout**:已有 `EVIDENCE_REGRESSION_GATE` 配置(`max_reverify_commands` /
  `reverify_timeout_s`),不需要新配置。
- **`SHERRY_SETTING_DEFAULTS`**:list setting 不注册(同 YOLO deny list),`load_sherry_list_setting`
  对缺失 key 返回空列表。

## 安全考量

- 白名单是回放安全边界(只回放子代理自己记录为 passed 的验证命令,不是任意命令);
- 用户配自己的 `sherry.jsonc` 是用户信任域——用户不会给自己下毒;
- 回放门还有 `_SHELL_META` 拒绝(有 shell 元字符的命令不回放)+ cap 10 + 60s timeout
  fail-open 兜底;
- 用户加不安全条目(如 `rm`)的风险有限:回放只重放子代理记录为 passed 的命令——
  子代理不会把 `rm -rf /` 记成验证命令。

## 测试

- 默认不配 `sherry.jsonc` 时行为不变(`load_sherry_list_setting` 返回空,合并结果 ==
  `_ALLOWED_EXECUTABLES`)。
- 配 `"regression_gate_allowed_executables": ["gradle"]` 后 `gradle test` 被 `_safe_argv`
  放行(argv[0]="gradle" 在合并后的白名单里 → return argv)。
- 不在白名单且不在用户配置里的(`ruby`/`php`)仍返回 `None`(记 skipped)。
- `_SHELL_META` 仍拒绝 `gradle test && rm -rf /`(`&&` 命中)。

<!-- FACTS.md: broad, module-independent pitfalls and conventions. Keep entries short — the oldest roll off at the char limit. -->
§
`find | sort` 使用 locale 排序（忽略标点/大小写），不是 ASCII 顺序（如 `__init__.py` 会排在 `IDENTITY.md` 之后）。比较两份文件清单行数时不要靠顺序推断结构，两个统计数字用同一条命令管道（`| wc -l`）获取才可比。
# MEMORY
§
Project wiki path: C:\app\code\project\EMA_AI_agent\src\data\wiki\ (not data/wiki/). User prefers wiki data under src/data/ rather than project root data/. This was a correction applied during llm-wiki skill adaptation.
§
本项目技能目录名可能包含连字符（如llm-wiki），Python的import语句不支持含连字符的包名。解决方案：使用importlib.util.spec_from_file_location()动态加载模块，而非标准import语句。模式：在__init__.py中用importlib加载各子模块并重新导出，子模块间引用也用importlib动态加载core模块。
§
llm-wiki SKILL.md中的导入示例有误：`from skills.builtin.llm-wiki.scripts import xxx` 在Python中会因目录名含连字符而报SyntaxError。需要修正为importlib动态加载模式。同时需要在SKILL.md的Pitfalls中补充说明：技能目录名含连字符时，所有Python导入必须用importlib，不能用标准import语句。
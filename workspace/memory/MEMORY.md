# MEMORY
§
## 图片refresh后丢失(/media 404)诊断结论（2026-08-11）
**根本原因**：后端进程运行的是旧代码（媒体持久化逻辑尚未加载），而非代码缺陷本身。
**证据链**：
- `multimodal_processor.py` 新增写 media/ 逻辑（L163-171，写 SRC_DIR/<session_id>/media/<ts>.<ext> + additional_kwargs["images"]，L230-233）为**未提交新代码**
- DB(src/store/mes_memory/mes_memory.db) 288行中 images 全部为 None；turn-65 用户消息(id=279)实际写到 src/main/mutil_temp/（旧临时目录，非 media/），且 image_to_text ImportError
- `git status` 显示 media.py/media 持久化均为未提交改动 → 旧进程无 /media 路由+无媒体持久化
**已确认修复链路（磁盘上闭合）**：
- 写：multimodal_processor.py → additional_kwargs["images"] → core.py add_messages L76/L91 → DB images 列(JSON)
- serve：media.py /media?session_id=<sid>&filename=<basename>，读 SRC_DIR/<session_id>/media/，SRC_DIR=ROOT/src
- 前端：ChatBox.vue resolveImageSrc L164-176 — AI消息取basename走/media，用户消息(base64,无分隔符)本地渲染
**当前状态**：用户已重启后端(PID 28040)，/media 路由已注册生效（传不存在文件返回500非404即证明路由存活）。
**剩余1步验证**：真实上传一张图片走 agent → 确认 src/<session>/media/ 生成文件 + DB human 行 images 非NULL + /media serve 成功 + refresh 重渲染。
**注意**：turn-65 会话历史数据不完整（旧代码生成），无法作为修复验证样本，需用新会话新上传。

§
2026年7月18日全技能健康检查结果：共30个技能，24个正常运行，2个需安装依赖（docker/docker-build需Docker Desktop，kubectl-apply需kubectl客户端），4个社区技能为轻量版仅有名称描述。xp_graph知识图谱已记录51个自动技能索引。
§
xp_graph 工具：experience_trace 参数曾因代码bug导致直接传字典报错（NoneType），用户修复后已可正常抽取知识到知识图谱。无参数调用可列出自动技能列表，auto_skill_name 参数可查看自动技能详情。
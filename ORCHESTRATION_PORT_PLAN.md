# ORCHESTRATION_PORT_PLAN:openclaw 编排模式移植 sherry_agent(重建版)

> 重建说明:原计划文档(383 行,34848 字节,2026-08-31 交付)于 2026-08-31 至 09-01 之间被外部删除。本文档为 2026-09-01 重建版:规格依据实施上下文与落地交付物回填,行数与字节不承诺与原版一致,一切以仓库实际交付物与第 5 章终验回执为准。
> 状态:四项全部实施完成,全量验证通过(2026-09-01)。

## 0. 执行摘要与环境前置

### 0.1 执行摘要

本计划把 openclaw 的四项编排能力移植到 sherry_agent,统一约束:纯协议层优先(能不用运行时代码就不用),全部技能 SKILL.md 中文行文 + 英文标识符,无 emoji,不加任何新依赖,测试随项交付并以 TDD 方式先行(先 RED 后 GREEN,保留两次 pytest 摘要)。

| 项 | 名称 | 形态 | 落位 |
| --- | --- | --- | --- |
| Item 1 | TaskFlow(durable 多步任务流) | 运行时代码 + 技能 | agent/tools/taskflow/ + skills/taskflow/ + tests/unit/taskflow/ |
| Item 2 | auto-qa(自动修 QA 失败项波次编排) | 纯协议技能 | skills/auto-qa/ + tests/module/test_auto_qa_skill.py |
| Item 3 | repair-sweep(修复清扫舰队) | 纯协议技能 | skills/repair-sweep/ + tests/module/test_repair_sweep_skill.py |
| Item 4 | gh-pipeline(GitHub issue 修复管线) | 纯协议技能 | skills/gh-pipeline/ + tests/module/test_gh_pipeline_skill.py |

### 0.2 环境前置(2026-08-31 实测)

- gh CLI 未安装。Item 4 必须双路径:Path A(winget install GitHub.cli + gh auth login + gh auth status),Path B(REST v3 匿名 60 req/h,公开仓可用,私有仓无效,建 PR 必须携带 token)。
- git 2.55.0.windows.2;uv 0.11.19;Python 3.13(.venv 由 uv 管理);PowerShell 5.1(GBK 代码页,禁用其文本 cmdlet 读写 UTF-8 文件)。
- aiosqlite / loguru / pydantic 均已在依赖内;四项一律不加新依赖。
- 全量终验入口:scripts/run_tests_split.py(双进程隔离,Group A=tests/unit,Group B=tests/integration + tests/system + tests/module)。
## 1. Item 1:TaskFlow 运行时(durable 多步任务流)

### 1.1 目标与映射

把 openclaw managedFlows 的 API 面移植为 sherry_agent 的主会话工具族,使其在跨轮次长任务上获得:持久状态、乐观锁并发检测、步骤派发给 detached 子会话、等待与恢复、终态封存。映射关系:taskflow_create=createManaged,taskflow_run_task=runTask,taskflow_set_waiting=setWaiting,taskflow_resume=resume,taskflow_finish=finish,taskflow_fail=fail,taskflow_cancel=requestCancel/cancel,taskflow_summary=getTaskSummary。

### 1.2 持久层(agent/tools/taskflow/registry/store_sqlite.py)

- 数据库:agent/tools/taskflow/data/taskflow_registry.db;表 task_flows(flow_id TEXT PK, state_json TEXT NOT NULL, wait_json TEXT, expected_revision INTEGER NOT NULL, status TEXT NOT NULL, child_session_key TEXT)。
- 蓝本:agent/tools/subagent/registry/store_sqlite.py。每条连接(aiosqlite 与 stdlib sqlite3 皆然)的首语句或连接参数即 5s busy_timeout(:44-45),争锁等待而不是直接抛 database is locked。
- WAL 切换一次性进行且容忍 OperationalError(:165-192):journal-mode 切换不保证遵守 busy_timeout,并发初始化时重查并降级继续,init 绝不因 journal mode 失败;稳态(已是 WAL)后完全跳过。
- 懒初始化:asyncio.Lock 只由属主事件循环触碰(_init_loop 机制,:203-234),外来循环轮询 _initialized 并在属主未完成时兜底自初始化,避免跨线程释放锁把别的循环睡死;同步路径以 threading.Lock 守一次建表(:237-256)。
- 乐观锁:一切变更走 UPDATE ... WHERE flow_id = ? AND expected_revision = ?;零行命中即冲突,FlowConflictError 携带 latest_revision(:108-123),错误文本直接给出重试值,调用方无需二次查询。FlowExistsError 与 FlowNotFoundError 语义分明。
- 状态机:config.py 的 TaskFlowStatus:running / waiting / done / failed / cancelled;done/failed/cancelled 为终态,TERMINAL_STATUSES 冻结,终态流拒绝一切变更。初始 revision=1(INITIAL_REVISION),每次变更恰好加一。

### 1.3 工具族(agent/tools/taskflow/tools/,8 个)

- taskflow_create(flow_id, description, initial_state):建流,初始 running,revision=1;重复 id 返回既有 revision 并提示以 taskflow_summary 重读。
- taskflow_run_task(flow_id, task, label, expected_revision):登记步骤并经既有 spawn 入口(spawn_subagent_direct,expects_completion_message=True)派发 detached 子会话;child_session_key 落库;结果经既有 announce/settle-wake 管线自动回流,不建 taskflow 本地回调。派发函数为模块级可注入缝 _dispatch_child(:50),测试以 monkeypatch 替换,不触碰 spawn 管线。
- taskflow_set_waiting(flow_id, wait_reason, expected_revision):置 waiting 并记录 wait 载荷;resume 时清除。
- taskflow_resume(flow_id, child_session_key, result, expected_revision):把子会话结果注入流状态,waiting 回 running,running 保持 running;同一 (child_session_key, result) 幂等,不二次注入,revision 不变。
- taskflow_finish / taskflow_fail / taskflow_cancel:终态封存(附 summary 或 reason),终态不可再变更。
- taskflow_summary(flow_id):只读回读状态、revision、child_session_key、步骤与结果,兼作冲突后的重读入口。
- 错误契约:工具绝不向 LLM 抛业务异常,一律返回 Error: 前缀的可读文本;冲突文本内嵌最新 revision。共享逻辑收敛在 _shared.py。
### 1.4 注册与可见性

- agent/tools/__init__.py:仅追加两行(from .taskflow import build_taskflow_tools 与 _MAIN_TOOLS_BUILDERS 追加 build_taskflow_tools),其余零改动。
- build_taskflow_tools()(tools/__init__.py:32-44):组装 8 工具,handle_tool_error=True 兜底,metadata 为 scope=main_only。共享流状态由主会话管理,子代理 tool-policy 无条件丢弃该族工具。
- skills/taskflow/SKILL.md:frontmatter name: taskflow,scope: main_only;正文覆盖工具一览、乐观锁冲突重试三步(以 taskflow_summary 重读、基于最新状态重放、冲突文本里的 expected_revision 即重试值)、状态机与幂等 resume 语义。

### 1.5 验收标准(实施结果全部满足)

1. 测试落位 tests/unit/taskflow/(Group A);uv run pytest tests/unit/taskflow -q 为 25 passed。
2. store 层与蓝本同进程共存无污染:与 tests/unit/subagent/test_store_sqlite.py 同批运行通过;conftest 提供 isolated_db(tmp_path 重定向 DB 路径并重置一次初始化态,含每测试新建 asyncio.Lock,因 pytest-asyncio 每测试换循环)。
3. stub 容忍:conftest 以私有名加载真实 skills/loader 与真实 agent/tools 包(_real_skills_loader/_real_agent_tools/_fix_stub_run_async),Group A 全上下文(tests/unit/subagent 的 conftest 已装入 sys.modules stub)下依然通过。
4. 乐观锁并发用例:双写一胜一败,败方收到含 latest revision 的冲突;幂等 resume 用例:重复 (child_session_key, result) 不二次注入且 revision 不变。
5. 注册验收:build_main_tools() 结果含 8 个 taskflow_* 工具(经 _MAIN_TOOLS_BUILDERS)。
6. SKILL.md 验收:frontmatter/scope/工具一览/冲突重试协议齐备,scope: main_only 经 loader 可见性契约断言(main 可见,subagent 不可见)。

## 2. Item 2:auto-qa 协议技能

### 2.1 campaign 目标与计数纪律

- 默认目标 100 个 verified fixes;计数只认证据台账中的合格条目,其余来源一律不采信;修复 agent 自称修完不计入,无台账合格条目支撑的完成声明一律视为未完成。

### 2.2 波次状态机

- 每 wave 依次经过 dispatching -> collecting -> reviewing -> done 四态;状态与派发清单落盘 .omo/evidence/auto-qa/<wave>/wave-state.md,由主会话维护,子代理不得写入。
- reviewing 状态必须完成台账复盘后才可切 done 并开启新 wave;复盘未完成不得进入下一波。

### 2.3 lane 划分与容量限流

- 按子系统/目录把失败项划分到 lane(例:auth、api、ui);lane 与 swarm group 一一对应,每 lane 配置一个 SwarmGroupConfig。
- 单组 lane 数(组内子代理数)不超过 max_children_per_group=5(types/swarm.py:27);组数 = ceil(N/5),N 为本波失败项 lane 总数;组内并发 max_concurrent=3(types/swarm.py:29)。
- 超订 spawn 停留在 swarm 队列 RESERVED(collector.py:86-92),由 FIFO 自然接管;波内不追加派发,不丢弃,不加塞。
### 2.4 派发模板与证据台账

- 每 lane 一条 sessions_spawn:task_name 为 autoqa-w<n>-<lane>(n 为波次号),mode="run"(一次性执行,结果经 announce 管线回传主会话),cleanup="keep"(保留子代理会话与上下文供复盘追查);task 文本引用 references/roles.md 的可写修复 agent 协议并要求修复后执行自验命令回填台账。
- 证据台账:.omo/evidence/auto-qa/<wave>/<fix-id>.md,每修复项一个文件,五必填字段:PID(live 进程探查结果)、baseline SHA(冻结基线 commit)、复现命令、验证输出(自验命令原始输出原文贴入)、评审结论。
- 准入规则:PID 与 baseline SHA 缺一即不计入 verified fixes。
- 每波复盘先查台账完整性:核对 wave-state.md 派发清单中每个 fix-id 是否都有台账文件、五必填字段是否齐全,核对通过后再统计 verified fixes。

### 2.5 显式前置依赖与降级

- 前置依赖:sessions_spawn 工具 schema 的 tool_allow/tool_deny 扩参(SUBAGENT_PORT_PLAN 2.2 项)尚未落地。
- 降级路径:扩参落地前,只读评审由主会话代查(主会话以只读方式执行检查命令、分析根因并填写台账评审结论字段);references/roles.md 的只读评审 agent 模板暂不派发,仅作为扩参落地后的目标协议保留。扩参落地后,评审改为派发只读评审 agent(带 tool_deny 清单)。

### 2.6 验收标准

- tests/module/test_auto_qa_skill.py(30 断言):loader 发现与 main_only 可见性契约;SKILL.md 内容断言(campaign 目标与计数纪律、四态波次状态机、lane 划分与 5/3 容量坐标与 RESERVED、派发模板三参数、台账五必填字段与准入规则、限流与复盘完整性、前置依赖与降级路径);references/roles.md 双模板断言(只读评审:tool_deny 清单、固定回报格式、输入约束;可写修复:允许工具、自验纪律、根因纪律)。

## 3. Item 3:repair-sweep 协议技能

### 3.1 参数模型

- scope 三档:refs(给定符号/引用清单,从代码引用反查修复点)、discovery(主动扫描失败项与待修 issue,默认档)、queue(从外部队列文件灌入,如 .omo/repair-sweep/queue.md)。
- batch_size:默认 5,上限 20;请求超过 20 时编排者直接拒绝该轮并提示拆批,不产生任何派发。
- workers:按 scope 默认 refs 8 / discovery 8 / queue 64;受 swarm 容量约束,每 5 workers 一组(max_children_per_group=5),组内并发 3(max_concurrent=3);queue 档 64 workers 约拆 13 组。

### 3.2 六步流程

队列构建 -> 批次切片 -> per-worker spawn -> 收集 -> 质疑处理 -> 汇总与 worktree 清理。切片内按文件/目录去重,同一文件不进两个 worker;scope=queue 时批次数 = ceil(条目数 / batch_size),切片之间无重叠。worker 结束后经 announce 回流,主会话逐一核对回报并更新台账。
### 3.3 worker 模板与授权清单(spawn 时填充)

- 每 worker 一次 sessions_spawn,prompt 内嵌专属 worktree 路径(../repair-sweep-wt/<item-id>)与授权清单;全程只在该路径内工作,禁止改主工作区。
- 直接授权:investigate / fix / commit / push / PR / comment(调用即授权,无需逐项确认);需复核:land 与 close 默认必须回传 orchestrator 复核,复核通过才执行。
- 产出:在隔离 worktree 内创建分支 fix/<item-id> 并提交;回报必含分支名、变更摘要、自验命令及输出。
- 质疑通道:worker 对关闭决定有异议时,经 sessions_send 向 orchestrator 发起质疑(Challenge);orchestrator 必须回应,回应内容在台账留痕。

### 3.4 与 openclaw 的显式差异(收紧声明)

openclaw 调用即全量授权(含 land/close);本移植收紧 land/close 两项为需 orchestrator 复核。理由:land 直接进主干、close 终结 issue,两者不可逆且改变仓库公共状态;sherry 无人工确认链,默认收紧更安全,需全自动时显式打开并自担风险。

### 3.5 worktree 生命周期与兜底

- sweep 开始:编排者经 terminal 批量执行 git worktree add ../repair-sweep-wt/<item-id> -b fix/<item-id>,每 worker 一个专属 worktree。
- sweep 结束:批量 git worktree remove <path>(存在未合并产出时先标记保留),随后 git worktree prune。
- 清理验证是收尾门禁:git worktree list 输出中不再含本 sweep 创建的路径;清理验证未完成,不得宣布 sweep 结束。
- 失控 worker:用 sessions_kill 终止(control/kill.py:28-78,cascade 级联 :81-133)。任务偏离:swarm run 不接受 steer(control/steer.py:44-46 拒绝 swarm run),worker 纠偏一律 kill 加重派,不使用 steer。

### 3.6 台账

- 路径 .omo/repair-sweep/ledger.md,由主会话(orchestrator)维护。
- 每 item 记录:item-id、worker、worktree 分支、结果状态、质疑内容与 orchestrator 回应;回应必须留痕于台账。
- land/close 复核结论同样记入台账;复核未通过的任务记为 blocked,不进入 land/close。

### 3.7 验收标准

- tests/module/test_repair_sweep_skill.py(17 断言):loader 发现与 main_only 契约;15000 字符预算内;三档 scope 齐;batch_size 默认 5 上限 20 超过拒绝并拆批;workers 配比与 5/3 容量坐标;六步流程有序;切片去重(同一文件不进两个 worker)与 queue 档 ceil 切片无重叠;sessions_spawn 派发与 announce 回流;授权清单有序且 land/close 标注需复核;openclaw 差异显式声明;worktree 路径内嵌与禁改主工作区;worktree 四命令(add/remove/prune/list)有序且 list 为门禁;sessions_send 质疑加台账留痕;sessions_kill 兜底与拒绝 steer 重派语义。
## 4. Item 4:gh-pipeline 协议技能

### 4.1 双路径环境前置

- Path A(gh CLI 已装或可装):winget install GitHub.cli,随后 gh auth login 完成鉴权,再以 gh auth status 确认登录态。
- Path B(gh 不可用):走 REST v3 兜底;鉴权经 GH_TOKEN 环境变量注入,匿名访问仅限公开仓,限流 60 req/h(按 IP 计)。
- 与 openclaw 的差异:本移植以 gh CLI 为主路径,REST v3 作为文档化兜底,双路径互为降级;token 全程只经环境变量进入内存,绝不落盘。

### 4.2 四阶段

- Phase 1 仓库身份解析:执行 git remote get-url origin 取远端 URL;https://github.com/<owner>/<repo>.git 与 git@github.com:<owner>/<repo>.git 两种规范形态共用同一条正则解析出 owner 与 repo;解析结果非 GitHub 远端时直接终止管线。
- Phase 2 issue 拉取(双路径):gh 路径为 gh issue list --repo <owner>/<repo> --label <label> --limit <N> --state open --json number,title,body,按里程碑圈定时追加 --milestone;REST 兜底为 GET /repos/{owner}/{repo}/issues?labels=<label>&per_page=<N>&state=open;issues 端点同时返回 PR 条目,过滤 pull_request 字段非空的条目,只保留真 issue;匿名限流 60 req/h,小批量够用。
- Phase 3 worker 派发:每 issue 一次 sessions_spawn,模板四要素按序填充:issue 编号、issue 标题、issue 正文、分支 gh-pipeline/<issue-number>(从 main 切出,所有提交只落该分支,禁止推送共享远端);工具边界 tool_allow:read,write,patch,python_repl,terminal 与 tool_deny(subagent/spawn 类与直接建 PR 的写远端操作);回报经 announce 回流主会话,必含分支名、变更摘要、自验命令及输出。
- Phase 4 PR 创建(双路径):gh 路径 gh pr create --repo <owner>/<repo> --head gh-pipeline/<issue-number> --base main;REST 兜底 POST /repos/{owner}/{repo}/pulls 必须携带 token(GH_TOKEN 注入 Authorization 头),body 含 title/head/base 三字段;PR 描述引用 issue 编号与变更摘要,head 必须是 Phase 3 产出的分支。

### 4.3 执行参数

- --dry-run:零远程写,只解析仓库身份、列出待拉 issue 与待建 PR 计划,不 spawn、不建分支、不建 PR。
- --yes:跳过确认;默认主会话在 Phase 3 派发前展示批次计划等待用户确认,加 --yes 直接执行。
### 4.4 降级表(三行)

| 场景 | 执行路径 |
| --- | --- |
| 公开仓,小批量 | REST v3 匿名即可,60 req/h 配额够拉取小批 issue;建 PR 仍需 gh 或 token |
| 私有仓 | gh CLI 或 token 必选,匿名 REST 对私有仓无效 |
| 无 gh 且无 token | 只出分支,PR 手工由用户创建 |

### 4.5 风险与回滚

- token 安全:token 只经环境变量进入内存,绝不落盘,不写入台账、日志或 spawn prompt;禁止把 token 拼进命令行参数或经 echo 打印,回报与日志中一律脱敏。
- 限流处理:REST 返回 403/429 时整批暂停,连续两次限流则中止本轮;不重试风暴,确认配额恢复后再续跑。
- 自验门禁:回报缺自验命令输出的条目视为未完成,不进入 Phase 4,对应 PR 不创建。
- 版本注记:REST v3 端点与参数以 2026-08 查询的 GitHub REST API 文档为准,接口变更时先核对再执行。
- 回滚:删除 skills/gh-pipeline 目录即卸载本协议;已产出的 gh-pipeline/<issue-number> 分支经 git branch -D 删除。

### 4.6 验收标准

- tests/module/test_gh_pipeline_skill.py(22 断言):frontmatter 与 main_only 契约;Phase 1 至 4 有序;双路径命令串逐字锁定;per-line 语义规则(POST 与 token 同行、--dry-run 与零远程写同行、--yes 与跳过确认同行、token 与落盘/echo、403/429 与暂停中止、自验与不进入 Phase 4、2026-08、回滚与删除);降级表三行;spawn 模板七 token 有序。
## 5. 测试布局与终验

### 5.1 测试落位(双进程分组)

- Group A(tests/unit,独立进程):运行时测试 tests/unit/taskflow/,conftest 内建 isolated_db 与 stub 容忍层。unit 组含 sys.modules stub 的子代理测试(tests/unit/subagent/conftest.py 在 import 时装 stub),因此裸 import skills.loader 的测试禁止放入 tests/unit。
- Group B(tests/integration + tests/system + tests/module,独立进程):纯协议技能结构测试统一落 tests/module/(与既有 test_skill_scope / test_skill_usage / test_skill_utils 同规范):test_auto_qa_skill.py、test_repair_sweep_skill.py、test_gh_pipeline_skill.py。
- 教训回执:test_auto_qa_skill.py 初版误放 tests/unit/,全量 split runner 的 Group A 在收集期 ImportError(skills.loader 命中 stub,unknown location);迁移到 tests/module 后消失。规则沉淀:结构断言型技能测试一律 tests/module。

### 5.2 终验清单(2026-09-01 全部通过)

1. uv run python scripts/run_tests_split.py,FINAL VERDICT: PASS;Group A 1202 passed + 2 skipped(18.33s),Group B 305 passed + 3 deselected(297.46s)。
2. skills/skills_snapshot.json 经官方机制(skills/skills_snapshot.py 的 build_skills_snapshot)再生,收录 taskflow / auto-qa / repair-sweep / gh-pipeline 四条记录(diff +25/-4)。
3. 附录 B 冒烟(结果见回执)。
4. llm_e2e 默认 deselected;--with-llm-e2e 专用作业另行执行,不与常规套件并行。

## 附录 A:代码事实坐标(实施依据,行号为实施时版本)

- skills/loader.py:parse_frontmatter :9-15;scope 语义 :52;可见性契约 :63-79;scan_skills 快照优先 :82-89;glob **/SKILL.md :96;SKILLS_DIR = ROOT_DIR/skills(config/path.py:25)。
- agent/tools/__init__.py:tool_flatten :23-34;_MAIN_TOOLS_BUILDERS :37-51(原 13 个 builder 加新增 1 个);build_main_tools :54-56。
- agent/tools/subagent/types/swarm.py:max_children_per_group=5 :27;max_concurrent=3 :29;swarm/collector.py RESERVED 队列 :86-92。
- spawn/core.py 派发入口 :173、tool policy :316-320 与 :725;announce/core.py 回流管线 :27-141、:84-100、:131-138;delivery.py :252-331 与 :306-313;registry/state.py :40-60;config.py run_timeout_seconds=300 :13。
- control/kill.py :28-78(cascade :81-133);control/steer.py :44-46(swarm run 拒绝 steer)。
- agent/tools/taskflow/registry/store_sqlite.py:busy_timeout :44-45;WAL 容忍切换 :165-192;ensure_db 跨循环初始化 :203-234;同步建表 :237-256;FlowConflictError :108-123;update_flow 乐观锁 :300-366。
- agent/tools/taskflow/tools/__init__.py:build_taskflow_tools :32-44(8 工具,scope=main_only);taskflow_run_task 可注入派发缝 :50。
## 附录 B:冒烟清单与实施回执

### B.1 冒烟清单(2026-09-01 执行,SMOKE PASS)

1. taskflow 导入冒烟:uv run 环境导入 build_taskflow_tools() 恰得 8 工具,名字逐一匹配,metadata scope=main_only:PASS。
2. loader 真扫冒烟:scan_skills(use_cache=False) 发现 taskflow / auto-qa / repair-sweep / gh-pipeline 四技能且 scope=main_only:PASS。
3. worktree 生命周期冒烟:git worktree add(-b smoke 分支)-> remove -> prune -> list 零残留:PASS。
4. gh 探针(可选项):本机未装 gh CLI,按协议跳过,不阻断终验。

### B.2 实施回执(偏差与声明)

- TDD 纪律:四项均先 RED 后 GREEN,RED 与 GREEN 的 pytest 摘要各留存一次(如 repair-sweep RED 17 failed -> GREEN 17 passed;gh-pipeline RED 1 failed + 21 errors -> GREEN 22 passed;taskflow GREEN 25 passed;auto-qa GREEN 30 passed)。
- 委派通道异常声明:实施期间委派通道出现三类异常,均已记录:(a) 一次 Insufficient Balance;(b) 两次子代理声称完成但零落盘(FILE CHANGES SUMMARY 为 No file changes detected,经实证核验不采信,后续以 task_id resume 补齐);(c) 一次派发 prompt 截损(与 Write 工具 SchemaError 同源)。受影响且由主会话直接实施的项:T4 测试文件 test_gh_pipeline_skill.py 与本计划文档重建(内容仅在主会话上下文);其余全部经 task() 委派并由主会话逐文件实证核验。
- 外部干扰记录:终验期间 client/repros/*.spec.ts(6 个)与 skills/skills_snapshot.json 被外部修改;前者未纳入本计划范围,原样保留;后者已按官方机制再生覆盖。原计划文档 ORCHESTRATION_PORT_PLAN.md 被外部删除,以本重建版回填。
- 未决事项:sessions_spawn 的 tool_allow/tool_deny 扩参(SUBAGENT_PORT_PLAN 2.2)仍为 auto-qa 只读评审 agent 的前置依赖;降级路径(主会话代查)已落地并在 SKILL.md 声明,扩参落地后切换为派发只读评审 agent。
- 验证基线:scripts/run_tests_split.py FINAL VERDICT PASS(Group A 1202 passed + 2 skipped,18.33s;Group B 305 passed + 3 deselected,297.46s),详见 5.2。

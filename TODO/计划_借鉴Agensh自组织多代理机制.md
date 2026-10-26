# 计划:借鉴 Agensh 自组织多代理机制(实现计划)

> 论文:Agensh: Scaling Organizational Intelligence to 1,024 Agents(Microsoft Research,
> arXiv:2609.26781,2026-09)。代码开源(github.com/microsoft/Agensh)。
> 配套分析见《[分析_子代理同文件写与符号破坏防护.md](分析_子代理同文件写与符号破坏防护.md)》——
> 那份文档回答"现有防护够不够、哪里会死循环",本计划回答"借论文哪些机制、怎么落"。

## 一、论文机制拆解

Agensh 的主张:**代理数量是新的扩展维度**。去掉中央编排者,让 N 个对等 worker 各自跑同一个
"协作循环",靠轻量基础设施共享状态。1→128 代理把 ProgramBench 五个最难任务的平均通过率从
19.31% 提到 28.78%(+49% 相对);pandoc 上 1→1024 代理从 33.89% 到 55.06%,且更大组织更早达到
同一水平(延迟维度)。

### 1. 五步协作循环(每个 worker 相同的 prompt,只有 worker id 不同)

1. **Gather context** — 读共享目标、同伴进度、消息、累积发现。
2. **Claim sub-task** — 把自己要做的范围作为 `CLAIM` 条目追加到共享上下文;两个 worker 的
   claim 重叠时**鼓励用直接消息自行解决**。
3. **Take action** — 本地干活,随时把可复用发现发布到共享上下文。
4. **Verify results** — 对照子任务的验收标准,不过就改,直到过。
5. **Merge progress** — 合入共享工作区;**合并被冲突挡住时,吸收最新同伴进度、解决冲突、
   再合**,并发布 `PATCH_SUMMARY(files= | idea= | evidence=)`。

循环写进 prompt 而不是硬编码进运行时——基础设施只做投递和存储,协议靠模型遵守。

### 2. 三件套基础设施

| 组件 | 实现承载 | 关键机制 |
| --- | --- | --- |
| 共享工作区 | Git(Gitea) | 私有 checkout+分支→合 main;Git 记录 provenance、检测**文本**冲突;issue/PR 承载全部工作流 |
| 消息接口 | Mattermost | 团队频道(低优先级,回合开始投递)+ 直接消息(高优先级,**在当前回合的下一个工具调用返回时**注入);历史保留、异步投递 |
| 共享上下文板 | DeLM 式 append-only 库 | 五类**短条目**:`OBSERVED`/`FACT`/`FAIL`/`CLAIM`/`PATCH_SUMMARY`(100/300 字符上限,`board_unfold` 看长版);近期内存渲染进 prompt,`board_grep`(AND/OR)查全量历史;新条目对其他 worker 是高优先级更新,**随下一个基础设施工具调用返回 mid-turn 注入** |

### 3. 事件驱动运行时(防丢、防重、防卡)

- 每个 worker 配一个 router:Gitea SSE / Mattermost 长连接,断线重连补拉;
- 事件带**唯一 id 去重**,写进 worker 的**持久队列**;派发器把队列事件排序合并进下一个
  prompt,回合结束才标记已发送;回合期间到达的事件排队等下一回合;
- 投递失败**退避重试**;router 重启后未完成事件回队列、worker 会话尽量恢复;
- **空闲探测器**(10 分钟)和"本回合没有发布任何共享上下文条目"提醒,逼 worker 持续产出。

### 4. 规模化治理

- **错峰启动**(前 1 小时每 30s 一个,之后每 3s 一个)减少 claim 撞车;
- **截止纪律**:T−45min"停止派新功能,确保根构建可用、合完 open PR";T−5min"能合的合掉,
  确认 `compile.sh && ./executable` 可用并公布最终状态";
- 涌现行为随规模升级:8 个=谈定接口各自实现/自行化解 claim 重叠;32 个=多 worker 共同评审
  (发现反例撤回批准→修复→再评审→同伴合并);128 个=出现常驻评审员/集成者与**标准化合并
  协议**(作者自测后发 commit hash→同伴验证并合并,失败后升级为"同伴全权 update-test-check-merge");
  1024 个=同角色多人冗余(联系多个候选集成者,取第一个响应的,其余取消)。

### 5. 论文里对 sherry 最相关的三个细节

- **"合并被拒→吸收最新 main→重验→再合"** 是 worker 协议的一等公民(不是错误路径);
- **`FAIL` 条目被论文称为最高价值条目**(阻止同伴再花预算);
- 接口一致性靠**工人间事先公告接口约定**(8 代理阶段),而不是靠工具强制。

## 二、与 sherry 现有概念的映射

| Agensh | sherry 现状 | 差距 |
| --- | --- | --- |
| 协作循环 | PlanContinuationEnforcer + announce 管线的 `_try_auto_resume_taskflow`(delivery.py:385,子代理完成→自动 resume 步骤→自动派发下一波) | sherry 是"主代理监督异常、流程自动推进",接近;无自认领 |
| Git 共享工作区 | `sessions_spawn(isolation=True)` worktree + CAS 合并(merge.py) | **默认关**;合并是机械 CAS,无"吸收后重验再合"的协议;无 PR 评审位 |
| 消息接口 | announce 完成消息(到父)、`sessions_send`(兄弟会话, NESTED lane) | 无优先级分层、无 mid-turn 注入(合并公告只在完成时到达) |
| 共享上下文板 | 证据账本(SQLite,跨会话可写但**只有派生读取**)、MesMemory(主代理侧) | **无跨子代理可查询的发现面**;无 CLAIM/FAIL/PATCH_SUMMARY 类型;无 board_grep |
| 事件运行时 | announce idempotency / delivery_state / 退避 | 已具备同构件(事件 id 幂等、状态机、重试) |
| 截止纪律 | run_timeout_seconds、flow deadline、continuation 15+latch | 无 T−45 式"收口提醒" |

## 三、实现计划

优先级依据见分析文档的结论:**同文件写(文件工具路径)已有防护;符号破坏(参数/返回值/变量名、
import 面)是真空;自动链路无死循环但有两个一次性写盲区。** P0 全部对着符号破坏,P1 引入
Agensh 的前置协调,P2 观望。

**一条来自图 5/图 6 的规模前提(视觉核对补出,正文只给均值)**:论文的分任务曲线**不是单调
的**——gromacs 在 8 与 32 代理时都比单代理更差(19.49 → 19.37 → 19.17),ctags 在 8 代理时
从 25.82 掉到 **18.77**(跑输单代理 7 分),图 6 的时间曲线上 ctags 的 8 代理线全程垫底。
也就是说**"不大不小"的规模最难看**,协调开销可以压过并行收益。sherry 的子代理典型并发
2~10 个正落在这个区间边缘,由此定两条纪律:

- **P0 三项不受影响**(它们是检测与验证,不需要每个工人额外交互一块共享黑板);
- **P1 的参与式协调(CLAIM 板、发现板、mid-turn 通知)一律 opt-in / 按并发子代理数门控 /
  默认 advisory**,并各带"关掉之后行为与今天完全一致"的开关。默认开启强制协调是**被论文
  数据反对**的做法,不是"更健壮"。

### P0-1 symbol_diff v2:签名面 + 模块级变量(关掉最大盲区)

- **现状**:`symbol_diff.py:137` 符号身份 = `(kind, name, parent)`,kind 只有
  function/method/class——**同名函数改参数/改返回值、模块级变量改名,合并公告完全看不见**。
- **改动**:`extract.py` 的 SymbolDef 增加 `signature`(参数名序列+是否仅关键字、参数个数、
  返回注记有无,不比较类型细节,只比"形状字符串");diff 时同名同 parent 但 signature 变化
  → 新增 `changed: list[SignatureChange]`;tree-sitter 捕获模块级赋值(kind=`variable`,
  仅顶层,函数体内的局部变量不进面)。`InterfaceDelta` 增字段;`_interface_lines`
  渲染 `changed: foo(a, b) → foo(a, b, *, timeout)`。
- **边界**:失败一律 fail-open(现状已是);大小上限沿用 `interface_diff_max_file_bytes`。
- **测试**:`tests/agent/tools/code_intel/` 增签名变化/模块变量改名用例;merge 公告渲染快照。

### P0-2 反向 import 索引:公告点名受影响文件(把 advisory 变得可执行)

- **现状**:合并公告只说"renamed foo→bar",**没有任何机制指出谁在 import foo**;
  跨会话失效广播按路径交集匹配(`evidence_ledger.py:457`),纯 import 用户(没改过、
  没验证过该文件)收不到 stale 行。
- **改动**:sherry 已有 code-intel 索引(`.sherry/codeintel/index.db`)。在
  `workspace_merge.py` 的公告阶段:对 `interface_changes` 里的每个 removed/renamed/
  changed 符号,用索引(缺失时现场 ast 扫描 `.py`/`.ts`,cap 文件数)反查引用文件,
  在 Interface changes 区追加 `affected importers: path3.py, path4.ts`;同时把这些
  引用文件作为路径调用 `mark_evidence_stale_for_paths`(writer=父会话)——**让"纯
  import 用户"的证据也失效**,使 finish 回放门/Gate C 能兜住。
- **边界**:反查只读、有 cap、fail-open;不阻断合并(维持 CAS 只管文本冲突的定位)。
- **测试**:双 worktree 集成用例——A 改名、B 引用,B 的 evidence 变 stale,finish 回放
  产生 broken。

### P0-3 合并即定向重验(论文"吸收→重验→再合"的落地)

- **现状**:重验只在 `taskflow_finish` 的回放门(regression_gate.py,一次性、cap 10、
  白名单),合并当下不跑任何东西。
- **改动**:隔离合并产生了 interface_changes 且反查到受影响 importers 时,立即对这些
  importer 关联的已记录 passed 命令跑一轮**定向**回放(复用 `_safe_argv`/`_run`,
  独立小 cap=5、开关默认关),结果 broken/unmark 直接进账本。即"接口变化合并 ⇒ 马上
  知道有没有打断同伴",不等 flow finish。
- **防循环红线**:重验只消费账本行、只追加事件,**事件追加绝不触发新的重验或新的合并**
  (维持"账本读取/写入不触发执行"的现有不变量);每个 run 的合并只发生一次(manifest
  消费,merge.py:212)。
- **测试**:合并→broken 出现→auto_resume 把失败带回父代理。

### P1-1 CLAIM 认领板(写前声明,防患于写前)

- 论文的 CLAIM:动手前公布范围,撞车用 DM 解决。sherry 对应物:每项目一张 append-only
  claims 表(复用 `BaseSQLiteRepository` 三件套套路),新工具 `claim_scope(paths[], note)`
  + 完成时自动释放;`write_file`/`patch_file` 在目标路径与**其他活跃会话**的 claim 重叠时,
  结果里附一行提醒(先 advisory,可配置升级为拒绝)。
- 与现有件的关系:这是锁之上的**意图层**(锁管字节,CAS 管版本,claim 管语义);对齐
  WorkspaceNotice 的"通知注入人类消息"手法,不动中间件链。
- **防循环**:claim 检查是纯读,不自动发消息、不自动 spawn;提醒每回合至多一次。
- **测试**:双会话并发 claim 同文件;claim 过期(TTL)自动失效。

### P1-2 PATCH_SUMMARY 类型化公告 + 发现板查询面(FAIL/FACT 跨子代理可读)

- 把合并公告从自由文本升级为结构化条目(files/idea/evidence/interface),连同子代理的
  `FAIL`(试过不行)与 `FACT`(已确认)写进一张 per-project 发现表;新只读工具
  `peers_findings(query)`(= board_grep 的 AND/OR 语义)给**子代理**用——现在兄弟
  之间只有父代理转述,论文的要点是同伴直接复用发现,省的是每个子代理的重复试错预算。
- **防循环**:只读查询;写入只发生在 announce/merge 既有钩子内。

### P1-3 中途投递:符号广播命中时通知正在工作的兄弟(论文 DM 时机)

- 论文的高优先级条目"随下一个工具调用返回 mid-turn 注入"。sherry 复用
  `sessions_send`(NESTED lane)作为传输:当 P0-2 的反向索引命中**另一个正在 RUNNING 的
  子代理**的会话时,自动投一条短消息("utils.py 的 foo 已被 run_A 改名为 bar,引用处需更新")。
- **防循环红线(最重要的一条)**:每会话每回合至多注入 1 条;只由"文件写/合并"触发,
  **绝不由消息/账本事件触发**(否则 A 通知 B→B 修文件→B 的写又通知 A→死循环);
  注入失败静默丢弃(fail-open)。
- **注入落点按图 3 的读写箭头定**:论文只把"读"放在 Gather context / Take action /
  Verify results 三处、"写"放在 Claim / Take action / Merge 三处。sherry 对应关系——
  注入挂在下一次模型调用前或下一次工具结果后(即"读"点);幂等与限流覆盖写工具
  (write_file/patch_file/合并)那侧。
- **测试**:模拟 A 改名时 B 在跑,B 的下一回合看到通知;连发两条只透传一条。

### P1-4 terminal / python_repl 写可见性(关掉 shell 写盲区)

- **现状**:证据记账只挂在 `write_file`/`patch_file` 装饰器上(evidence_recorder.py:114),
  shell 写文件**既无许可、也无 file_edit 记录**——它的证据永远拿不到 modified_files,
  别人的写也无法失效它;回放门的 formatter 命令(`black`/`prettier` 不带 `--check`)是
  绕过许可的一次性直写。
- **改动**:terminal/python_repl 执行前后对项目根做有界 mtime 扫描(cap 路径数),差异
  即记 `record_file_edit`(+对应 stale 广播);回放白名单收紧为"只读验证器"或在回放前对
  可写 formatter 强制 `--check`(black/ruff/prettier 的 check 形态),拒绝带写形态。
- **测试**:terminal 里 `sed -i` 后,账本出现该路径;`black foo.py` 作为证据命令不被回放。

### P2(观望,不排期)

- **错峰启动**:TaskFlow auto-dispatch 波内已有并发上限,暂无 1024 规模需求;
- **T−45/T−5 收口提醒**:flow deadline 已有,可在 reminder catalogue 加"临近截止"条目;
- **标准化合并协议/常驻集成者角色**:等 P1-1/P1-2 跑起来看涌现,不预制。

## 四、防循环设计红线(所有新增件必须遵守)

1. **事件幂等**:一切广播/注入带唯一事件 id,重复投递被 announce idempotency 同款机制吞掉;
2. **账本只标记不执行**:唯一允许的自动执行仍是"一次性、白名单、有 cap"的回放(现状
   regression_gate,新增 P0-3 同规格);
3. **注入上限**:mid-turn 通知每会话每回合 ≤1 条,且只由文件写触发;
4. **锁不嵌套**:任何新逻辑不得在持有一个路径锁时再取另一个(维持无死锁论证);
5. **资源上限兜底**:行为性循环(父代理反复重派)由 max_total_spawns_per_session=50、
   step 天花板、run_timeout_seconds 兜底——这是现有最后防线,新件不得绕过。

## 五、架构错配与可迁移性(它是去中心化的,sherry 是中心化的,为什么仍能借)

"中心化"要拆两层,可借鉴件全在另一层:

- **任务分配层**(谁派活):Agensh 工人自认领,sherry 父代理/TaskFlow 派发。**这一层不借**——
  小规模 + 判定门 + HITL + 角色遏制,拆编排者就是拆守门人。
- **运行时资源仲裁层**(并发工人怎么不互踩):**sherry 在这一层已经去中心化**——默认
  `isolation=False` 共享同一棵工作树;spawn 到 announce 之间父代理不在环里,不能仲裁任何
  一次写;现有防护(路径锁/flock/读许可 CAS/证据广播)本身就是无仲裁者的对等机制。同文件
  盲区与符号破坏全是这一层的病,论文的 CLAIM/类型化公告/mid-turn 注入/"吸收→重验→再合"
  也全是这一层的纪律——原样移植进有编排者的系统,不碰编排者。

中心化反而是受益方(借的是"交通规则",不是"取消交警"):

1. **知识转述瓶颈**:兄弟之间只有父代理转述;spawn≤50 时父上下文就是全系统缩放上限,
   发现板让兄弟直接查,枢纽瓶颈消失而派发权不动。
2. **探测瓶颈**:合并公告是否被处理取决于父代理反应;mid-turn 注入让正在 import 该符号的
   兄弟直接收到——探测从枢纽移到边缘,跟组织形态无关。
3. **活性耦合**:父代理卡住则发现躺在账本里;可查询的板把"知识可用"与"枢纽活着"解耦。

两个佐证:(a) PlanContinuationEnforcer 的 auto-turn 触发本来就会 defer 过 busy 信号等
回合边界——论文"频道消息回合头投递"的对应物,缺的只是 mid-turn 这半边,移植是补全既有
模式;(b) 论文自己在 32→128→1024 阶段自发电把集成职能重新中心化(常驻集成者/标准化合并
协议/多候选集成者取先响应),去中心化系统规模化时自发收敛出枢纽——sherry 的父代理就是
预任命的那个。两边各强一手:论文强在协调纪律,sherry 强在验证机器(判定门/证据账本/回放门
/HITL,论文只有 worker 自验 + 偶尔同伴评审)。借鉴是互补单向:借协调纪律喂强中心。

**图 2 是这段论证的直接证据**:左侧"编排者组织工人"与右侧 Agensh 的**拓扑都是星形**,
Agensh 只是把中心节点从"一个有智能的编排者"换成了"共享工作区+消息接口+共享上下文这套
被动设施"。所以可迁移的是**把中心基础设施化**(兄弟之间共享发现这类活下沉到哑设施),
而不是取消中心——sherry 保留父代理做派发/判定,与图 2 右侧的形状并不冲突。

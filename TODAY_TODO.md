# 开发规范与配置变更记录（索引）

> 原始内容已拆分为 4 个子文件，每个不超过 1000 行。下表链接指向各子文件内的章节锚点。

## 文件总览

| 文件                                                                   | 节范围    | 行数 | 主题                                                                   |
| ---------------------------------------------------------------------- | --------- | ---- | ---------------------------------------------------------------------- |
| [part1-precommit-quality.md](./part1-precommit-quality.md)             | 一~五     | 865  | Pre-commit 边界守卫、Ruff S110、Nuxt 指令、ESLint+Prettier、Commitlint |
| [part2-ci-test-structure.md](./part2-ci-test-structure.md)             | 六~十     | 763  | GitHub Actions CI、测试目录重构、命名规范、DPDM 循环依赖               |
| [part3-anticorruption-build.md](./part3-anticorruption-build.md)       | 十一~十四 | 843  | import-linter 层级契约、注释腐化防治、build.sh、Dependabot             |
| [part4-prepush-logging-sandbox.md](./part4-prepush-logging-sandbox.md) | 十五~十八 | 690  | Pre-push 门禁、Ruff T20 禁 print、ESLint no-console、沙箱防逃逸        |
| [part5-token-comment-rot.md](./part5-token-comment-rot.md)             | 十九~二十 | —    | 三级降级 Token 估算 + 注释腐化清理记录                                 |

## 全部章节导航

| #    | 分类     | 标题                                                        | 所在文件 |
| ---- | -------- | ----------------------------------------------------------- | -------- |
| 一   | 项目结构 | Pre-commit 边界守卫：前端/Rust 文件限制在 client/ 目录      | part1    |
| 二   | 后端质量 | Ruff S110 规则：禁止 try-except-pass 静默吞异常             | part1    |
| 三   | 前端功能 | Nuxt4 自定义指令全局注册：v-debounce / v-safe-html 免引入   | part1    |
| 四   | 前端质量 | ESLint + Prettier 配置与 Pre-commit fix-then-check 流程     | part1    |
| 五   | 提交规范 | Commitlint：提交信息强制 Angular 风格                       | part1    |
| 六   | CI       | GitHub Actions：前后端测试并行运行                          | part2    |
| 七   | 测试结构 | Python 测试目录重构为集中镜像式                             | part2    |
| 八   | 测试结构 | 前端测试改为就近式                                          | part2    |
| 九   | 代码规范 | 前后端命名规范：通过文件名/函数名格式防止代码外溢           | part2    |
| 十   | 代码质量 | DPDM：前端循环依赖检测                                      | part2    |
| 十一 | 代码质量 | import-linter：server 层级依赖契约                          | part3    |
| 十二 | 代码质量 | 注释腐化防治：死代码检测 + JSDoc 校验 + Python docstring    | part3    |
| 十三 | 构建部署 | 一键打包脚本 build.sh                                       | part3    |
| 十四 | 依赖管理 | Dependabot：前后端依赖自动更新                              | part3    |
| 十五 | 代码质量 | Pre-push 门禁：重操作从 pre-commit 分离                     | part4    |
| 十六 | 代码质量 | Ruff T20：禁用 print，强制使用 loguru                       | part4    |
| 十七 | 代码质量 | ESLint no-console：禁用 console，强制使用日志工具           | part4    |
| 十八 | 安全     | 沙箱防逃逸与子代理权限递减                                  | part4    |
| 十九 | 代码质量 | 三级降级 Token 估算：usage_metadata → tiktoken → CJK 启发式 | part5    |
| 二十 | 代码质量 | 注释腐化清理记录（Comment Rot Remediation）                 | part5    |

- sherry.jsonc中LangSmith tracing三个变量外面套一层LANGSMITH。然后调试现有代码到langsmith生效，并解决移动带来的文档、注释漂移
- 在sherry.jsonc创建curator子对象，将curator.yaml的所有变量放进该对象里，并调试现有curator逻辑代码使用curator子对象的变量，不再用curator.yaml。最后删除curator.yaml，并解决移动带来的文档、注释漂移
- docs/subagent下的subagentREADME全移动到agent/tools/subagent目录下，并解决移动带来的文档、注释漂移
- 删除smart_tool_node.py以及关联代码，并解决删除带来的文档、注释漂移
- 查看skills下auto-qa,gh-pipeline,repair-sweep技能 是否在项目中有用到，没用到就删除，并删除相关的测试代码。然后再把taskflow移动到skills/builtin/core下，并解决移动带来的文档、注释漂移
- eslint,prettier等前后端代码检查工具，不符合规范的警告信息 从中文换为英文
- Nuxt4架构放到composables的不用再显式导入,用eslint规则约束并严格冒烟测试、功能測試、回归测试
- 查看 token三层降级计算方法是否覆盖了max_tokens_boost中间件，没有补上，然后按现有中间件功能 更新agent/middlewares下README
- HITL弹框多加YOLO按钮（同意所有操作）以及打通前后端相关逻辑，补上四国语言国际化。
- 检查四国语国际化哪些可以下沉到组件的<i18n>标签里并执行下沉，并严格冒烟测试、功能測試、回归测试
- 删除failover-model-fallback.md,failover-model-breaker.md,THINKING_TOKEN_BUDGET_PLAN.md,CONTEXT_LIMIT_GUARD_WRAPPER.md,处理删除后文档,注释漂移
- 检查TODO/AUDIT_REPORT.md中## 1. `resolve_path` 无 ROOT_DIR 越界防护 — 任意文件读/写 和 ## 5. `resolve_path` 下游：任意文件读/写（见严重 #1 的利用链） 是否已经修好，修好就把这两项去掉.要求不能留下任何代码注释
- 解决TODO/AUDIT_REPORT_ROUND2.md中 3、4、5、13、14、15、16、17、19、20、21、27、32、34、36、38、45（前后端都要限制）、46（前后端都要限制）、54、55. 要求不能留下任何代码注释
- 解决完AUDIT_REPORT_ROUND2.md的问题后将AUDIT_REPORT_ROUND2.md的问题合入AUDIT_REPORT.md，并重新编排，去掉历史信息。然后再删除AUDIT_REPORT_ROUND2.md，并删除相关文档漂移
- TODO/DESIGN_PATTERN_REFACTORING_BACKEND.md 过时,先解决1.1 God 类/函数,1.2 长方法与深嵌套,2.1 God 类/函数,3.1 God 类/函数,3.2.6，需要严格冒烟测试、功能測試、回归测试，是否影响原有功能。然后删除已修改的项，并解决删除后的文档漂移。最后再更新整个文档.
- 解决DESIGN_PATTERN_REFACTORING_FRONTEND.md中的 4.1 代码重复、4.2 God 类/函数、4.3 长方法与深嵌套、4.5 缺失抽象 / 紧耦合，需要严格冒烟测试、功能測試、回归测试，是否影响原有功能。然后删除已修改的项，并解决删除后的文档漂移。最后再更新整个文档.
- 执行TODO/PATH_RESOLVE_REFACTOR.md并严格冒烟测试、功能測試、回归测试
- 创建 .github/workflows/dependabot-auto-merge.yml，分级自动合并策略：dev依赖(patch+minor)自动合并、生产依赖(patch)自动合并、github-actions(patch+minor)自动合并，major均需人工审查
    ```yaml
    name: Dependabot Auto-Merge
    
    on: pull_request
    
    permissions:
      contents: write
      pull-requests: write
    
    jobs:
      dependabot:
        runs-on: ubuntu-latest
        if: github.actor == 'dependabot[bot]'
        steps:
          - name: Dependabot metadata
            id: metadata
            uses: dependabot/fetch-metadata@v2
            with:
              github-token: "${{ secrets.GITHUB_TOKEN }}"
    
          - name: Auto-merge dev dependency groups (patch + minor)
            if: |
              contains(fromJson(steps.metadata.outputs.updated-dependencies-json), 'dependency-type') &&
              steps.metadata.outputs.dependency-type == 'development'
            run: gh pr merge --auto --merge "$PR_URL"
            env:
              PR_URL: ${{ github.event.pull_request.html_url }}
              GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    
          - name: Auto-merge production deps (patch only)
            if: |
              steps.metadata.outputs.dependency-type != 'development' &&
              steps.metadata.outputs.update-type == 'version-update:semver-patch'
            run: gh pr merge --auto --merge "$PR_URL"
            env:
              PR_URL: ${{ github.event.pull_request.html_url }}
              GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    
          - name: Auto-merge GitHub Actions updates (patch + minor)
            if: |
              steps.metadata.outputs.package-ecosystem == 'github-actions' &&
              steps.metadata.outputs.update-type != 'version-update:semver-major'
            run: gh pr merge --auto --merge "$PR_URL"
            env:
              PR_URL: ${{ github.event.pull_request.html_url }}
              GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    ```
- 提交暂存区所有代码并推送，并确保走通github的CI流水线
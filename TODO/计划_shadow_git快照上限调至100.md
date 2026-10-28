# 计划:shadow git 快照上限调至 100

## 现状

`config/features/agent_side/shadow_git.py:21` 的 `max_snapshots = 20`。shadow store 是
per-project-root 的,同一 project root 上所有会话(含子代理)的快照共享这一个 store 和这
20 个名额。多子代理并发时(5 个子代理各拍几轮),20 个名额 1-2 轮就满,`restore_checkpoint`
的回滚窗口缩到最近一两步。

## 改动

```python
# config/features/agent_side/shadow_git.py:21
"max_snapshots": 100,  # was 20
```

一行配置,无逻辑改动。`_prune_snapshots`(shadow_git.py:235-251)的裁剪逻辑不变——
`git rev-list --count` 数总数,超过上限用 `update-ref` 截断。

## 为什么 100 够

- `ensure_checkpoint` 每会话每回合 dedup(每个 session 每个 turn 最多拍一次);
- 100 个快照 ≈ 单会话 100 个 turn,或 10 个子代理并发各 10 turn;
- 回滚场景只需要回到最近几步,100 远超实际回滚需求。

## 存储影响

git 对象存储是去重 + delta 压缩的——相邻 commit 之间通常只有少数文件变了,增量对象很小。
100 个快照的存储成本相比 20 个增长有限(bare store 里只有 objects + refs,无工作树)。

## 不受影响

- P0-4 的 shadow-git diff(`run_shadow(["diff","--name-only",SHADOW_REF])`)只看工作树 vs
  最新 HEAD,不依赖历史深度——不受 max_snapshots 影响。
- `git_timeout_s=30.0` 不变——裁剪操作(`rev-list --count` + `update-ref`)在 100 条
  量级下远低于 30s。

## 测试

- `tests/agent/tools/pub_base/test_shadow_git.py` 增一条:拍 101 个快照后 `rev-list --count`
  返回 100(裁剪生效);拍 99 个返回 99(不裁剪)。

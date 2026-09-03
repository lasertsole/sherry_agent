"""Linux bwrap (bubblewrap) sandbox backend.

仅验证构造逻辑，未在 Linux 实机验证（Windows 开发机无 bwrap 二进制；
本模块的测试全部 mock subprocess，不会执行真实 bwrap）。

argv 顺序是承重结构：
- ``--ro-bind / /`` 先把整个根目录挂为只读；
- 随后仅把 ROOT_DIR / TEMP_DIR 以可写方式 ``--bind``（可写面 = 这两个目录）；
- ``--clearenv`` 必须出现在所有 ``--setenv`` 之前 —— 两者结合实现 env
  白名单语义（上游 env_scrub 已清洗，这里只注入白名单内的变量）；
- ``--`` 之后是被包装的原始命令。

参考: oh-my-openagent sandbox-bwrap-probe.ts / sandbox-platform.ts buildBwrapArgs
（探测语义原型：bwrap 存在但被 AppArmor 阻止时（Ubuntu 24.04+
kernel.apparmor_restrict_unprivileged_userns=1），存在性检查不够，必须冒烟）。
"""

from __future__ import annotations

import subprocess

from config.path import ROOT_DIR, TEMP_DIR

try:  # Task 2 产出优先；Task 2 未落地时回退到本地 ABC 形状（见 notepad problems.md）
    from agent.tools.pub_base.sandbox import SandboxBackend
except ImportError:  # pragma: no cover
    from abc import ABC, abstractmethod

    class SandboxBackend(ABC):
        """Local ABC-shaped fallback mirroring agent.tools.pub_base.sandbox contract."""

        @abstractmethod
        def probe(self) -> bool: ...

        @abstractmethod
        def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]: ...


#: probe 冒烟超时（秒）
_PROBE_TIMEOUT_SECONDS = 3

#: 冒烟命令：最小但仍触发 user-namespace/uid-map 建立的调用
_PROBE_ARGV = [
    "bwrap",
    "--ro-bind", "/", "/",
    "--proc", "/proc",
    "--dev", "/dev",
    "true",
]


class BwrapBackend(SandboxBackend):
    """Linux bubblewrap backend. bwrap 不接受 env 字典，env 经 --setenv 注入。"""

    _probe_cache: bool | None = None  # 类级缓存：进程生命周期内只探测一次

    def probe(self) -> bool:
        """冒烟测试 bwrap 可用性；异常/非零返回码/超时一律 False，结果类级缓存。"""
        if BwrapBackend._probe_cache is not None:
            return BwrapBackend._probe_cache
        try:
            result = subprocess.run(
                _PROBE_ARGV,
                capture_output=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
            ok = result.returncode == 0
        except Exception:  # FileNotFoundError / TimeoutExpired / OSError 等
            ok = False
        BwrapBackend._probe_cache = ok
        return ok

    def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        """把 cmd 包装进 bwrap argv；env 白名单经 --setenv 注入。"""
        argv: list[str] = [
            "bwrap",
            "--ro-bind", "/", "/",
        ]
        for path in _writable_paths():
            argv += ["--bind", path, path]
        argv += [
            "--tmpfs", "/tmp",
            "--dev", "/dev",
            "--proc", "/proc",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
        ]
        for key, value in env.items():
            argv += ["--setenv", key, str(value)]
        argv += ["--", *cmd]
        return argv, env


def _writable_paths() -> list[str]:
    """可写目录清单（str 化），路径相同（TEMP_DIR == ROOT_DIR 等场景）时去重。"""
    root, temp = str(ROOT_DIR), str(TEMP_DIR)
    return [root] if root == temp else [root, temp]

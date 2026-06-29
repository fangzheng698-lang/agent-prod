"""工具执行器 — 方案 A (Gateway 工具代理) 的运行层。

Gate0 通过后，由 ToolExecutor 在受限沙箱环境中执行工具。
沙箱策略是独立于 Gate0 的第二层防御——即使 Gate0 放行，
执行器也会做最终的安全检查。

沙箱策略:
  - read_file:  仅允许白名单路径
  - write_file: 仅允许白名单路径，块设备/特殊文件拒绝
  - terminal:   危险命令黑名单 (rm -rf, curl|sh, mkfs, dd 等)
  - execute_code: 允许（已通过 Python exec 隔离）
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
import time
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 沙箱策略（线程安全）──────────────────────────────────────────
import threading as _threading

# 读/写文件的允许路径前缀 (可被 config.yaml sandbox.path_whitelist 覆盖)
_PATH_WHITELIST: list[str] = [
    "/root/experiment/",
    "/root/project/",
    "/root/.hermes/",
    "/tmp/",
    "/var/tmp/",
    "/var/lib/quality_gates/",
]

# 始终禁止的路径（即使前缀匹配）
_PATH_BLACKLIST: list[str] = [
    "/etc/passwd", "/etc/shadow", "/etc/sudoers",
    "/root/.ssh/", "/root/.bashrc", "/root/.profile",
    "/proc/", "/sys/", "/dev/",
    "/etc/kubernetes/", "/etc/ssl/", "/var/run/secrets/",
]

_sandbox_lock = _threading.Lock()


def _get_whitelist() -> list[str]:
    with _sandbox_lock:
        return list(_PATH_WHITELIST)


def _get_blacklist() -> list[str]:
    with _sandbox_lock:
        return list(_PATH_BLACKLIST)


def load_sandbox_config(config: dict | None = None) -> None:
    """从 config.yaml 加载沙箱配置，覆盖默认白名单。

    config.yaml 示例:
        sandbox:
          path_whitelist:
            - /root/experiment/
            - /tmp/
            - /home/agent/
          path_blacklist:
            - /etc/ssl/private/
    """
    global _PATH_WHITELIST, _PATH_BLACKLIST
    if not config or "sandbox" not in config:
        return
    sandbox = config["sandbox"]
    with _sandbox_lock:
        if "path_whitelist" in sandbox:
            _PATH_WHITELIST[:] = sandbox["path_whitelist"]
            logger.info("Sandbox whitelist loaded (%d paths)", len(_PATH_WHITELIST))
        if "path_blacklist" in sandbox:
            _PATH_BLACKLIST[:] = sandbox["path_blacklist"]
            logger.info("Sandbox blacklist loaded (%d paths)", len(_PATH_BLACKLIST))

# 危险命令模式（终端）
DANGEROUS_COMMAND_PATTERNS: list[str] = [
    r"rm\s+-rf\s+/",           # 递归删除根
    r"mkfs\.",                  # 格式化
    r"dd\s+if=",                # 块设备操作
    r">\s*/dev/",               # 写入块设备
    r"chmod\s+777\s+/",         # 根目录权限放开
    r"chown\s+-R\s+\w+\s+/",    # 根目录改属主
    r"curl\s+.*\|\s*(ba)?sh",   # curl|sh
    r"wget\s+.*-O\s+-\s*\|\s*(ba)?sh",  # wget|sh
    r":\(\)\s*\{\s*:\|:&\s*\};:",   # fork bomb
    r"reboot|shutdown|halt|poweroff",  # 关机
    r"iptables|nft\s+",         # 防火墙
    r"systemctl\s+(stop|disable|mask)",  # 禁服务
    r"docker\s+(rm|stop|kill)\s+-f",    # 强杀容器
]


def is_path_allowed(filepath: str) -> bool:
    """检查文件路径是否在沙箱白名单内且不在黑名单中。"""
    abs_path = os.path.abspath(os.path.expanduser(filepath))

    # 黑名单检查
    for black in _get_blacklist():
        if abs_path.startswith(os.path.abspath(black)):
            return False

    # 白名单检查
    for white in _get_whitelist():
        white_abs = os.path.abspath(white)
        if abs_path.startswith(white_abs):
            return True

    return False


def is_command_safe(command: str) -> tuple[bool, str]:
    """检查终端命令是否安全。返回 (safe, reason)。"""
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"命令匹配危险模式: {pattern}"
    return True, ""


# ── 执行器 ────────────────────────────────────────────────────


class ToolExecutor:
    """在沙箱策略内执行工具调用。

    使用方式 (Gateway 端点):
        executor = ToolExecutor()
        result = executor.execute(tool_name, arguments)
        → {"success": True, "output": "...", "duration_ms": 12.3}

    如果工具不在实现列表中，返回 NOT_IMPLEMENTED。
    """

    def execute(self, tool_name: str, arguments: dict | None = None) -> dict[str, Any]:
        args = arguments or {}
        t0 = time.time()

        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return {
                "success": False,
                "error": f"工具 '{tool_name}' 未在代理执行器中实现",
                "status": "NOT_IMPLEMENTED",
                "duration_ms": (time.time() - t0) * 1000,
            }

        try:
            result = handler(args)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "status": "EXECUTION_ERROR",
                "duration_ms": (time.time() - t0) * 1000,
            }

        result["duration_ms"] = round((time.time() - t0) * 1000, 1)
        return result

    # ── 工具处理器 ─────────────────────────────────────────

    def _handle_read_file(self, args: dict) -> dict:
        path = args.get("path", "") or args.get("file_path", "")
        if not is_path_allowed(path):
            return {"success": False, "error": f"沙箱拒绝访问路径: {path}"}

        offset = args.get("offset", 0) or 0
        limit = args.get("limit", 500) or 500

        with open(path) as f:
            lines = f.readlines()
            total = len(lines)
            start = max(0, offset - 1 if offset > 0 else 0)
            end = min(total, start + limit)
            content = "".join(lines[start:end])

        return {
            "success": True,
            "output": content,
            "total_lines": total,
            "lines_returned": min(limit, total),
        }

    def _handle_write_file(self, args: dict) -> dict:
        path = args.get("path", "") or args.get("file_path", "")
        content = args.get("content", "")

        if not is_path_allowed(path):
            return {"success": False, "error": f"沙箱拒绝写入路径: {path}"}

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

        return {"success": True, "output": f"写入 {len(content)} 字节到 {path}"}

    def _handle_terminal(self, args: dict) -> dict:
        command = args.get("command", "")
        timeout = args.get("timeout", 60)
        workdir = args.get("workdir") or None

        safe, reason = is_command_safe(command)
        if not safe:
            return {"success": False, "error": f"沙箱拒绝危险命令: {reason}"}

        # 如果指定了 workdir，也做路径检查
        if workdir and not is_path_allowed(workdir):
            return {"success": False, "error": f"沙箱拒绝工作目录: {workdir}"}

        try:
            cmd_list = shlex.split(command)
            proc = subprocess.run(
                cmd_list,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=workdir,
                env={**os.environ, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"命令超时 ({timeout}s)"}

        return {
            "success": proc.returncode == 0,
            "output": proc.stdout[:50000],
            "stderr": proc.stderr[:5000],
            "exit_code": proc.returncode,
        }

    def _handle_execute_code(self, args: dict) -> dict:
        code = args.get("code", "")
        if len(code) > 100_000:
            return {"success": False, "error": "代码超过 100KB 限制"}
        # 在受限 namespace 中执行，禁止危险操作
        forbidden_imports = [
            r"\bimport\s+os\b", r"\bimport\s+subprocess\b",
            r"\bimport\s+shutil\b", r"\bimport\s+socket\b",
            r"\bimport\s+ctypes\b", r"\bimport\s+pty\b",
            r"\bimport\s+signal\b", r"\bimport\s+sys\b",
            r"\bbuiltins\b",
            r"__import__\s*\(", r"exec\s*\(", r"eval\s*\(",
            r"compile\s*\(", r"open\s*\(", r"file\s*\(",
        ]
        for fi in forbidden_imports:
            if re.search(fi, code):
                return {"success": False, "error": f"沙箱拒绝: 禁止使用 '{fi.strip()}'"}
        try:
            safe_builtins = {
                "abs": abs, "all": all, "any": any, "ascii": ascii,
                "bin": bin, "bool": bool, "bytes": bytes, "chr": chr,
                "complex": complex, "dict": dict, "divmod": divmod,
                "enumerate": enumerate, "filter": filter, "float": float,
                "format": format, "frozenset": frozenset, "hex": hex,
                "id": id, "int": int, "isinstance": isinstance,
                "issubclass": issubclass, "iter": iter, "len": len,
                "list": list, "map": map, "max": max, "min": min,
                "next": next, "object": object, "oct": oct, "ord": ord,
                "pow": pow, "print": print, "range": range,
                "repr": repr, "reversed": reversed, "round": round,
                "set": set, "slice": slice, "sorted": sorted,
                "str": str, "sum": sum, "tuple": tuple, "type": type,
                "zip": zip, "True": True, "False": False, "None": None,
            }
            restricted_globals = {"__builtins__": safe_builtins}
            local_vars = {}
            exec(code, restricted_globals, local_vars)
            output = local_vars.get("result", local_vars.get("output", ""))
            return {"success": True, "output": str(output)[:50000]}
        except SyntaxError as e:
            return {"success": False, "error": f"语法错误: {e}"}
        except Exception as e:
            return {"success": False, "error": f"执行异常: {type(e).__name__}: {e}"}

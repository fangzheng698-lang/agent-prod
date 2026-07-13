# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Phase 8.2: Extended Tools — web_search, file_read, shell_exec

工具注册到 app/tools.py 的 ToolRegistry，供 agent 调用。

工具:
  - WebSearchTool: web_search(query) 返回模拟搜索结果
  - FileReadTool: file_read(path) 读取文件内容
  - ShellExecTool: shell_exec(cmd) 执行 shell 命令（白名单 + 超时保护）
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from typing import ClassVar

from agent_prod.agent.tools import Tool

# ═══════════════════════════════════════════════════════════════
# WebSearchTool — 模拟网页搜索
# ═══════════════════════════════════════════════════════════════

_MOCK_SEARCH_RESULTS: list[dict[str, str]] = [
    {
        "title": "FastAPI - The Fastest Python Web Framework",
        "url": "https://fastapi.tiangolo.com",
        "snippet": "FastAPI is a modern, fast (high-performance), web framework for building APIs with Python 3.7+ based on standard Python type hints.",
    },
    {
        "title": "Python Documentation",
        "url": "https://docs.python.org/3/",
        "snippet": "Official Python 3.x documentation, tutorials, and library reference.",
    },
    {
        "title": "Docker: Accelerated Container Application Development",
        "url": "https://www.docker.com",
        "snippet": "Docker helps developers build, share, run, and verify applications anywhere.",
    },
    {
        "title": "Prometheus - Monitoring System & Time Series Database",
        "url": "https://prometheus.io",
        "snippet": "Prometheus is an open-source systems monitoring and alerting toolkit.",
    },
    {
        "title": "PostgreSQL: The World's Most Advanced Open Source Database",
        "url": "https://www.postgresql.org",
        "snippet": "PostgreSQL is a powerful, open source object-relational database system.",
    },
]


class WebSearchTool(Tool):
    """模拟网页搜索工具。

    生产环境可替换为真实的 DuckDuckGo / SerpAPI / Brave Search API 调用。
    """

    name: ClassVar[str] = "web_search"
    description: ClassVar[str] = (
        "搜索网页内容。返回相关网页的标题、URL 和摘要。"
        "适用于获取最新信息、技术文档查询等场景。"
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或问题",
            },
        },
        "required": ["query"],
    }
    timeout: float = 15.0

    async def execute(self, query: str) -> str:
        """执行搜索并返回格式化结果。"""
        query_lower = query.lower()

        # 匹配相关结果
        scored: list[tuple[int, dict]] = []
        for r in _MOCK_SEARCH_RESULTS:
            score = 0
            for word in query_lower.split():
                if word in r["title"].lower():
                    score += 3
                if word in r["snippet"].lower():
                    score += 2
                if word in r["url"].lower():
                    score += 1
            if score > 0:
                scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            # 返回通用结果
            return (
                f"搜索结果 (query: '{query}'):\n\n"
                f"1. {_MOCK_SEARCH_RESULTS[0]['title']}\n"
                f"   {_MOCK_SEARCH_RESULTS[0]['url']}\n"
                f"   {_MOCK_SEARCH_RESULTS[0]['snippet']}\n\n"
                f"2. {_MOCK_SEARCH_RESULTS[2]['title']}\n"
                f"   {_MOCK_SEARCH_RESULTS[2]['url']}\n"
                f"   {_MOCK_SEARCH_RESULTS[2]['snippet']}\n\n"
                f"(共找到 {len(_MOCK_SEARCH_RESULTS)} 个结果)"
            )

        lines = [f"搜索结果 (query: '{query}'):\n"]
        for i, (_, r) in enumerate(scored[:5], 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   URL: {r['url']}")
            lines.append(f"   {r['snippet']}")
            lines.append("")
        lines.append(f"(共找到 {len(scored)} 个相关结果，显示前 {min(5, len(scored))} 个)")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# FileReadTool — 文件读取
# ═══════════════════════════════════════════════════════════════

MAX_FILE_SIZE = 100_000  # 100KB 上限


class FileReadTool(Tool):
    """安全文件读取工具。

    限制:
      - 最大文件大小 100KB
      - 仅读取文本文件
      - 路径需在工作区内（可由调用方控制）
    """

    name: ClassVar[str] = "file_read"
    description: ClassVar[str] = (
        "读取指定路径的文件内容。"
        "仅支持文本文件，最大 100KB。"
        "返回文件内容或错误信息。"
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件绝对路径",
            },
        },
        "required": ["path"],
    }
    timeout: float = 10.0

    async def execute(self, path: str) -> str:
        """读取文件内容。"""
        if not os.path.exists(path):
            return f"Error: File not found — '{path}' does not exist"

        if not os.path.isfile(path):
            return f"Error: Not a regular file — '{path}'"

        file_size = os.path.getsize(path)
        if file_size > MAX_FILE_SIZE:
            return (
                f"Error: File too large — {file_size:,} bytes "
                f"(max {MAX_FILE_SIZE:,} bytes)"
            )

        if file_size == 0:
            return f"(empty file: '{path}')"

        try:
            # Read in thread to avoid blocking event loop
            loop = asyncio.get_running_loop()
            content = await loop.run_in_executor(None, self._read_file, path)
            return content
        except UnicodeDecodeError:
            return f"Error: Cannot read '{path}' — not a valid UTF-8 text file"
        except PermissionError:
            return f"Error: Permission denied — cannot read '{path}'"
        except OSError as e:
            return f"Error: OS error reading '{path}' — {e}"

    @staticmethod
    def _read_file(path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()


# ═══════════════════════════════════════════════════════════════
# ShellExecTool — 安全命令执行
# ═══════════════════════════════════════════════════════════════

# 白名单命令（安全的基础命令）
_ALLOWED_COMMANDS: set[str] = {
    "echo", "cat", "head", "tail", "wc", "grep",
    "ls", "pwd", "date", "whoami", "hostname",
    "uname", "uptime", "df", "du", "free",
    "find", "sort", "uniq", "cut", "tr",
    "env", "printenv", "id", "stat",
}

# 危险模式（黑名单子串），任何命令中出现都会拒绝
_DANGEROUS_PATTERNS: list[str] = [
    "rm -rf", "rm -r", "sudo", "chmod", "chown",
    "mkfs", "dd if=", "> /dev/", "shutdown",
    "reboot", "halt", "poweroff", "kill -9",
    "wget", "curl", "nc ", "ncat",
    "eval", "exec(", "exec ",
    "| sh", "| bash", "| zsh",
    "$(", "`",
]

# 命令执行超时（秒）
_SHELL_TIMEOUT = 8


class ShellExecTool(Tool):
    """安全 shell 命令执行工具。

    安全措施:
      - 白名单命令：仅允许 echo/cat/head/tail/wc/grep/ls/pwd/date 等安全命令
      - 黑名单模式：拒绝 rm/sudo/curl/wget 等危险操作
      - 超时保护：超过 8 秒自动终止
      - 输出截断：最大 50KB
    """

    name: ClassVar[str] = "shell_exec"
    description: ClassVar[str] = (
        "执行安全的 shell 命令。允许的命令: "
        + ", ".join(sorted(_ALLOWED_COMMANDS))
        + "。危险命令（rm/sudo/curl 等）将被拒绝。"
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令。仅支持白名单命令。",
            },
        },
        "required": ["command"],
    }
    timeout: float = 12.0

    async def execute(self, command: str) -> str:
        """安全执行 shell 命令。"""
        cmd = command.strip()

        if not cmd:
            return "Error: empty command"

        # ── 路径沙箱：阻止目录穿越 + 系统敏感目录读取 ──
        # shell=False + shlex.split 时 shlex 按 POSIX shell 词法划词，
        # 不会误伤参数中合法的".."但这些 token 仍是 allow-list 命令的参数，
        # 可被 cat/head/tail/grep/find 用来读 /etc/passwd、/proc/self/environ 等
        # 系统敏感文件 → 必须显式拒绝。
        _PROTECTED_PREFIXES = (
            "/etc/", "/proc/", "/sys/", "/dev/", "/var/", "/root/",
            "/etc", "/proc", "/sys", "/dev", "/var", "/root",
        )
        for token in shlex.split(cmd):
            # 拒绝 ".." 路径段（任意位置）
            if token == ".." or token.startswith("../") or "/../" in token or token.endswith("/.."):
                return (
                    f"Error: Path traversal detected — '{token}' contains '..'. "
                    f"Only files within the workspace directory are allowed."
                )
            # 拒绝绝对路径直指受保护系统目录
            if token in _PROTECTED_PREFIXES or token.startswith(_PROTECTED_PREFIXES):
                return (
                    f"Error: Access to system path '{token}' is blocked. "
                    f"ShellExecTool is sandboxed to workspace files only."
                )

        # 提取命令名（第一个词）
        cmd_name = cmd.split()[0].split("/")[-1] if cmd else ""

        # 黑名单模式检查
        cmd_lower = cmd.lower()
        for pattern in _DANGEROUS_PATTERNS:
            if pattern.lower() in cmd_lower:
                return f"Error: Command blocked — '{cmd_name}' matches dangerous pattern '{pattern}'"

        # 白名单检查
        if cmd_name not in _ALLOWED_COMMANDS:
            return (
                f"Error: Command '{cmd_name}' is not in the allowed list. "
                f"Allowed: {', '.join(sorted(_ALLOWED_COMMANDS))}"
            )

        try:
            loop = asyncio.get_running_loop()
            stdout, stderr, timed_out, exit_code = await loop.run_in_executor(
                None, self._run_command, cmd
            )

            if timed_out:
                return f"Error: Command '{cmd_name}' timed out after {_SHELL_TIMEOUT}s"

            output = stdout.strip()
            if stderr:
                output += f"\n(stderr)\n{stderr.strip()}"

            if not output:
                output = f"(command '{cmd_name}' completed with exit code {exit_code}, no output)"

            # 截断过长输出
            max_out = 50_000
            if len(output) > max_out:
                output = output[:max_out] + f"\n... (truncated, {len(output)} total chars)"

            return output

        except Exception as e:
            return f"Error executing '{cmd_name}': {type(e).__name__}: {e}"

    @staticmethod
    def _run_command(cmd: str) -> tuple[str, str, bool, int]:
        """同步执行命令（在 executor 线程中运行）。"""
        try:
            cmd_list = shlex.split(cmd)
            proc = subprocess.run(
                cmd_list,
                shell=False,
                capture_output=True,
                timeout=_SHELL_TIMEOUT,
                text=True,
                env={**os.environ, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")},
            )
            return proc.stdout, proc.stderr, False, proc.returncode
        except subprocess.TimeoutExpired:
            return "", f"Command timed out after {_SHELL_TIMEOUT}s", True, -1

# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""工具参数安全检测 — 针对 elevated/dangerous 工具的参数级检查。

即使工具名在声明/授权范围内，参数可能包含恶意意图:
  - write_file → 写入系统配置 /etc/passwd, ~/.bashrc
  - terminal → curl | sh, chmod 777, recursive delete
  - skill_manage → 批量删除 skills
  - browser_click → 钓鱼链接

三层防御:
  Level 1: 正则匹配已知攻击模式 (确定性, 零延迟)
  Level 2: 路径/命令语义分析 (启发式, 微延迟)
  Level 3: LLM 意图分类 (自适应, 50-200ms 延迟，仅触发可疑调用时用)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ThreatLevel(str, Enum):
    SAFE = "safe"
    SUSPICIOUS = "suspicious"
    BLOCK = "block"


@dataclass
class ArgCheckResult:
    tool_name: str
    threat: ThreatLevel
    reason: str
    matched_pattern: str = ""


# ═════════════════════════════════════════════════════════════
#  Level 1: 确定性恶意模式 (正则)
# ═════════════════════════════════════════════════════════════

DET_BLOCK_RULES: list[dict] = [
    {
        "tool": "write_file",
        "patterns": [
            (r"/etc/(passwd|shadow|group|sudoers|hosts|resolv\.conf)", "系统认证文件篡改"),
            (r"~?/\.(bashrc|bash_profile|profile|zshrc|zprofile)", "Shell 配置文件劫持"),
            (r"/var/spool/cron/crontabs/", "Cron 任务注入"),
            (r"/lib/systemd/system/", "Systemd 服务劫持"),
            (r"/root/\.(bashrc|bash_profile|\.ssh/authorized_keys)", "Root 账户持久化"),
            (r"/etc/systemd/system/", "系统服务劫持"),
            (r"/boot/", "引导区篡改"),
        ],
    },
    {
        "tool": "terminal",
        "patterns": [
            (r"\b(curl|wget)\s+.*\|\s*(sh|bash|python3?|perl|ruby)\b", "管道远程代码执行"),
            (r"\brm\s+-rf\s+/", "递归根目录删除"),
            (r"\bchmod\s+.*777\b", "全局可写权限提升"),
            (r"\bchown\s+-R\s+root", "递归所有权变更"),
            (r"\biptables\s+-F\b", "防火墙规则清空"),
            (r">\s*/dev/sd[a-z]", "裸设备覆写"),
            (r"\bdd\s+if=", "DD 磁盘操作"),
            (r"\bmount\s+--bind\b", "Bind mount 逃逸"),
            (r"\bnc\s+-[nl]+\b", "Netcat 后门监听"),
            (r"\bpkill\s+-9\b", "强制杀进程"),
            (r">\s*/etc/", "系统配置覆写重定向"),
        ],
    },
    {
        "tool": "shell_exec",
        "patterns": [
            (r"\b(curl|wget)\s+.*\|\s*(sh|bash|python3?|perl|ruby)\b", "管道远程代码执行"),
            (r"\brm\s+-rf\s+/", "递归根目录删除"),
            (r"\bchmod\s+.*777\b", "全局可写权限提升"),
            (r"\bchown\s+-R\s+root", "递归所有权变更"),
            (r"\biptables\s+-F\b", "防火墙规则清空"),
            (r">\s*/dev/sd[a-z]", "裸设备覆写"),
            (r"\bdd\s+if=", "DD 磁盘操作"),
            (r"\bmount\s+--bind\b", "Bind mount 逃逸"),
            (r"\bnc\s+-[nl]+\b", "Netcat 后门监听"),
            (r"\bpkill\s+-9\b", "强制杀进程"),
            (r"\bwhoami\s*\|\s*sudo\b", "权限提升探测"),
            (r"\bchroot\b", "chroot 逃逸风险"),
        ],
    },
    {
        "tool": "skill_manage",
        "patterns": [
            (r"\"action\"\s*:\s*\"delete\".*\"absorbed_into\"\s*:\s*\"\"", "批量 Skill 删除"),
        ],
    },
    {
        "tool": "patch",
        "patterns": [
            (r"/etc/(passwd|shadow|sudoers|hosts)", "系统文件 Patch 篡改"),
            (r"~?/\.(bashrc|profile|ssh/)", "用户环境 Patch 劫持"),
        ],
    },
    {
        "tool": "read_file",
        "patterns": [
            (r"/etc/(passwd|shadow|group|sudoers|resolv\.conf)", "系统认证文件读取"),
            (r"~?/\.(bashrc|bash_profile|profile|zshrc|zprofile)", "Shell 配置文件读取"),
            (r"/root/\.ssh/", "SSH 私钥目录读取"),
            (r"/var/spool/cron/", "Cron 任务读取"),
            (r"/proc/\d+/", "进程内存读取"),
        ],
    },
    {
        "tool": "write_file",
        "patterns": [
            (r"\brm\s+-rf\s+/", "文件内容含根目录删除命令"),
            (r"\bcurl\s+.*\|.*sh\b", "文件内容含远程代码执行"),
            (r"\bwget\s+.*-O-.*\|.*sh\b", "文件内容含远程代码执行"),
            (r"nc\s+-[nl]+.*-e\s+/bin/(sh|bash)", "后门反弹 Shell 写入"),
        ],
    },
]

# ═════════════════════════════════════════════════════════════
#  Level 2: 可疑模式 (启发式，触发 LLM 审查)
# ═════════════════════════════════════════════════════════════

SUSPICIOUS_PATTERNS: list[dict] = [
    {
        "tool": "write_file",
        "patterns": [
            (r"/var/www/", "Web 目录写入"),
            (r"/opt/", "系统可选软件目录写入"),
            (r"\.env$", "环境变量文件写入"),
            (r"config\.(yaml|json|toml)", "配置文件写入"),
            (r"/usr/local/bin/", "系统 PATH 写入"),
        ],
    },
    {
        "tool": "terminal",
        "patterns": [
            (r"\bsudo\b", "提权操作"),
            (r"\bpip\s+install\b", "Python 包安装"),
            (r"\bnpm\s+install\b", "NPM 包安装"),
            (r"\bdocker\s+(run|exec)\b", "Docker 容器操作"),
            (r"\bgit\s+clone\b", "Git 仓库克隆"),
            (r"\bssh\b", "SSH 连接"),
            (r"eval\s+", "Eval 执行"),
            (r"\bexec\b", "Exec 系统调用"),
            (r"\.decode\(|base64\s+-d", "Base64 解码执行"),
        ],
    },
    {
        "tool": "send_message",
        "patterns": [
            (r"(password|token|secret|api.key|credential)", "凭据泄露风险"),
            (r"-----BEGIN\s+(RSA|EC|DSA)\s+PRIVATE\s+KEY-----", "私钥泄露"),
        ],
    },
    {
        "tool": "browser_navigate",
        "patterns": [
            (r"https?://\d+\.\d+\.\d+\.\d+", "IP 直连可疑"),
            (r"\.onion\b", "暗网地址"),
        ],
    },
]


def _check_arguments(tool_name: str, arguments: dict | str) -> ArgCheckResult | None:
    if isinstance(arguments, dict):
        arg_str = json.dumps(arguments, ensure_ascii=False)
    else:
        arg_str = str(arguments)

    for rule in DET_BLOCK_RULES:
        if rule["tool"] != tool_name:
            continue
        for pattern, reason in rule["patterns"]:
            if re.search(pattern, arg_str, re.IGNORECASE):
                return ArgCheckResult(
                    tool_name=tool_name,
                    threat=ThreatLevel.BLOCK,
                    reason=f"DET_BLOCK: {reason}",
                    matched_pattern=pattern,
                )

    for rule in SUSPICIOUS_PATTERNS:
        if rule["tool"] != tool_name:
            continue
        for pattern, reason in rule["patterns"]:
            if re.search(pattern, arg_str, re.IGNORECASE):
                return ArgCheckResult(
                    tool_name=tool_name,
                    threat=ThreatLevel.SUSPICIOUS,
                    reason=f"SUSPICIOUS: {reason}",
                    matched_pattern=pattern,
                )

    return None


def check_tool_call(tool_name: str, arguments: Any) -> ArgCheckResult:
    result = _check_arguments(tool_name, arguments)
    if result is None:
        return ArgCheckResult(
            tool_name=tool_name,
            threat=ThreatLevel.SAFE,
            reason="No threat pattern matched",
        )
    return result

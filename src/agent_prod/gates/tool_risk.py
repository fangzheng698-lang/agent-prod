# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""工具风险分类库 — 所有已知 agent 工具按风险等级标记。

三级风险:
  benign     — 纯只读/无副作用操作，任何 agent 默认可用
  elevated   — 有写入/修改/外部请求，声明+记录即可
  dangerous  — 可执行代码、文件覆写、进程控制、对外通信，需显式授权

Gate0 行为:
  - benign: 无需声明，无需授权，静默记录
  - elevated: 需在 declared_tools 中声明，否则拒；声明了就记录放行
  - dangerous: 需有用户授权记录，否则拒+告警；已授权→记录放行
  - unknown: 不在任何分类库中的工具 → 拒

配置驱动:
  tool_risk.py 优先从 config.yaml 的 tools.risk 和 tools.aliases 加载。
  如果没有配置或配置为空，使用内置 TOOL_RISK 作为默认值。
  新增 agent 只需在 config.yaml 添加 tools.aliases.<agent> 映射，不改代码。
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from typing import ClassVar
from urllib import request


class RiskLevel(str, Enum):
    BENIGN = "benign"          # 无副作用
    ELEVATED = "elevated"      # 有副作用，需声明
    DANGEROUS = "dangerous"    # 高风险，需显式授权


# ═════════════════════════════════════════════════════════════
#  内置默认工具风险分类 (54 tools)
# ═════════════════════════════════════════════════════════════

TOOL_RISK: dict[str, RiskLevel] = {
    # ── 只读/观察: benign ────────────────────────────────
    "read_file":          RiskLevel.BENIGN,
    "search_files":       RiskLevel.BENIGN,
    "session_search":     RiskLevel.BENIGN,
    "skills_list":        RiskLevel.BENIGN,
    "skill_view":         RiskLevel.BENIGN,
    "memory":             RiskLevel.BENIGN,
    "vision_analyze":     RiskLevel.BENIGN,
    "browser_navigate":   RiskLevel.BENIGN,
    "browser_snapshot":   RiskLevel.BENIGN,
    "browser_console":    RiskLevel.BENIGN,
    "browser_vision":     RiskLevel.BENIGN,
    "browser_get_images": RiskLevel.BENIGN,
    "browser_scroll":     RiskLevel.BENIGN,
    "browser_back":       RiskLevel.BENIGN,
    "web_search":         RiskLevel.BENIGN,    # read-only HTTP GET
    "process_poll":       RiskLevel.BENIGN,
    "process_log":        RiskLevel.BENIGN,
    "process_list":       RiskLevel.BENIGN,
    "process":            RiskLevel.BENIGN,    # Hermes unified process action
    "todo":               RiskLevel.BENIGN,    # 自管理，无外部副作用
    "execute_code":       RiskLevel.BENIGN,    # 沙箱内执行，不是本地shell

    # ── 写入/修改: elevated ──────────────────────────────
    "write_file":         RiskLevel.ELEVATED,
    "patch":              RiskLevel.ELEVATED,
    "skill_manage":       RiskLevel.ELEVATED,
    "browser_click":      RiskLevel.ELEVATED,
    "browser_type":       RiskLevel.ELEVATED,
    "browser_press":      RiskLevel.ELEVATED,
    "text_to_speech":     RiskLevel.ELEVATED,

    # ── shell/系统/进程控制: dangerous ──────────────────
    "terminal":           RiskLevel.DANGEROUS,
    "shell_exec":         RiskLevel.DANGEROUS,
    "process_kill":       RiskLevel.DANGEROUS,
    "process_wait":       RiskLevel.DANGEROUS,
    "process_submit":     RiskLevel.DANGEROUS,
    "process_write":      RiskLevel.DANGEROUS,
    "process_close":      RiskLevel.DANGEROUS,

    # ── 对外通信/委托: dangerous ───────────────────────
    "send_message":       RiskLevel.DANGEROUS,
    "delegate_task":      RiskLevel.DANGEROUS,
    "cronjob":            RiskLevel.DANGEROUS,
    "clarify":            RiskLevel.DANGEROUS,  # 可构造社会工程
}

# ── Agent 工具名别名映射 ─────────────────────────────────
# 由 _load_aliases() 从 config.yaml tools.aliases 加载
# 结构: {agent_type: {agent_tool_name: canonical_tool_name}}
_ALIASES: dict[str, dict[str, str]] = {}


def _load_from_config(config: dict | None = None) -> dict[str, RiskLevel]:
    """从 config dict 加载工具风险分类。

    合并策略:
      1. 从 config.tools.risk 读取分类列表
      2. 如果存在，覆盖 TOOL_RISK；否则用内置 TOOL_RISK
    """
    if not config:
        return dict(TOOL_RISK)

    tools_cfg = config.get("tools", {})
    risk_cfg = tools_cfg.get("risk", {})
    if not risk_cfg:
        return dict(TOOL_RISK)

    merged = {}
    for level_name, tool_list in risk_cfg.items():
        try:
            level = RiskLevel(level_name)
        except ValueError:
            continue
        for tool in tool_list:
            merged[tool] = level
    return merged if merged else dict(TOOL_RISK)


def _load_aliases(config: dict | None = None) -> dict[str, dict[str, str]]:
    """从 config dict 加载 per-agent 工具名别名映射。

    结构: {agent_type: {agent_tool_name: canonical_tool_name}}
    """
    if not config:
        return {}
    tools_cfg = config.get("tools", {})
    aliases_cfg = tools_cfg.get("aliases", {})
    if not aliases_cfg:
        return {}
    return {
        agent: dict(mapping)
        for agent, mapping in aliases_cfg.items()
    }


def configure(config: dict | None = None) -> None:
    """用配置覆盖默认风险分类和别名。允许增量更新。"""
    global TOOL_RISK, _ALIASES
    TOOL_RISK = _load_from_config(config)
    _ALIASES = _load_aliases(config)


def resolve_tool_name(tool_name: str, agent_type: str | None = None) -> str:
    """解析 agent 工具名到规范工具名。

    如果 agent_type 有别名映射，先查别名；否则直接返回原工具名。
    """
    if agent_type and agent_type in _ALIASES:
        return _ALIASES[agent_type].get(tool_name, tool_name)
    return tool_name


def get_risk(tool_name: str, agent_type: str | None = None) -> RiskLevel | None:
    """查询工具风险等级。支持 agent 别名解析。未知工具返回 None。"""
    canonical = resolve_tool_name(tool_name, agent_type)
    return TOOL_RISK.get(canonical)


def is_known_tool(tool_name: str, agent_type: str | None = None) -> bool:
    """工具是否在已知分类库中（支持别名）。"""
    canonical = resolve_tool_name(tool_name, agent_type)
    return canonical in TOOL_RISK


# ── 方便查询 ──────────────────────────────────────────────

BENIGN_TOOLS: set[str] = {t for t, r in TOOL_RISK.items() if r == RiskLevel.BENIGN}
ELEVATED_TOOLS: set[str] = {t for t, r in TOOL_RISK.items() if r == RiskLevel.ELEVATED}
DANGEROUS_TOOLS: set[str] = {t for t, r in TOOL_RISK.items() if r == RiskLevel.DANGEROUS}


# ═════════════════════════════════════════════════════════════
#  LLM 自动分类 — 未知工具的语义匹配
# ═════════════════════════════════════════════════════════════

logger = logging.getLogger(__name__)

# 自动分类缓存: {f"{agent_type}:{tool_name}": (canonical_name, risk_level)}
_AUTO_CLASSIFIED: dict[str, tuple[str, RiskLevel]] = {}

TOOL_CLASSIFIER_PROMPT = """You are a tool classification assistant for a security gate system.
Your task: given an unknown tool name and the agent that invoked it, find the BEST semantic match
among known canonical tools, OR propose a new risk level if no good match exists.

Known canonical tools and their risk levels:
{known_tools}

Agent type: {agent_type}
Unknown tool name: {tool_name}

Instructions:
1. First try to match the unknown tool to a known canonical tool by semantic similarity.
2. If you find a match, return the canonical tool name and its risk level.
3. If NO good match exists, propose a new risk level based on the tool name alone:
   - "benign" — read-only, no side effects
   - "elevated" — writes/modifies/external requests
   - "dangerous" — code execution, file overwrite, process control, network communication
4. Be conservative: when uncertain, prefer "elevated" or "dangerous" over "benign".

Respond ONLY with valid JSON, no other text:
{{"match": "canonical_tool_name" or null, "risk": "benign"|"elevated"|"dangerous", "confidence": 0.0-1.0, "reason": "brief explanation"}}"""


def auto_classify_tool(
    tool_name: str,
    agent_type: str | None = None,
    llm_config: dict | None = None,
) -> tuple[str, RiskLevel] | None:
    """Use LLM to semantically classify an unknown tool.

    Args:
        tool_name: The raw tool name (e.g. "exec").
        agent_type: The agent type (e.g. "qclaw").
        llm_config: Dict with keys 'llm_endpoint', 'llm_model', 'llm_api_key',
                    'timeout_seconds' (all optional, falls back to env vars).

    Returns:
        (canonical_tool_name, risk_level) if classified with confidence >= 0.5,
        None on failure/uncertainty.
    """
    agent_type = agent_type or "unknown"
    cache_key = f"{agent_type}:{tool_name}"
    if cache_key in _AUTO_CLASSIFIED:
        return _AUTO_CLASSIFIED[cache_key]

    # Build known tools list for prompt
    known_tools_lines = [
        f"  - {name} ({level.value})"
        for name, level in sorted(TOOL_RISK.items())
    ]
    known_tools_str = "\n".join(known_tools_lines)

    prompt = TOOL_CLASSIFIER_PROMPT.format(
        known_tools=known_tools_str,
        agent_type=agent_type,
        tool_name=tool_name,
    )

    # Resolve LLM config: passed in > env vars
    api_key = (
        (llm_config or {}).get("llm_api_key")
        or os.environ.get("OPENAI_API_KEY", "")
    )
    base_url = (
        (llm_config or {}).get("llm_endpoint")
        or os.environ.get("OPENAI_BASE_URL", "")
    )
    model = (
        (llm_config or {}).get("llm_model")
        or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    )
    timeout = (llm_config or {}).get("timeout_seconds", 10.0)

    if not api_key or not base_url:
        logger.debug("auto_classify_tool: LLM not configured, skipping %s", tool_name)
        return None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a tool classification assistant. Output ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 200,
    }

    try:
        url = f"{base_url.rstrip('/')}/chat/completions"
        req = request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            content = body["choices"][0]["message"]["content"]
            result = _parse_classifier_response(content)
    except Exception as e:
        logger.warning("auto_classify_tool: LLM call failed for %s/%s: %s", agent_type, tool_name, e)
        return None

    match = result.get("match")
    risk_str = result.get("risk")
    confidence = result.get("confidence", 0.0)

    if not risk_str:
        return None

    try:
        risk = RiskLevel(risk_str)
    except ValueError:
        return None

    if match and match in TOOL_RISK and confidence >= 0.5:
        _AUTO_CLASSIFIED[cache_key] = (match, risk)
        logger.info("auto_classify: %s/%s -> %s (%s, confidence=%.2f)",
                     agent_type, tool_name, match, risk.value, confidence)
        return (match, risk)

    if confidence >= 0.6:
        _AUTO_CLASSIFIED[cache_key] = (tool_name, risk)
        logger.info("auto_classify: %s/%s -> NEW %s (confidence=%.2f)",
                     agent_type, tool_name, risk.value, confidence)
        return (tool_name, risk)

    logger.debug("auto_classify: %s/%s low confidence (%.2f), skipping",
                  agent_type, tool_name, confidence)
    return None


def _parse_classifier_response(content: str) -> dict:
    """Extract JSON from LLM classification response."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    if "```" in content:
        try:
            start = content.index("```") + 3
            end = content.index("```", start)
            json_str = content[start:end].strip()
            if json_str.startswith("json"):
                json_str = json_str[4:].strip()
            return json.loads(json_str)
        except (ValueError, json.JSONDecodeError):
            pass
    try:
        start = content.index("{")
        end = content.rindex("}") + 1
        return json.loads(content[start:end])
    except (ValueError, json.JSONDecodeError):
        pass
    return {"match": None, "risk": None, "confidence": 0.0, "reason": f"parse error: {content[:80]}"}


def clear_auto_classified_cache() -> None:
    """Clear auto-classification cache. Used in tests."""
    _AUTO_CLASSIFIED.clear()

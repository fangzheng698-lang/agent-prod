"""agent-prod init — 一键引导式初始化。

引导用户逐步完成关键配置：
  1. LLM endpoint + API key（Gate6 评估用）
  2. 自研 agent 接入（添加 agent + 设 observe/enforce 模式）
  3. 启动服务 + 验证

Usage:
    agent-prod init
"""

from __future__ import annotations

import sys
import os
import json
import urllib.request
from pathlib import Path

from agent_prod.cli_common import CONFIG_PATH, load_config, save_config, live_server
from agent_prod.cli_configure import (
    _generate_default_config,
    _display_config,
    _prompt_str,
    _prompt_choice,
    _confirm,
    KNOWN_AGENTS,
)


def cmd_init(args: argparse.Namespace) -> None:  # noqa: F821
    """增强版一键初始化向导。"""
    print()
    print("=" * 60)
    print("  agent-prod 初始化向导")
    print("=" * 60)
    print()
    print("  本向导会帮你完成：")
    print("    1. 配置 LLM（Gate6 答案质量评估用）")
    print("    2. 添加/配置自研 Agent 的 Gate0 模式")
    print("    3. 启动服务并验证")
    print()

    # ── Step 0: 确保 config 存在 ──
    if not CONFIG_PATH.exists():
        print("  创建默认配置...")
        save_config(_generate_default_config())

    config = load_config()

    # ── Step 1: LLM 配置 ──
    print("-" * 60)
    print("  步骤 1/4: LLM 配置（Gate6 评估用）")
    print("-" * 60)
    print("  Gate6 需要用 LLM 来评估 agent 的回答质量。")
    print()

    gate6 = config.setdefault("gates", {}).setdefault("gate6", {})
    security = config.setdefault("security", {})

    # LLM endpoint
    current = gate6.get("llm_endpoint", "")
    default_endpoint = current or "https://api.openai.com/v1"
    val = _prompt_str("  LLM API 地址", default=default_endpoint)
    if val:
        gate6["llm_endpoint"] = val

    # LLM model
    current_model = gate6.get("llm_model", "")
    default_model = current_model or "gpt-4o-mini"
    val = _prompt_str("  LLM 模型名", default=default_model)
    if val:
        gate6["llm_model"] = val

    # API key — 写到 .env 而不是 config.yaml
    print()
    print("  API Key 可以填在这里，或通过环境变量 OPENAI_API_KEY 传入。")
    current_key = security.get("api_key", "") or os.environ.get("OPENAI_API_KEY", "")
    has_env_key = bool(os.environ.get("OPENAI_API_KEY", ""))
    if has_env_key:
        print(f"  [检测到环境变量 OPENAI_API_KEY 已设置，跳过]")
    else:
        val = _prompt_str("  API Key（留空则不设）", default=current_key, password=True)
        if val:
            dotenv_path = CONFIG_PATH.parent.parent / ".env"
            existing = {}
            if dotenv_path.exists():
                for line in dotenv_path.read_text().splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        existing[k.strip()] = v.strip()
            existing["OPENAI_API_KEY"] = val
            lines = "\n".join(f"{k}={v}" for k, v in existing.items())
            dotenv_path.write_text(lines + "\n")
            print(f"  API key saved to {dotenv_path}")

    # ── Step 2: 工具别名 ──
    print()
    print("-" * 60)
    print("  步骤 2/4: 工具别名配置")
    print("-" * 60)
    print("  如果自研 agent 的工具名和规范名不同，需要配置别名。")
    print("  例如：你的 agent 调用 exec 实际是执行 shell，就映射为 terminal。")
    print()

    tools_cfg = config.setdefault("tools", {})
    aliases = tools_cfg.setdefault("aliases", {})
    known_alias_agents = set(aliases.keys())

    # 展示现有别名
    if known_alias_agents:
        print(f"  已配置别名的 agent: {', '.join(sorted(known_alias_agents))}")
        print()
        for a in sorted(known_alias_agents):
            for tool, mapping in aliases[a].items():
                print(f"    {a}: {tool} → {mapping}")

    # 添加新别名
    if _confirm("  是否添加新的工具别名？", default=False):
        while True:
            print()
            agent_name = _prompt_str("  Agent 名称（留空结束）", default="")
            if not agent_name:
                break
            agent_aliases = aliases.setdefault(agent_name, {})
            while True:
                local_name = _prompt_str("    工具名（你的 agent 里的叫法，留空结束）", default="")
                if not local_name:
                    break
                canonical = _prompt_str(f"    {local_name} → 映射到规范名", default="")
                if canonical:
                    agent_aliases[local_name] = canonical
            print(f"  Agent '{agent_name}' 已配置 {len(agent_aliases)} 个别名")

    # ── Step 3: Agent Gate0 模式 ──
    print()
    print("-" * 60)
    print("  步骤 3/4: Gate0 权限模式")
    print("-" * 60)
    print("  每个 agent 可以单独设置模式：")
    print("    observe  = 只记录不拦截（接入手抖阶段用）")
    print("    enforce = 拦截违规调用（稳定后开启）")
    print()

    gate0 = config.setdefault("gates", {}).setdefault("gate0", {})
    per_agent = gate0.setdefault("per_agent", {})

    # 列出已知 agent + 新 agent 入口
    existing_agents = list(per_agent.keys())
    if existing_agents:
        print(f"  已配置的 agent: {', '.join(existing_agents)}")
        print()
        for a in existing_agents:
            current_mode = per_agent[a].get("mode", "enforce")
            new_mode = _prompt_choice(f"  {a} 的模式", ["observe", "enforce"], default=current_mode)
            per_agent[a]["mode"] = new_mode

    # 添加新 agent
    if _confirm("  是否添加新的 agent？", default=True):
        while True:
            name = _prompt_str("  新 agent 名称（留空结束）", default="")
            if not name:
                break
            mode = _prompt_choice(f"  {name} 的模式", ["observe", "enforce"], default="observe")
            per_agent[name] = {"mode": mode}
            print(f"  Agent '{name}' 已添加（mode={mode}）")

    # ── Step 4: 启动与验证 ──
    print()
    print("-" * 60)
    print("  步骤 4/4: 保存配置、启动服务、验证")
    print("-" * 60)
    print()

    # 保存
    save_config(config)
    print()

    # 询问是否立即启动
    if _confirm("  是否现在启动服务？", default=True):
        import subprocess
        import signal

        # 从环境变量读 API key
        env = os.environ.copy()
        api_key = security.get("api_key", "") or os.environ.get("OPENAI_API_KEY", "")
        if api_key:
            env["OPENAI_API_KEY"] = api_key

        host = "0.0.0.0"
        port = "8000"
        print(f"  启动服务 http://{host}:{port} ...")
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_prod", "serve", "--port", port],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        import time
        time.sleep(3)

        # 验证
        base = live_server()
        try:
            req = urllib.request.Request(f"{base}/health", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            print(f"  ✅ 服务运行中: {base}")
            print(f"  Model: {data.get('model')}")
            print(f"  Gates: {'ENABLED' if data.get('quality_gates') else 'DISABLED'}")
            print(f"  Session: {data.get('sessions_active', 0)} active")
        except Exception as e:
            print(f"  ⚠️ 服务启动中，但 health 检查未通过: {e}")
            print(f"  后台进程 PID: {proc.pid}")

        def _cleanup(sig=None, frame=None):
            if proc.poll() is None:
                proc.terminate()
                print("  服务已停止")

        signal.signal(signal.SIGINT, _cleanup)
        signal.signal(signal.SIGTERM, _cleanup)

    else:
        base = live_server()
        print(f"  配置文件已保存到: {CONFIG_PATH}")
        print(f"  稍后手动启动：")
        print(f"    agent-prod serve")
        print()

    # ── 完成 ──
    print("=" * 60)
    print("  agent-prod 初始化完成！")
    print("=" * 60)
    print()
    print("  常用的操作：")
    print(f"    agent-prod stats                     查看门禁统计")
    print(f"    agent-prod stats --agent qclaw       只看某个 agent")
    print(f"    agent-prod configure --show          查看当前配置")
    print(f"    agent-prod configure --mode observe --agent my-agent  快速设模式")
    print(f"    agent-prod doctor                    健康检查")
    print()
    print("  自研 agent 接入（一行代码）：")
    print("    from agent_prod import trace")
    print('    result = trace(agent="my-agent", session_id="s1", decisions=[...])')
    print()

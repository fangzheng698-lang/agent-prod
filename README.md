# agent-prod — Enterprise AI Agent Quality Gate Infrastructure

**生产级 AI Agent 质量门禁系统。** 任何 agent（Hermes、Claude Code、自研）都可以通过一行代码接入，经过 Gate0-Gate7 质量门评估，决定是否发布到生产环境。

```
agent_prod/
├── gates/          质量门 (Gate0-Gate7)
├── server/         FastAPI REST API 服务
├── gateway/        评估管道编排
├── integration/    qclaw 等外部 agent 集成
├── adaptivity/     数据飞轮 + 因果归因
├── observability/  指标 + 日志
└── trace_client.py 一行代码 SDK
```

## 快速开始

```bash
pip install agent-prod

# 交互式配置 (LLM endpoint、API key、Gate0 模式)
agent-prod configure

# 启动服务
agent-prod serve
```

## 自研 Agent 接入（一行代码）

任何 agent 都可以通过 `trace()` 函数提交数据到门禁系统：

```python
from agent_prod import trace

result = trace(
    agent="my-custom-agent",        # ← 你的 agent 名字
    session_id="session_001",
    decisions=[{
        "decision_id": "d1",
        "model": "gpt-4",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "tool_calls": [{
            "tool_id": "t1",
            "tool_name": "search",
            "arguments": {"query": "weather"},
            "result_summary": "Sunny, 22C",
            "success": True,
            "duration_ms": 120.0,
        }],
    }],
    current_metrics={
        "final_response": "Sunny, 22C",
        "latency_p95_ms": 300,
        "success_rate": 0.99,
    },
)

if result["passed"]:
    print("门禁通过 → 生产发布")
else:
    print(f"被拒绝: {result['failed_at']} - {result['fail_reason']}")
```

## CLI 命令

```bash
# ── 配置 ──
agent-prod configure                  # 交互式配置向导
agent-prod configure --show           # 查看当前配置
agent-prod configure --reset          # 重置为默认配置

# ── 服务 ──
agent-prod serve                      # 启动服务
agent-prod doctor                     # 健康检查

# ── 统计 ──
agent-prod stats                      # 所有 agent 的门禁统计
agent-prod stats --agent qclaw        # 按 agent 筛选
agent-prod stats --agent "qclaw,claude-code,my-agent"  # 多 agent
agent-prod stats --rejected           # 只看被拒绝的
agent-prod stats --detail <id>        # 查看单条评估详情

# ── 飞轮反馈 ──
agent-prod feedback                   # 查看改进建议列表
agent-prod feedback --id <id>         # 查看详情
agent-prod feedback --apply <id>      # 应用改进建议

# ── 监控 ──
agent-prod watch                      # 启动会话监控
```

## 配置说明

### LLM 配置 (Gate6 评估用)

```bash
agent-prod configure
# 依次输入：
#   LLM API endpoint URL
#   LLM model name
#   API key
```

### Gate0 权限模式

每个 agent 可以单独设置模式：

| 模式 | 行为 |
|---|---|
| `observe` | 记录违规但不拦截（推荐：新 agent 接入时） |
| `enforce` | 拦截违规工具调用（推荐：稳定后开启） |

配置方式：
```bash
# 交互式配置时会逐 agent 询问
agent-prod configure
```

或直接编辑 config.yaml：
```yaml
gates:
  gate0:
    per_agent:
      my-agent:
        mode: observe   # 或 enforce
```

### 工具别名

如果自研 agent 的工具名与内置规范名不同，需要配置别名：

```yaml
tools:
  aliases:
    my-agent:
      my_run_shell: terminal     # 映射为危险操作
      my_read_file: read_file    # 映射为安全操作
```

## Gate0-Gate7 质量门

| Gate | 名称 | 作用 |
|---|---|---|
| Gate0 | 权限 | 工具调用 ACL，观察/拦截模式 |
| Gate1 | 预算 | Token 和时间预算检查 + 熔断 |
| Gate2 | 追踪完整性 | LLM ↔ 工具调用 DAG 完整性 |
| Gate3 | 回归检测 | 对比历史基线，检测性能回退 |
| Gate4 | 灰度发布 | 流量逐步放量 |
| Gate5 | 审计 | 发布合规审计 |
| Gate6 | 答案质量 | LLM 评估答案质量（12 项检查清单） |
| Gate7 | 执行一致性 | 校验执行计划、输出与目标是否一致 |

## 安装

```bash
# 基础安装
pip install agent-prod

# 完整安装（Postgres、Prometheus、Jaeger、Unleash）
pip install "agent-prod[all]"
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_PROD_URL` | `http://localhost:8000` | 服务地址 |
| `AGENT_PROD_API_KEY` | - | API 密钥 |
| `OPENAI_API_KEY` | - | LLM 评估用 API key |
| `QUALITY_GATES_MODE` | `memory` | `memory` 或 `production` |

## 版本

0.5.0

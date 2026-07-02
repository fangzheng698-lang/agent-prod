# agent-prod — AI Agent 生产质量门禁

[English](README.md) | [简体中文](README.zh-CN.md) | [设计文档](docs/DESIGN.md) | [MCP 接入](docs/MCP_INTEGRATION.md)

Agent 上线前的 8 道质量门——权限、预算、轨迹完整性、回归检测、灰度、审计、答案质量、执行一致性。像测试代码一样测试 Agent 行为。

```python
from agent_prod import trace

result = trace(
    agent="my-agent",
    session_id="session-001",
    current_metrics={"final_response": "巴黎是法国的首都", "success_rate": 0.99},
)
print(result["status"])  # "production" | "rejected"
```

## 管线

```
Agent 运行 ──▶ Gate0 ──▶ Gate1 ──▶ Gate2 ──▶ Gate3 ──▶ Gate4 ──▶ Gate5 ──▶ Gate6 ──▶ Gate7 ──▶ 批准?
               │         │          │          │          │          │          │          │
             风险      预算      轨迹      回归      灰度      审计      答案      执行
             ACL      检查      DAG      比较      发布      策略      质量      一致性
```

| 道门 | 检查内容 | 拦截条件 |
|---|---|---|
| **Gate0** 权限 | 工具 ACL、参数检查、声明工具强制 | 未声明或高危工具调用 |
| **Gate1** 预算 | Token 和时间预算，熔断器 | 超预算或 LLM 端点降级 |
| **Gate2** 轨迹完整性 | LLM→工具 DAG 完整性 | 孤立工具调用或 LLM 缺失 |
| **Gate3** 回归 | 延迟/成功率/质量漂移 vs 演进基线 | 显著性能或质量下降 |
| **Gate4** 灰度 | 渐进式发布阶段（1%→10%→50%→100%） | 阶段内错误率或延迟飙升 |
| **Gate5** 审计 | 策略即代码：先验门、回滚预案、人肉审批 | 关键策略违规 |
| **Gate6** 答案质量 | Checklist 评估（12 项二值检查）或 LLM-as-judge | 低于 per-agent 阈值 |
| **Gate7** 执行一致性 | 计划与输出对齐、目标达成 | 偏离计划或幻觉执行 |

> **和 Eval 框架的区别：** Eval 框架在真空中对单一维度评分。agent-prod
> 形成闭环——权限→预算→轨迹→回归→发布→审计→质量→一致性——在**第一道**
> 失败的门即拒绝本次运行。这是"这个回答 0.85 分"和"这次 Agent 运行不安全"
> 的区别。[查看设计哲学 →](docs/DESIGN.md)

## 5 分钟快速开始

```bash
# 安装
pip install agent-prod

# 一行代码评估一条 trace（无需启动服务）
python -c "
from agent_prod import trace
result = trace(
    agent='demo', session_id='demo-1',
    decisions=[{'decision_id': 'd1', 'model': 'gpt-4',
                'tool_calls': [{'tool_name': 'web_search', 'arguments': {'q': '天气'}, 'success': True}]}],
    current_metrics={'final_response': '晴天 22°C', 'success_rate': 1.0},
    human_approver='demo',
)
print(f'通过: {result[\"status\"]}')  # production
"

# 或启动服务
agent-prod serve
python examples/basic_trace.py
agent-prod stats
```

## MCP Server

所有 8 道门暴露为 MCP 工具，任何 MCP 客户端（Claude Desktop、Cursor、Cline）
可直接调用。

```bash
pip install "agent-prod[mcp]"
agent-prod-mcp
```

| MCP 工具 | 用途 |
|---|---|
| `evaluate_trace` | 完整 Gate0–Gate7 管线评估 |
| `check_tool_safety` | 单次工具调用的 Gate0 预检 |
| `get_gate_stats` | 历史评估统计 |
| `health_check` | 引擎健康检查 |

→ [完整 MCP 接入指南](docs/MCP_INTEGRATION.md)

## GatePlugin 接口 — 继承一个类即可扩展

每道门都是插件。~30 行代码写自己的门：

```python
from agent_prod.gates.interface import GatePlugin, register_gate
from agent_prod.gates.models import GateName, GateResult, Improvement

class MyCustomGate(GatePlugin):
    name = GateName("my_custom_gate")

    def verify(self, improvement: Improvement) -> GateResult:
        if improvement.candidate_output.get("my_field", 0) >= 90:
            return GateResult(gate_name=self.name, passed=True, reason="OK")
        return GateResult(gate_name=self.name, passed=False, reason="my_field < 90")

    def rollback(self, improvement: Improvement) -> None:
        pass

    @classmethod
    def from_config(cls, config, name):
        return cls()

register_gate(GateName("my_custom_gate"), MyCustomGate)
```

引擎通过 `GatePlugin` ABC 发现所有门——无需猴子补丁，无需 Fork 框架。
→ [完整接口设计 →](docs/DESIGN.md)

## 和 Eval 框架有什么不同

| | Eval 框架 | agent-prod |
|---|---|---|
| **范围** | 给一个回答打分 | 门禁整个运行过程（8 个维度） |
| **流程** | 提交 → 评分 → 报告 | Gate0 → Gate1 → … → 早拒绝 |
| **状态** | 无状态 | 完整状态机：candidate → production → rejected → rolled back |
| **复杂度** | 一个指标 | 策略、审计追踪、灰度、自动回滚 |
| **集成** | 独立使用 | SDK + MCP 服务 + 配置即代码 |

Eval 框架回答"这个输出有多好"。agent-prod 回答"这次 Agent 运行是否安全"。

## 部署

```bash
docker compose up -d  # Postgres + agent-prod + MCP
```

## 证明

| 信号 | 证据 |
|---|---|
| 217 条真实会话 | 在 Hermes traces 上验证通过 |
| 4,345 次工具调用 | 全路径工具风险 + 轨迹完整性 |
| 194 个测试 | CI 全绿 |
| 自评估报告 | [docs/DOGFOOD_REPORT.md](docs/DOGFOOD_REPORT.md) — 70% 通过率 |

## 从这里开始

- [设计文档](docs/DESIGN.md) — 架构决策、GatePlugin ABC、管线拓扑
- [MCP 接入指南](docs/MCP_INTEGRATION.md) — Claude Desktop、Cursor、Cline、Hermes 配置
- [示例](examples/) — 可运行的 trace 和发布场景
- [使用指南](docs/USAGE.md) — CLI、配置、Gate0–Gate7 详解
- [自评估报告](docs/DOGFOOD_REPORT.md) — 我们吃自己的狗粮
- [校准指南](docs/CALIBRATION.md) — 为你的 Agent 调整 Gate5/Gate6 阈值
- [路线图](ROADMAP.md) — 生产验证计划和下一步

## License

MIT License. See [LICENSE](LICENSE).
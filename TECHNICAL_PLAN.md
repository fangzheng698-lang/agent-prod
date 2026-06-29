# agent-prod 技术方案

## 1. 项目定位

**Agent CI/CD 质量管道**。把每次 agent trace 当成一次构建提交，经过 7 道质量门（Gate0-Gate7）评估，最终判断"能否上线"。

```
一句话: 任何 AI agent (Hermes, Claude Code, 自研) → 一行 trace() → 7 道门 → production/rejected
```

## 2. 核心概念

| 概念 | 说明 |
|------|------|
| **Improvement** | 一次 agent trace 的评估提案，含输入数据、门结果、状态 |
| **Gate** | 一道质量门，verify() 返回 passed/rejected + reason |
| **Pipeline** | 7 道门串联执行，任一失败立即回滚并 REJECT |
| **Trace** | agent 执行记录（decisions + tool_calls + metrics） |
| **Baseline** | 历史表现基线，用于回归检测和自动演进 |

## 3. 7 道质量门

| Gate | 名称 | 检测什么 | 阻断性 |
|------|------|---------|--------|
| Gate0 | 工具权限 | 工具调用是否声明 + 是否获授权 + 参数安全审计 | enforce |
| Gate1 | 执行资源 | Schema 契约校验 + token/time 预算 + 熔断降级 | enforce |
| Gate2 | 轨迹完整性 | LLM↔Tool DAG 校验 + OTel 分布式追踪可达性 | enforce |
| Gate3 | 回归检测 | 关键指标 (f1, latency, success_rate) 退化 vs 基线 | enforce |
| Gate4 | 灰度发布 | 1%→10%→50%→100% 灰度状态机 + Prometheus 指标 | enforce |
| Gate5 | 发布审计 | 人工审批 + policy tag 合规 | enforce |
| Gate6 | 答案质量 | LLM judge 评估 final_response 正确性/完整性 | enforce |
| Gate7 | 执行一致性 | 回复是否偏离计划、是否有实质内容 (observe/enforce) | 可配置 |

### Gate 执行顺序

```
Gate0 → Gate7 → Gate1 → Gate2 → Gate3 → Gate4 → Gate5 → Gate6
```

Gate7 放在 Gate1 之前（仅次于 Gate0），确保在资源校验前就发现偏离趋势。

### 熔断降级

Gate1 内置熔断器（Circuit Breaker）：连续失败 N 次后自动跳过，cooldown 后自动恢复。防止 LLM endpoint 故障阻塞所有评估。

## 4. 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     外部 Agent (Hermes / Claude Code / SDK)      │
│                         trace() / POST /v1/agent/evaluate       │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  FastAPI Server  (app.py)                                       │
│   ├─ 解析 AgentTrace  →  Gateway.evaluate_agent_trace()        │
│   ├─ CORS / Auth / RateLimit 中间件                              │
│   └─ /v1/...  (deprecated) + /v2/... 端点                        │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  QualityGateGateway  (gateway.py)                               │
│   ├─ adapter.to_improvement() → Improvement                     │
│   └─ engine.run_pipeline()                                      │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  QualityGateEngine  (engine.py)                                 │
│   ├─ Pipeline 编排 + 超时 + 回滚                                 │
│   ├─ Gate0 (权限) → Gate7 (一致性) → Gate1 (资源) → ... → Gate6  │
│   ├─ 失败: rollback + alert + persist(rejected)                  │
│   └─ 通过: evolve_baseline + persist(production)                 │
└─────────────────────────────────────────────────────────────────┘
```

### 存储层

| 模式 | 实现 | 适用场景 |
|------|------|---------|
| MemoryRepository | 内存 dict | 开发/测试 |
| FileRepository | JSON 文件持久化 | 单机/轻量生产 |
| PostgresRepository | PostgreSQL + JSONB | 生产环境 |

## 5. 外部集成方式

### SDK 层

```python
from agent_prod import trace

result = trace(
    agent="my-agent",
    session_id="ses_001",
    decisions=[...],       # LLM 调用记录
    current_metrics={      # 性能指标
        "final_response": "...",
        "latency_p95_ms": 300,
        "success_rate": 0.99,
    },
    human_approver="admin@example.com",
)

if result["passed"]:  # production
    deploy()
```

### Proxy 模式

agent 把 OpenAI API base URL 指向 agent-prod proxy → 自动拦截 trace + 门禁评估（Gate0 实时拦截 + Gate1-6 会话结束评估）。

### Watchdog 模式

文件系统监听 → 检测到新 session 文件 → 自动提交到 evaluate 端点。

## 6. 自适应闭环

```
评估失败 → 归因引擎 (attribution.py) → 定位根因决策
         → 错误分类 (error_classifier.py) → 5 种错误类型
         → 自动修复提示 → 组装 auto_fix_prompt
         → 数据飞轮 → 飞轮反馈循环 (feedback loop)
```

自动演进：PRODUCTION 通过的 trace → 自动更新 baseline metrics。

## 7. 数据模型

```
Improvement
├── id / name / status (CANDIDATE → PRODUCTION / REJECTED)
├── baseline_output      # 历史基线指标
├── candidate_output     # 本次评估输出
├── budget_* / actual_*  # 预算 vs 实际
├── llm_calls            # LLM 调用列表 (response_id, duration_ms, ...)
├── tool_calls           # 工具调用列表 (request_id, tool, duration_ms, ...)
├── gate_results[]       # 门结果列表 (gate_name, passed, reason, details)
├── human_approver       # 审批人
└── metadata             # 扩展字段 (agent, declared_tools, gate7_mode, ...)
```

## 8. 关键技术决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 每道门独立超时 | ThreadPoolExecutor + future.timeout | 防止单门 hang 死整个管道 |
| 失败立即回滚 | early return，不继续后续门 | 资源隔离 + 错误明确 |
| Gate7 优先于 Gate1 | pipeline 中 GATE7 在 GATE1 之前 | 尽早发现偏离，避免资源浪费 |
| Gate7 observe/enforce | 可配置，默认 observe | 观察模式可在不阻断时积累偏离证据 |
| 基线自动演进 | PRODUCTION 时自动更新 | 降低误报率，适应 agent 正常演化 |
| 熔断降级 | Gate1 内置 circuit breaker | 防止 LLM 故障阻塞全管道 |
| 沙箱二层防御 | Gate0 授权 + 执行器安全检查 | 即使 Gate0 放行，执行器做最终检查 |
| Config 驱动 | config.yaml + 热更新阈值 | 不改代码调阈值 + per-agent 配置 |

## 9. 安全防线

```
第一层: Gate0 工具权限 (声明列表 vs 实际调用)
第二层: Gate0 参数审计 (正则匹配危险模式: password, token, private key 等)
第三层: 沙箱白名单 (tool_executor.py: 路径白+黑名单, 命令黑名单, 线程安全)
第四层: execute_code 受限 exec() (safe_builtins, 禁止 os/subprocess/socket)
第五层: shell 命令不用 shell=True (shlex.split + list 形式)
第六层: 服务器 auth 中间件 (Bearer token, 默认开启)
第七层: 无明文密钥 (全部走 .env / 环境变量)
```

## 10. CLI 命令体系

| 命令 | 功能 |
|------|------|
| `agent-prod serve` | 启动 FastAPI 服务 |
| `agent-prod init` | 交互式初始化向导 |
| `agent-prod configure` | 配置 LLM endpoint / 阈值 / per-agent 模式 |
| `agent-prod doctor` | 健康检查，含数据库 + 各组件状态 |
| `agent-prod stats` | 评估统计（按 agent/status 筛选） |
| `agent-prod evaluate` | 命令行单次 trace 评估 |
| `agent-prod logs` | 历史评估日志 |
| `agent-prod alert` | 门禁告警配置 |
| `agent-prod feedback` | 飞轮改进建议 |
| `agent-prod watch` | 启动会话文件监控 |

## 11. 部署架构

### 开发模式

```
agent-prod serve (memory mode) → 零外部依赖
```

### 生产模式

```
agent-prod serve + PostgreSQL + Prometheus + Jaeger + Unleash
         ↓
docker compose up -d  (6 个服务)
```

### 安全参数化

所有密码/token 通过 `.env` 文件的 `${VAR:-default}` 环境变量替换，零硬编码。
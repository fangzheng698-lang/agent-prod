# agent-prod — 生产级 Agent API 服务

纯 FastAPI + OpenAI 协议 + SQLite，零框架依赖，可上线。

## 一分钟启动

```bash
pip install -r requirements.txt
cp .env.example .env   # 填入 API Key
python3 -m app
```

## Docker 部署

```bash
# 完整生产栈（agent-prod + PostgreSQL + Prometheus + Pushgateway + Jaeger + Unleash）
docker compose up -d

# 仅 agent-prod
docker compose up -d agent-prod
```

## 架构

```
                                  ┌─────────────────────────────────────────┐
                                  │         Phase 3: Quality Gates           │
                                  │  Gate1(Execution) → Gate2(Trajectory)    │
                                  │  → Gate3(Regression) → Gate4(Canary)     │
                                  │  → Gate5(Audit)                          │
                                  └──────────────────┬──────────────────────┘
                                                     │
┌─────────────┐     ┌──────────────┐     ┌──────────▼───────────┐     ┌──────────────┐
│  FastAPI     │────▶│  Runtime     │────▶│  QualityGateGateway   │────▶│  LLM Client  │
│  (app/main)  │     │  (app/runtime)│     │  (app/gateway)       │     │  (app/llm)   │
└─────────────┘     └──────┬───────┘     └──────────────────────┘     └──────────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
     ┌────────▼───┐ ┌─────▼──────┐ ┌───▼──────────┐
     │ ToolRegistry│ │  Budget    │ │  TaskRun     │
     │ + skills/   │ │ Controller │ │  StateMachine│
     │ + tools_ext │ │ (budget.py)│ │ (task_state) │
     └─────────────┘ └────────────┘ └──────────────┘

   Phase 4: Execution Layer
   ┌────────────────────────────────────────────────────┐
   │ BudgetController │ MessageLifecycle │ CrossSession │
   │ TaskRun 状态机   │ (生命期内/跨会话管理)           │
   └────────────────────────────────────────────────────┘

   Phase 5: Gate Stress & Observability
   ┌────────────────────────────────────────────────────┐
   │ GateStressHarness │ ThresholdHeatmap │ Profiling   │
   │ (压力测试/热力图/性能分析)                          │
   └────────────────────────────────────────────────────┘

   Phase 6: Data Flywheel MVP
   ┌────────────────────────────────────────────────────┐
   │ ExecutionLog │ EvalLoop │ Optimizer │ ReleaseMgr   │
   │ (执行→评估→优化→发布 完整闭环)                      │
   └────────────────────────────────────────────────────┘

   Phase 7: Loop Engineer 工业化
   ┌────────────────────────────────────────────────────┐
   │ LoopEngine │ Replay │ Benchmark │ Governance       │
   │ (三层闭环/可回放/基准快照/治理面板)                  │
   └────────────────────────────────────────────────────┘

   Phase 8: agent-prod 完善
   ┌────────────────────────────────────────────────────┐
   │ SSE Streaming │ Extended Tools │ Docker Production │
   │ (流式/网页搜索+文件读+shell/生产镜像)               │
   └────────────────────────────────────────────────────┘

持久化: app/state.py (SQLite + WAL)
观测: structlog + Prometheus + Jaeger
配置: pydantic-settings (.env)
```

## API

| 端点 | 方法 | 说明 | Phase |
|------|------|------|-------|
| `/health` | GET | 健康检查 | 1 |
| `/v1/chat/completions` | POST | Agent 调用（兼容 OpenAI 格式） | 1 |
| `/v1/chat/stream` | GET | SSE 流式响应 | 8.1 |
| `/sessions` | GET | 会话列表 | 1 |
| `/sessions/{id}` | GET | 会话详情 | 1 |
| `/sessions/{id}/messages` | GET | 消息历史 | 1 |
| `/sessions/{id}/gates` | GET | 质量门结果查询 | 3 |
| `/sessions/{id}` | DELETE | 删除会话 | 1 |
| `/executions` | GET | 执行日志（分页） | 6.1 |
| `/releases` | GET | 发布列表 | 6.4 |
| `/releases/{id}/approve` | POST | 审批发布 | 6.4 |
| `/releases/{id}/rollback` | POST | 回滚版本 | 6.4 |
| `/benchmarks` | GET | 基准测试对比 | 7.3 |
| `/governance/status` | GET | 治理面板状态 | 7.4 |
| `/governance/rollback` | POST | 紧急回滚 | 7.4 |

### 调用示例

```bash
# 标准调用
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "帮我算 (1+2)*3"}],
    "system_prompt": "你是一个数学助手。"
  }'

# 流式调用
curl -N "http://localhost:8000/v1/chat/stream?prompt=帮我搜索FastAPI"
```

## 模块清单

### Phase 1-3: 基础 + Quality Gates 中间件

| 模块 | 文件 | 说明 |
|------|------|------|
| 应用入口 | `app/main.py` | FastAPI + lifespan + 路由 |
| 配置 | `app/config.py` | pydantic-settings, .env 加载 |
| LLM 客户端 | `app/llm.py` | OpenAI 兼容协议, httpx |
| 工具系统 | `app/tools.py` | Tool / ToolRegistry |
| 运行时 | `app/runtime.py` | Agent 循环执行 |
| 持久化 | `app/state.py` | SQLite + WAL |
| 数据模型 | `app/schemas.py` | Pydantic V2 models |
| 门禁网关 | `app/gateway.py` | QualityGateGateway |
| 质量门引擎 | `quality_gates/` | 5 道门 + 仓库 + 引擎 |

### Phase 4: 执行层 (Execution Layer)

| 模块 | 文件 | 说明 |
|------|------|------|
| 预算控制 | `app/budget.py` | Token/时间双重约束，超支自动截断 |
| 消息生命周期 | `app/message_lifecycle.py` | 消息创建/追加/截断/淘汰管理 |
| 跨会话记忆 | `app/cross_session_memory.py` | 用户偏好跨 session 继承，LRU 淘汰 |
| 任务状态机 | `app/task_state.py` | PENDING→RUNNING→GATE_EVAL→APPROVED/REJECTED |

### Phase 5: 门禁压力测试 + 可观测

| 模块 | 文件 | 说明 |
|------|------|------|
| 压力测试 | `app/gate_stress.py` | N=1000 批量门禁测试，统计通过率/延迟 |
| 阈值热力图 | `app/threshold_heatmap.py` | Grid search 各门阈值空间，生成热力图 |
| 性能分析 | `app/profiling.py` | 门禁 pipeline 逐 gate 耗时分析 |

### Phase 6: 数据飞轮 MVP

| 模块 | 文件 | 说明 |
|------|------|------|
| 执行日志 | `app/execution_log.py` | 结构化执行轨迹记录，支持分页查询 |
| 评估循环 | `app/eval_loop.py` | 对比 baseline/candidate 指标，Effect Size |
| 优化建议 | `app/optimizer.py` | 基于拒绝分布自动生成阈值调整建议 |
| 发布管理 | `app/release_manager.py` | Candidate→Approved→Production→Rollback |

### Phase 7: Loop Engineer 工业化

| 模块 | 文件 | 说明 |
|------|------|------|
| 闭环引擎 | `app/loop_engine.py` | Execution→Optimization→Release 三层闭环 |
| 可回放 | `app/replay.py` | 完整执行录制与精确复现 |
| 基准测试 | `app/benchmark.py` | 10 标准查询性能基线，release 前自动跑 |
| 治理面板 | `app/governance.py` | 灰度/候选/生产版本统一治理 |

### Phase 8: agent-prod 完善

| 模块 | 文件 | 说明 |
|------|------|------|
| SSE 流式 | `app/sse.py` | Server-Sent Events 流式响应 |
| 扩展工具 | `app/tools_extended.py` | web_search / file_read / shell_exec |
| Docker 生产 | `docker/Dockerfile` | 多阶段构建，非 root，健康检查 |
| 部署编排 | `docker-compose.yml` | agent-prod + 5 基础设施一键部署 |

## 扩展

### 注册新工具

```python
# tools_impl/my_tool.py
from app.tools import Tool

class SearchTool(Tool):
    name = "web_search"
    description = "搜索互联网"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    }

    async def execute(self, query: str) -> str:
        return f"搜索结果: ..."

# 在 app/main.py 的 lifespan 中注册
from tools_impl.my_tool import SearchTool
tools.register(SearchTool())
```

### 内置工具

| 工具名 | 说明 | 来源 |
|--------|------|------|
| `calculator` | 安全数学表达式计算 | `tools_impl/calculator.py` |
| `web_search` | 模拟网页搜索 | `app/tools_extended.py` |
| `file_read` | 安全文件读取（最大100KB） | `app/tools_extended.py` |
| `shell_exec` | 安全命令执行（白名单+黑名单） | `app/tools_extended.py` |

## 基础设施

| 服务 | 端口 | 说明 |
|------|------|------|
| agent-prod | 8000 | Agent API 主服务 |
| PostgreSQL 16 | 5432 | 关系数据库 |
| Prometheus | 9090 | 指标收集与查询 |
| Pushgateway | 9091 | 指标推送桥梁 |
| Jaeger | 16686 | 分布式追踪 (OTLP: 4317) |
| Unleash | 4242 | Feature Flag 中心 |

## 技术栈

| 组件 | 选型 | 为什么 |
|------|------|--------|
| Web 框架 | FastAPI | 行业标准，异步原生 |
| LLM 协议 | OpenAI 兼容 | 事实标准，所有 provider 兼容 |
| 数据库 | SQLite + WAL | 零运维，单机够用 |
| 门禁 | Quality Gates 5道门 | 生产不放行 |
| 观测 | structlog + Prometheus + Jaeger | 全链路可观测 |
| 配置 | pydantic-settings (.env) | 12-factor app |
| 部署 | Docker + docker compose | 一键部署 6 服务 |
| 依赖数 | 6 个核心包 | 五年内不会大变 |

## 测试

```bash
# 运行所有测试
python3 test_phase1_real.py
python3 test_phase2_infra.py
python3 test_phase3_middleware.py
python3 test_phase4_budget.py
python3 test_phase4_lifecycle.py
python3 test_phase4_memory.py
python3 test_phase4_state_machine.py
python3 test_phase5_heatmap.py
python3 test_phase5_stress.py
python3 test_phase5_profiling.py
python3 test_phase6_log.py
python3 test_phase6_eval.py
python3 test_phase6_optimizer.py
python3 test_phase6_release.py
python3 test_phase7_loop.py
python3 test_phase7_benchmark.py
python3 test_phase7_replay.py
python3 test_phase7_governance.py
python3 test_phase8_tools_extended.py
python3 test_phase8_infra.py
```

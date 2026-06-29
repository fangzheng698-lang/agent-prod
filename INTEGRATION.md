# agent-prod 集成指南

> 任何人都能在 1 行代码内为自己的 agent 接入 7 道质量门禁 + 数据飞轮。
> 无论 agent 用什么语言、什么框架、是否自研。

---

## 总览：3 种集成方式

| 方式 | 侵入度 | 改几行代码 | 捕获数据 | 适用场景 |
|---|---|---|---|---|
| **① 代理模式** | **零侵入** | 改 1 个 URL | LLM 调用链、token 消耗、成功/失败 | **任何 agent，任何语言** |
| **② 装饰器模式** | 低侵入 | +2 行（import + @） | 函数级输入/输出、执行时间 | Python 自研 agent |
| **③ HTTP 推模式** | 低侵入 | ~10 行 | 完整的 trace（含工具调用细节） | 深度集成、自定义场景 |

---

## 方式 ① 代理模式（推荐，零侵入）

### 原理

agent-prod 启动一个透明的 LLM 代理端点。agent 只需把 `base_url` 指向这个代理，所有 LLM 请求自动经过 agent-prod。

```
  别人 agent                   agent-prod                    OpenAI
  LLM 请求 ──────→  /v1/proxy/chat/completions ──────→  /chat/completions
                    │                                     │
                    ├ Gate0 前置检查（工具安全）           └ 返回原始响应
                    ├ 累积 decision/token 到 session      │
                    └ 当 session 结束时，自动跑 Gate1-6   └ 返回给 agent
```

### 集成步骤

**第 1 步：启动 agent-prod 服务**

```bash
# 方式 A：Docker（推荐）
docker compose up

# 方式 B：直接运行
agent-prod serve
# 或 QUALITY_GATES_MODE=memory python -m agent_prod serve
```

**第 2 步：让别人改 1 行配置**

```python
# 改之前（以 OpenAI SDK 为例）
client = OpenAI(base_url="https://api.openai.com/v1")

# 改之后 —— 指向 agent-prod 代理
client = OpenAI(base_url="http://你的-agent-prod-地址:8000/v1/proxy")
```

**Anthropic SDK（Claude Code 等）：**

```python
# 改之前
client = Anthropic()

# 改之后
client = Anthropic(base_url="http://你的-agent-prod-地址:8000/v1/proxy")
```

**原生 httpx / curl：**

```bash
# 改之前
curl https://api.openai.com/v1/chat/completions ...

# 改之后 —— 改 URL
curl http://你的-agent-prod-地址:8000/v1/proxy/chat/completions ...
```

### 效果

一旦 agent 调用 LLM，agent-prod 自动：

1. **Gate0 前置检查** — 检查工具声明的安全风险，危险工具有授权才放行
2. **累积 trace** — 每次 LLM 调用的 prompt/completion token、工具调用链自动记录
3. **会话结束后自动评估** — agent 发 `x-end-session: true` 标记结束，或心跳超时自动触发
4. **返回门禁结果** — 可以通过 `GET /v1/proxy/sessions/{session_id}` 查询评估结果

### 终止会话

```python
# 标记当前会话结束（agent-prod 收到后立即触发门禁评估）
client.chat.completions.create(
    model="gpt-4",
    messages=[...],
    extra_headers={"x-end-session": "true"},
)
```

---

## 方式 ② 装饰器模式（Python 自研 agent）

如果你的 agent 是用 Python 写的，有一个统一的 `run()` 入口函数，用装饰器是最简单的方式。

### 集成步骤

**第 1 步：安装**

```bash
pip install agent-prod httpx
```

**第 2 步：加 2 行代码**

```python
# 改之前 —— 你的原始 agent
def my_agent(query: str) -> str:
    # ... 自研逻辑 ...
    return "结果"

# 改之后 —— 加 @agent_gate
from agent_prod.integration import agent_gate

@agent_gate(agent_type="my-agent", endpoint="http://localhost:8000")
def my_agent(query: str) -> str:
    # ... 完全不变的自研逻辑 ...
    return "结果"
```

**第 3 步：启动 agent-prod**

```bash
docker compose up

# 或者一个命令启动
agent-prod serve
```

**第 4 步：运行**

```python
result, gate_report = my_agent("帮我查一下数据")

if gate_report["passed"]:
    print(f"✅ 门禁通过：{result}")
else:
    print(f"❌ 被 {gate_report['failed_at']} 拦截：{gate_report['fail_reason']}")
```

### 带工具声明的完整示例

```python
from agent_prod.integration import agent_gate

@agent_gate(
    agent_type="customer-support-v2",
    endpoint="http://localhost:8000",
    version="2.1.0",
    declared_tools=["search_kb", "create_ticket", "calculate"],
    proxy_llm=True,        # 自动将 LLM 请求指向代理
    raise_on_reject=True,  # 门禁不通过时抛出异常
)
def support_agent(ticket_id: str) -> dict:
    # 你的 LLM 调用、工具调用、业务逻辑
    # proxy_llm=True 时，OPENAI_BASE_URL 自动设置为代理地址
    return {"resolved": True, "ticket_id": ticket_id}
```

### 异步 agent

```python
@agent_gate(agent_type="async-agent")
async def run(input_data: str) -> str:
    result = await your_async_logic(input_data)
    return result

result, gate = await run("hello")
```

### 无 LLM 代理（纯函数评估）

如果只是想评估函数的输入输出质量（不涉及 LLM 调用拦截）：

```python
@agent_gate(agent_type="no-llm-agent", proxy_llm=False)
def pure_function(x: int) -> int:
    return x * 2
```

---

## 方式 ③ HTTP 推模式（完整控制）

在 agent 运行完成后，将 trace 打包成 JSON，POST 到 `/v1/agent/evaluate`。

### 集成步骤

**第 1 步：构建 trace**

```python
import httpx

trace = {
    "agent": "my-agent",
    "version": "1.0.0",
    "session_id": "ses_001",
    "decisions": [
        {
            "decision_id": "turn-1",
            "model": "gpt-4",
            "prompt_tokens": 500,
            "completion_tokens": 300,
            "tool_calls": [
                {
                    "tool_id": "tc-1",
                    "tool_name": "search",
                    "arguments": {"q": "天气"},
                    "result_summary": "晴天",
                    "success": True,
                    "duration_ms": 200,
                }
            ],
        }
    ],
    "current_metrics": {
        "latency_p95_ms": 5000,
        "success_rate": 1.0,
        "final_response": "今天是晴天",
    },
    "declared_tools": ["search"],
}
```

**第 2 步：POST 到门禁端点**

```python
resp = httpx.post("http://localhost:8000/v1/agent/evaluate", json=trace)
gate_result = resp.json()

print(gate_result["status"])      # "production" | "rejected" | ...
print(gate_result["passed"])      # True / False
print(gate_result["failed_at"])   # 被哪道门拦截
print(gate_result["fail_reason"]) # 拦截原因
print(gate_result["gates"])       # 每道门的详细结果
```

**第 3 步：查询历史评估**

```python
# 获取所有 agent 的统计
resp = httpx.get("http://localhost:8000/v1/agent/stats")
print(resp.json()["by_status"])
# {"production": 2909, "rejected": 77}

# 获取某个 agent 的统计
resp = httpx.get("http://localhost:8000/v1/agent/stats?agent=my-agent")
```

---

## 对比总结

```
                   代理模式                   装饰器模式                 HTTP 推模式
图层：

  别人 agent       改 base_url               @agent_gate              trace JSON
                       │                          │                       │
                       ▼                          ▼                       ▼
               ┌──────────────┐           ┌──────────────┐        ┌──────────────┐
               │  proxy 端点   │           │  装饰器拦截   │        │  trace 端点   │
               │  Gate0 前置   │           │  set proxy   │        │  7 道门评估   │
               │  累积 session │           │  捕获 I/O    │        │              │
               │  后台自动评估  │           │  提交 trace   │        │              │
               └──────────────┘           └──────────────┘        └──────────────┘
                       │                          │                       │
                       ▼                          ▼                       ▼
                7 道门禁 + 飞轮              7 道门禁 + 飞轮         7 道门禁 + 飞轮

侵入度：        零侵入（改 1 个 URL）        低侵入（+2 行代码）      低侵入（~10 行代码）
数据捕获：      LLM 调用链 + token          函数 I/O + 执行时间      最完整（含工具细节）
适用语言：      Python/JS/Go/Java/...        Python 只               Python/JS/Go/Java/...
适用 agent：    任何 agent                  有统一 run() 入口         任何 agent
```

---

## 实操示例：集成自研 agent-framework

你的 `简单agent-framework` 有 `AgentRuntime.run()` 作为统一执行入口，用装饰器模式只需改 1 行：

```python
from agent_prod.integration import agent_gate

@agent_gate(agent_type="my-agent", declared_tools=["calculator"])
async def cli_run(query: str):
    llm = LLMClient(...)
    runtime = AgentRuntime(llm, tools, max_turns=10)
    messages = [{"role": "user", "content": query}]
    final_messages, turns = await runtime.run(messages)
    return turns[-1].response.content if turns else ""

# 使用
result, gate_report = await cli_run("计算 (1+2)*3")
```

---

## 常见问题

### Q：代理模式下，拿到门禁结果？

评估完成后，通过 session ID 查询结果：

```bash
curl http://localhost:8000/v1/proxy/sessions/ses_xxx
```

### Q：agent 崩溃了，代理怎么感知？

Heartbeat Monitor 默认 120 秒无活动自动标记为 CRASHED，触发门禁评估。

### Q：我只想拦截非法工具调用（Gate0），不需要其他门禁？

在配置中只开启 Gate0：

```bash
QUALITY_GATES_MODE=memory agent-prod serve
```

默认配置下的 Gate0 是 observe（警告不拦截）模式，通过 API 热切换为 enforce：

```bash
curl -X POST http://localhost:8000/v1/gate0/mode \
  -H 'Content-Type: application/json' \
  -d '{"agent": "my-agent", "mode": "enforce"}'
```

### Q：集成后 agent 变慢了？

门禁管道本身延迟约 2ms（见 `/v1/agent/stats` 确认）。代理模式额外增加一次 LLM 数据转发，延迟约 5-10ms。

### Q：不在同一台机器上？

部署 agent-prod 到服务器上，别人把 `endpoint` 或 `base_url` 指向你的服务器地址即可。Docker 默认映射 8000 端口。

### Q：怎么看到门禁效果？

```bash
# 健康检查
curl http://localhost:8000/health

# 查看所有 gate 是否启用
# → quality_gates: true

# 发送一条测试 trace
curl -X POST http://localhost:8000/v1/agent/evaluate/dry-run \
  -H 'Content-Type: application/json' \
  -d '{"agent":"test","session_id":"test-1","decisions":[]}'

# 查看统计数据
curl http://localhost:8000/v1/agent/stats
```

---

## 方式 ④ QClaw 集成（零侵入，改 1 个配置字段）

QClaw 是一个基于本地代理 LLM 的 AI 助手桌面客户端。agent-prod 可以直接接管其 LLM 通信或分析其 session 文件。

### 方案 A：代理模式（推荐，改 1 个配置字段）

QClaw 所有 LLM 请求走本地代理（默认 `127.0.0.1:19000`）。改一个配置文件，让它走 agent-prod：

**改 `~/.qclaw/agents/main/agent/models.json`：**

```json
{
  "providers": {
    "qclaw": {
      "baseUrl": "http://127.0.0.1:8000/v1/proxy",
      "apiKey": "__QCLAW_AUTH_GATEWAY_MANAGED__",
      "api": "openai-completions"
    }
  }
}
```

**效果：**

```
QClaw                                agent-prod                          qclaw 原始代理
LLM 请求 -----> /v1/proxy/chat/completions -----> http://127.0.0.1:19000/proxy/llm
              |
              +-- Gate0 前置检查（工具安全）
              +-- 累积每次 LLM 调用的 token/工具链
              +-- QClaw 会话结束时自动评估 Gate1-6
```

优点：
- **改 1 个 JSON 字段**，零代码改动
- 实时拦截 + 评估，不依赖文件变化
- Gate0 可以直接拦截危险工具调用

### 方案 B：文件监控模式（零侵入，无需改 QClaw 配置）

QClaw 每次对话保存为一个 `.jsonl` 文件在：
```
~/.qclaw/agents/main/sessions/<uuid>.jsonl
```

通过 watchdog 自动监控：

```bash
# 启动 QClaw watchdog（后台监控新 session）
python -m agent_prod.integration.qclaw_watchdog \
  --url http://localhost:8000 \
  --interval 5.0

# 或者指定 agent 类型
python -m agent_prod.integration.qclaw_watchdog \
  --agent-type qclaw \
  --url http://localhost:8000
```

**工作原理：**

```
~/.qclaw/agents/main/sessions/ 目录
  +-- watchdog 每 5 秒轮询
  +-- 发现新的 .jsonl 文件
  +-- qclaw_parser 解析 JSONL -> AgentTrace
  +-- POST /v1/agent/evaluate
  +-- 返回 7 道门禁结果
```

**数据映射：**

| qclaw 数据 | AgentTrace 字段 | 对应门禁 |
|---|---|---|
| model_change.modelId | decisions[].model | Gate0/Gate1 |
| message.usage.input/output | prompt_tokens / completion_tokens | Gate1 预算 |
| message.role:assistant + toolCall | decisions[].tool_calls[] | Gate2 DAG 完整性 |
| toolResult.details.exitCode | tool_calls[].success / duration_ms | Gate3 回归 |
| 文件时间戳 | current_metrics.latency_p95_ms | Gate4 灰度 |
| toolResult.toolName | declared_tools | Gate5 审计 |
| assistant.content.text | current_metrics.custom.final_response | Gate6 答案质量 |

### 方案 A vs B

| 维度 | A：代理模式 | B：文件监控 |
|---|---|---|
| 改 QClaw 配置 | **改 1 个 JSON 字段** | 不用改 |
| 实时性 | **实时** | 延迟（对话结束后才触发） |
| Gate0 拦截 | **支持**（拒绝危险工具调用） | 不支持（只做评估） |

**推荐：初次用方案 B 零风险验证，确认数据正常后切换方案 A 实时保护。**

---

## 总览：4 种集成方式

| 方式 | 侵入度 | 改几行代码 | 适用场景 |
|---|---|---|---|
| **① 代理模式** | **零侵入** | 改 1 个 URL | **任何 agent，任何语言** |
| **② 装饰器模式** | 低侵入 | +2 行（import + @） | Python 自研 agent |
| **③ HTTP 推模式** | 低侵入 | ~10 行 | 深度集成、自定义场景 |
| **④ QClaw 集成** | **零侵入** | 改 1 个 JSON 字段 或 0 行 | QClaw 桌面客户端 |

---

## 常见问题

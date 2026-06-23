# agent-prod 全量实施计划

> **For Hermes:** Use subagent-driven-development skill to implement each phase task-by-task.

**目标:** 从当前 Phase 3 完成状态出发，依次完成 执行层 → 门禁压力测试 → 数据飞轮 MVP → Loop Engineer 工业化 → agent-prod 完善，交付一个成熟的、可验证的、可回滚的持续优化工程系统。

**架构:** 在现有 agent-prod FastAPI + Quality Gates 中间件基础上，向下补执行层状态机和预算控制，向上建门禁性能可观测面板，侧翼打通数据飞轮闭环（执行→评估→优化→发布），最后参照 Loop Engineer 19 篇设计文档做工业化治理。

**技术栈:** Python 3.11+, FastAPI, asyncpg/SQLite, structlog, Prometheus, Jaeger, Pydantic V2

---

## 当前基线

| Phase | 状态 | 内容 |
|-------|------|------|
| Phase 1 | ✅ | 5 道质量门核心引擎 + 6/6 预设场景 |
| Phase 2 | ✅ | 真实基础设施 (Postgres, Prometheus, Jaeger, Unleash) 27/27 |
| Phase 3 | ✅ | 质量门中间件接入 agent-prod API，百智云真 LLM 跑通 10/10 |

35 个文件，~4300 行代码。Docker 5 服务运行中。

---

## Phase 4: 执行层 (Execution Layer)

对应之前的 Option C：message lifecycle、cross-session memory、token/time budget。

### Task 4.1: BudgetController — 预算控制核心

**目标:** 每次 Runtime 执行受 token 和时间双重预算约束，超支自动截断并记录。

**文件:**
- 新建: `app/budget.py`
- 修改: `app/runtime.py` (注入 BudgetController)
- 测试: `test_phase4_budget.py`

**实现要点:**
- BudgetController 包含 `token_limit`, `time_limit_ms`, 当前消耗追踪
- Runtime 每轮后调用 `budget.check_and_report(tokens_used, time_ms)` → 返回 `(ok, reason)`
- 超支时返回 `BudgetExceeded` 异常，Runtime catch 后标记 `finish_reason="budget"`
- 超支事件记入 structlog (`event="budget_exceeded"`)
- Token 统计使用 LLM response 的 `usage.prompt_tokens + usage.completion_tokens`
- 时间统计使用 `time.monotonic()` 差值（不含 gate 时间）

### Task 4.2: MessageLifecycle — 消息生命周期管理

**目标:** 消息在 session 中的创建/追加/截断/淘汰由统一的生命周期管理器处理。

**文件:**
- 新建: `app/message_lifecycle.py`
- 修改: `app/main.py` (chat_completions 中使用 lifecycle)
- 测试: `test_phase4_lifecycle.py`

**实现要点:**
- `MessageLifecycle` 类: `add_user()`, `add_assistant()`, `add_tool_result()`, `trim_to_budget(token_limit)`
- Token 估算使用简单的 char/4 近似（不需要精确 tokenizer 依赖）
- `trim_to_budget` 保留 system prompt + 最近 N 轮，老消息从中间截断
- 每次截断事件记 structlog (`event="messages_trimmed"`)
- 消息总量上限 100K tokens（与 `max_tokens` 对齐）

### Task 4.3: CrossSessionMemory — 跨会话记忆

**目标:** 用户在不同 session 之间的偏好/事实/习惯能被新 session 继承。

**文件:**
- 新建: `app/cross_session_memory.py`
- 修改: `app/main.py` (lifespan 注入 + chat_completions 注入记忆)
- 修改: `app/schemas.py` (SessionInfo 增加 memory 相关字段)
- 测试: `test_phase4_memory.py`

**实现要点:**
- `CrossSessionMemory` 使用 SQLite 表 `user_memory`:
  - `key TEXT`, `value TEXT`, `category TEXT`, `updated_at TEXT`, `access_count INT`
- 每次 chat_completions 开始时，检索相关记忆注入 system prompt:
  - `[PERSISTENT MEMORY]\n- fact 1\n- fact 2\n`
- 每次对话结束后，LLM 提取值得记住的事实（可选：用 LLM 调用来总结）
- Phase 4 MVP 用规则提取：用户明确说"记住"/"以后都"等关键词触发存储
- 记忆上限 50 条，LRU 淘汰

### Task 4.4: TaskRun 状态机

**目标:** 每次 Runtime 执行有清晰的状态流转，支撑后续飞轮闭环。

**文件:**
- 新建: `app/task_state.py`
- 修改: `app/main.py` (chat_completions 中使用状态机)
- 测试: `test_phase4_state_machine.py`

**实现要点:**
- 状态枚举: `PENDING → RUNNING → GATE_EVAL → APPROVED | REJECTED | ROLLED_BACK`
- 状态变更记录到 SQLite `task_runs` 表:
  - `run_id, session_id, status, started_at, updated_at, gate_status, error`
- 每次状态变更记 structlog (`event="task_state_transition"`)
- 关联 Improvement ID: `task_run_{run_id}` ↔ `imp-{session_id}`
- GET `/sessions/{id}/runs` 新端点

### Task 4.5: 集成验证

**目标:** Phase 4 全部模块集成到 agent-prod，端到端验证。

**文件:**
- 新建: `test_phase4_e2e.py`

**验证项:**
1. Budget 超支截断 → finish_reason="budget" ✅
2. 消息过多自动修剪 → 老消息被移除 ✅
3. 跨会话记忆存储和召回 ✅
4. 状态机正常流转 ✅
5. Budget + Lifecycle + Memory 三者不冲突 ✅

---

## Phase 5: 门禁压力测试 + 阈值热力图

### Task 5.1: Gate Stress Harness

**目标:** 批量生成 Improvement 并跑全量门禁 pipeline，统计通过率和瓶颈。

**文件:**
- 新建: `tests/gate_stress_test.py`

**实现要点:**
- 生成 N=1000~10000 个 Improvement（参数随机化）:
  - confidence: uniform(0, 1)
  - token_count: randint(0, 500000)
  - tool_calls: 随机配对/错配（模拟 trace 完整性不同场景）
  - 消息内容: 随机长度
- 跑 pipeline 统计:
  - 各 gate 的 pass_rate
  - 各 gate 的 p50/p95/p99 duration
  - 整体 rejection_rate
- 输出 CSV: `stress_results.csv`

### Task 5.2: Threshold Heatmap

**目标:** 可视化各 gate 阈值在不同参数下的通过/失败边界。

**文件:**
- 新建: `tests/gate_heatmap.py`

**实现要点:**
- Grid search Gate1 参数空间:
  - confidence: 0.0, 0.1, ..., 1.0
  - token_count: 0, 1000, 5000, ..., 100000
- Grid search Gate3 参数空间:
  - baseline.latency_p95_ms vs candidate.latency
- 生成 heatmap CSV，用 Python matplotlib 或简单 ASCII art 输出
- 输出到 `heatmaps/gate1_heatmap.csv`, `heatmaps/gate3_heatmap.csv`

### Task 5.3: Performance Profiling

**目标:** 找出门禁 pipeline 的性能瓶颈。

**文件:**
- 修改: `quality_gates/engine.py` (添加 profiling 装饰器)

**实现要点:**
- 每道门的 `verify()` 函数计时
- Gate4 灰度阶段拆解（1%/10%/50%/100% 各自耗时）
- 输出 profiling 报告: `profiling/gate_profile_{timestamp}.json`

---

## Phase 6: 数据飞轮 MVP

**目标:** 执行 → 评估 → 优化 → 发布 的完整闭环。

### Task 6.1: Execution Log

**目标:** 每次 agent 执行的完整轨迹存入结构化日志表。

**文件:**
- 新建: `app/execution_log.py`

**实现要点:**
- SQLite 表 `execution_logs`:
  - `log_id, session_id, run_id, improvement_id, prompt_tokens, completion_tokens, duration_ms, tool_calls_count, gate_status, created_at`
- 每次 chat_completions 完成后自动写入
- GET `/executions` 端点（分页，筛选）

### Task 6.2: Evaluation Loop

**目标:** 基于历史执行数据，自动评估改进效果。

**文件:**
- 新建: `app/evaluator.py`

**实现要点:**
- 对比 baseline 和 candidate 的执行指标:
  - latency: 平均响应时间变化
  - accuracy: 工具调用成功率变化
  - cost: token 消耗变化
- 计算 Effect Size（Cohen's d 简化版）
- 输出评估报告: `{improvement_id: {score, verdict, details}}`

### Task 6.3: Optimization Suggestion

**目标:** 基于评估结果，自动生成下一轮改进建议。

**文件:**
- 新建: `app/optimizer.py`

**实现要点:**
- 分析 rejected improvements 的 fail_gate 分布
- 生成建议:
  - 如果 gate1 频繁失败 → 建议调整 confidence 阈值
  - 如果 gate3 频繁失败 → 建议增大 regress_pct
  - 如果 gate4 在某个灰度阶梯失败 → 建议调整该阶梯参数
- 输出 Suggestion 列表: `[{gate, current_threshold, suggested_threshold, confidence}]`

### Task 6.4: Release Manager

**目标:** 管理从 candidate → production 的发布流程。

**文件:**
- 新建: `app/release_manager.py`

**实现要点:**
- `ReleaseCandidate`: {improvement_id, version, gate_results, status}
- `ReleaseManager.review()`: 人工审批接口（当前用 api_key 模拟）
- `ReleaseManager.rollout()`: 发布到 production
- `ReleaseManager.rollback()`: 回滚到上一个 stable 版本
- GET `/releases`, POST `/releases/{id}/approve`, POST `/releases/{id}/rollback`

---

## Phase 7: Loop Engineer 工业化

参照用户 19 篇设计文档的 3-layer 架构。

### Task 7.1: 三层闭环架构搭建

**目标:** execution → optimization → release 三层分离。

**文件:**
- 新建: `layers/execution.py`
- 新建: `layers/optimization.py`
- 新建: `layers/release.py`

**实现要点:**
- execution: 包装 AgentRuntime + Budget + Lifecycle + Gate
- optimization: 包装 Evaluator + Optimizer + 候选版本管理
- release: 包装 ReleaseManager + 灰度 + 回滚

### Task 7.2: 可回放机制

**目标:** 任何一次执行都可以用相同输入精确复现。

**文件:**
- 新建: `app/replay.py`

**实现要点:**
- 录制: 存储完整 messages + tool_results + LLM responses
- 回放: 给定 session_id，从录制数据重建执行过程
- 不可回放则标记 `replayable=False`
- 验证: 回放结果与原始结果比对

### Task 7.3: Benchmark 快照

**目标:** 建立性能基线，后续改进可以对比。

**文件:**
- 新建: `app/benchmark.py`

**实现要点:**
- 基准测试集: 10 个标准查询（数学/逻辑/搜索/多轮）
- 每次 release 前自动跑 benchmark
- 存储结果: `benchmarks/` 目录，JSON 格式
- GET `/benchmarks` 对比当前 vs baseline

### Task 7.4: 治理面板

**目标:** 灰度/候选版本/回滚 的统一治理界面。

**文件:**
- 新建: `app/governance.py`

**实现要点:**
- 统一查询: 当前 production 版本、灰度中的版本、候选中的版本
- 灰度状态: 每个版本的 traffic_percentage
- GET `/governance/status`
- POST `/governance/rollback` (紧急回滚)

---

## Phase 8: agent-prod 完善

### Task 8.1: SSE 流式响应

**目标:** `/v1/chat/completions` 支持 `stream=true`。

**文件:**
- 修改: `app/main.py` (新增流式端点)
- 修改: `app/llm.py` (stream 方法)

**实现要点:**
- `LLMClient.chat_stream()` 返回 `AsyncIterator[str]`
- `chat_completions` 检测 `req.stream`，切换流式模式
- SSE 格式: `data: {"choices":[{"delta":{"content":"..."}}]}\n\n`

### Task 8.2: 新增工具

**目标:** agent 可以搜索网页、读写文件、执行 shell。

**文件:**
- 新建: `tools_impl/web_search.py`
- 新建: `tools_impl/file_tool.py`
- 新建: `tools_impl/shell_tool.py`

**实现要点:**
- web_search: 使用 httpx 调用 DuckDuckGo API
- file_tool: read/write 操作限制在 `workspace/` 目录
- shell_tool: 命令白名单 (ls, cat, grep, find, python3)，其他需审批

### Task 8.3: Docker 部署完善

**目标:** 一键部署 agent-prod 到生产环境。

**文件:**
- 新建: `Dockerfile`
- 修改: `docker-compose.yml` (加入 agent-prod 服务)
- 新建: `deploy.md`

**实现要点:**
- 多阶段 Dockerfile (slim base)
- docker-compose 中 agent-prod 依赖 postgres
- 健康检查: curl /health
- deploy.md: 完整部署文档

### Task 8.4: README 更新

**文件:**
- 修改: `README.md`

**实现要点:**
- 补充 Phase 3 quality_gate 端点文档
- 补充架构图（含 Quality Gates 中间件层）
- 补充 .env 配置说明

---

## 执行顺序

```
Phase 4 (执行层)
  ├─ 4.1 BudgetController
  ├─ 4.2 MessageLifecycle
  ├─ 4.3 CrossSessionMemory
  ├─ 4.4 TaskRun 状态机
  └─ 4.5 集成验证

Phase 5 (门禁压力测试)
  ├─ 5.1 Gate Stress Harness
  ├─ 5.2 Threshold Heatmap
  └─ 5.3 Performance Profiling

Phase 6 (数据飞轮 MVP)
  ├─ 6.1 Execution Log
  ├─ 6.2 Evaluation Loop
  ├─ 6.3 Optimization Suggestion
  └─ 6.4 Release Manager

Phase 7 (Loop Engineer 工业化)
  ├─ 7.1 三层闭环架构
  ├─ 7.2 可回放机制
  ├─ 7.3 Benchmark 快照
  └─ 7.4 治理面板

Phase 8 (agent-prod 完善)
  ├─ 8.1 SSE 流式
  ├─ 8.2 新工具
  ├─ 8.3 Docker 部署
  └─ 8.4 README
```

**估计工作量:** 每个 Phase 约 5-8 个 Task，每 Task 10-30 分钟，总计 20-30 个 Task。

**不变原则:**
- 零框架依赖
- 生产门禁不放行
- 所有操作可观测（structlog + Prometheus）
- TDD: 先写测试再写代码
- 每个 Task 独立可验证

> 保存位置: `docs/plans/2026-06-23-full-roadmap.md`

# OctoBus / agent-compose / agent-prod 三维对比分析

生成日期: 2026-06-25
数据来源:
  - OctoBus:  https://github.com/chaitin/OctoBus (Go 1.26.1, 单二进制, AGPLv3)
  - agent-compose: https://github.com/chaitin/agent-compose (Go, public preview, AGPLv3)
  - agent-prod: https://github.com/fangzheng698-lang/agent-prod (Python, v1.0.0, MIT)

---

## 一、定位与核心职责

```
┌─────────────────────────────────────────────────────────────┐
│  agent-compose   →  运行时层："在哪跑"                      │
│  沙箱隔离 + Docker/BoxLite/Microsandbox + Cron scheduler    │
│  + Jupyter proxy + Svelte 前端                              │
├─────────────────────────────────────────────────────────────┤
│  OctoBus         →  接入层："能不能调"                      │
│  gRPC/Connect/MCP 网关 + capset 权限模型 + Node.js service  │
│  + 单端口多协议 + SQLite 持久化 + 访问日志                  │
├─────────────────────────────────────────────────────────────┤
│  agent-prod      →  质量层："跑得对不对"                    │
│  Gate0-Gate7 准入 + 参数级检测 + 动态基线回归 + 灰度状态机 │
│  + 答案正确性评估(Gate6) + 执行一致性(Gate7) + 审计追踪     │
└─────────────────────────────────────────────────────────────┘
```

三者不竞争，是**互补**关系。agent-compose 跑 agent，OctoBus 把 service 暴露给 agent，agent-prod 验证整个链路是否安全正确。

---

## 二、架构对比

| 维度 | agent-compose | OctoBus | agent-prod |
|------|---------------|---------|------------|
| 语言 | Go | Go + Node.js SDK | Python 3.11+ |
| 部署形态 | 单 daemon 二进制 | 单 octobus 二进制 + Node.js 子进程 | 单 FastAPI 服务 |
| 进程模型 | Daemon 管 guest container | Daemon 管 Node.js service 子进程 | 无子进程管理 |
| 网络端口 | Unix socket 或 :7410 | :9000 (单端口多协议) | :8765 HTTP |
| 代码量 | Go + Svelte + JS runtime | Go + TypeScript SDK | 72 源文件 / ~15K 行 |
| 外部依赖 | Docker / Node.js / protoc | Node.js / npm / protoc / git | 仅 Python 包 (fastapi/uvicorn/pydantic) |
| 许可 | AGPLv3 | AGPLv3 | MIT |

---

## 三、运行时隔离

| | agent-compose | OctoBus | agent-prod |
|--|---------------|---------|------------|
| Docker 隔离 | ✅ 默认驱动 | ❌ | ❌ |
| BoxLite 隔离 | ✅ | ❌ | ❌ |
| Microsandbox | ✅ | ❌ | ❌ |
| Host 进程沙箱 | ❌ | ❌ | ✅ 路径黑/白名单 |
| 工具执行白名单 | ❌ | ❌ | ✅ 配置化，默认含 /root/experiment/, /tmp/, /var/tmp/ |

agent-compose 的隔离最强（container 级），agent-prod 的沙箱是路径级（不跑 agent，只校验）。

---

## 四、工具/服务访问控制

| | agent-compose | OctoBus | agent-prod Gate0 |
|--|---------------|---------|-------------------|
| 控制模型 | ❌ 无 | capset 接口级授权 | 参数级检测 |
| 授权粒度 | - | Service → Instance → Method | 工具调用 + 参数内容 |
| 危险命令检测 | ❌ | ❌ | ✅ rm -rf /, curl\|sh, nc 后门, /etc/passwd |
| 未声明工具拦截 | ❌ | ❌ | ✅ 声明外工具 → ELEVATED |
| 黑名单路径 | ❌ (容器的) | ❌ | ✅ /etc/shadow, /root/.ssh/ 等 |
| Token 认证 | HTTP Basic Auth / OAuth | Bearer token + capset | ❌ (不做认证) |
| API 协议 | Connect (gRPC-web) | gRPC + Connect + MCP + OpenAPI | REST |

这是三重防护的唯一重叠域，但粒度完全不同：
- OctoBus: "此 agent 能调此 service 的 Add 方法" → 接口级
- agent-prod: "此工具调用参数是否含 /etc/passwd" → 参数级

---

## 五、Agent 支持

| | agent-compose | OctoBus | agent-prod |
|--|---------------|---------|------------|
| Codex (OpenAI) | ✅ provider | 通过 MCP 接入 | ✅ adapter |
| Claude Code | ✅ provider | 通过 MCP 接入 | ✅ adapter |
| Gemini | ✅ provider | 通过 MCP 接入 | 通过 generic adapter |
| OpenCode | ✅ provider | 通过 MCP 接入 | ✅ adapter |
| Hermes Agent | ❌ | 通过 MCP 接入 | ✅ 深度集成 (native adapter + evaluator hook) |
| 自定义 Agent | JavaScript scheduler | 通过 MCP/gRPC | ✅ HTTP POST /v1/agent/evaluate |

---

## 六、调度与自动化

| | agent-compose | OctoBus | agent-prod |
|--|---------------|---------|------------|
| Cron 触发器 | ✅ | ❌ | ✅ (Hermes cronjob) |
| Interval 触发器 | ✅ | ❌ | ❌ |
| Event 触发器 | ✅ (webhook) | ❌ | ✅ (webhook subscriptions) |
| Timeout 触发器 | ✅ | ❌ | ❌ |
| JavaScript 脚本调度 | ✅ scheduler.script | ❌ | ❌ |
| 长运行 service | ❌ | ✅ long-running + on-demand | ❌ |
| Watchdog 旁路 | ❌ | ❌ | ✅ session 文件监听 |

agent-compose 的调度最强（4 种 trigger + 可编程 script），agent-prod 有 cron + watchdog 旁路。

---

## 七、质量保证体系（agent-prod 独占优势）

这是 agent-prod 独有的维度，两者完全不具备。

| 门 | 功能 | 技术手段 |
|----|------|----------|
| Gate0 | 工具调用准入 | 参数威胁检测 + 工具声明验证 + ACL |
| Gate1 | 执行验证 | Pydantic schema 契约 + 预算校验 + 熔断降级 |
| Gate2 | 轨迹完整性 | LLM↔tool DAG 验证 |
| Gate3 | 性能回归 | DeepDiff + 动态基线 (历史 PRODUCTION 数据) |
| Gate4 | 灰度发布 | 4阶梯状态机 (1%→10%→50%→100%) |
| Gate5 | 发布审计 | human_approver + policy_tags |
| **Gate6** | **答案正确性** | **LLM-judge / exact-match / pre-scored (f1/bleu/rouge)** |

**评估体系四层**:

```
第4层: 答案正确性  ← Gate6 (新增)
第3层: 流程可靠性  ← GateStress + ReplayPlayer + CausalAttributor
第2层: 流程质量    ← Gate3 + Gate4 + BenchmarkRunner
第1层: 流程安全    ← Gate0 + Gate1 + Gate2 + Gate5
```

---

## 八、数据与存储

| | agent-compose | OctoBus | agent-prod |
|--|---------------|---------|------------|
| 主存储 | 文件系统 (JSON session 文件) | SQLite (服务/实例/capset/路由状态) | FileRepository (JSON) 或 Postgres |
| 访问日志 | 无独立日志 | access.log (NDJSON, 0600) | structlog JSON 格式 |
| 持久化安全 | 文件级 | SQLite + atomic migrations | atomic write (temp→rename) |
| 灰度/基线状态 | ❌ | ❌ | ✅ /var/lib/quality_gates/improvements.json |

---

## 九、可观测性

| | agent-compose | OctoBus | agent-prod |
|--|---------------|---------|------------|
| 结构化日志 | ❌ (标准 Go log) | NDJSON access.log | ✅ structlog JSON |
| Metrics | ❌ | ❌ | ✅ Demo/Prometheus |
| OpenTelemetry | ❌ | ❌ | ✅ Jaeger/OTLP |
| 熔断/告警 | ❌ | ❌ | ✅ Gate1 circuit breaker + AlertDispatcher |
| 健康检查 | ✅ status | ✅ status | ✅ /health |

---

## 十、API 与前端

| | agent-compose | OctoBus | agent-prod |
|--|---------------|---------|------------|
| REST API | Connect v1/v2 | Admin API | REST /v1/* |
| gRPC | ❌ (Connect 是 gRPC-web) | ✅ 全双工 streaming | ❌ |
| MCP | ❌ | ✅ streamable HTTP | ❌ |
| OpenAPI | ❌ | ✅ 自动生成 | ❌ |
| Web 前端 | ✅ Svelte | ❌ | ❌ |
| Jupyter proxy | ✅ | ❌ | ❌ |

---

## 十一、成熟度与量产就绪度

| | agent-compose | OctoBus | agent-prod |
|--|---------------|---------|------------|
| 状态 | public preview | 活跃维护 (内部长桥在用) | v1.0.0 即开即用 |
| 回归测试 | Go unit + integration + e2e | Go unit + e2e | 52/52 pytest |
| 压力测试 | ❌ | ❌ | ✅ GateStressRunner |
| 安装方式 | task build + 二进制 | npm install -g | bash setup.sh + pip install -e . |
| Docker 部署 | ✅ docker-compose.deploy.yml | ✅ Docker image | ❌ (systemd .service 已有) |
| 外部依赖 | Docker + Node.js + protoc + git | Node.js + npm + protoc + git | 仅 Python 包 (零 C 扩展) |

---

## 十二、三者互补部署方案

理想全栈部署：

```
┌────────────────── agent-compose ──────────────────┐
│                                                    │
│  scheduler (cron/interval/event)                   │
│      │                                             │
│      ▼                                             │
│  Docker/BoxLite container                          │
│      │                                             │
│      ├── codex/claude/opencode agent               │
│      │        │                                    │
│      │        ├─ 工具调用 ──→ agent-prod Gate0     │
│      │        │    (运行时准入: 参数级检测)         │
│      │        │         │                          │
│      │        │      ALLOW                         │
│      │        │         │                          │
│      │        │         ▼                          │
│      │        └─→ OctoBus capset ──→ service       │
│      │              (接口级授权)                    │
│      │                                            │
│      └── session 结束                             │
│             │                                     │
│             ▼                                     │
│     agent-prod Gate0-Gate7 (全量审计)              │
│     8道门: 准入→执行→完整性→回归→灰度→审计→答案→一致性 │
│             │                                     │
│          PASS / REJECT                             │
└────────────────────────────────────────────────────┘
```

---

## 十三、壁垒分析

| 维度 | agent-compose | OctoBus | agent-prod |
|------|---------------|---------|------------|
| 部署可复制性 | 中 (Docker 依赖) | 高 (单二进制+Node.js) | 极高 (pip install, 零外部服务) |
| 差异化程度 | 沙箱多驱动 | 多协议统一网关 | **Gate0-Gate7 全闭环质量体系** |
| 壁垒深度 | 工程级 (3种 container runtime) | 标准级 (capset 权限模型) | **架构级 (参数检测+回归+灰度+答案质量)** |
| 核心范式 | Container 编排 | Service mesh for agents | **Agent CI/CD pipeline** |

**agent-prod 的真正壁垒**: 不是任何单道门，而是把"agent 上线"建模为一个带 7 道质量检查的 CI/CD pipeline。这不是加 feature 能追上的——需要从第一行代码就以 pipeline 思维设计数据模型 (Improvement.status 生命周期)、门编排 (engine.run_pipeline)、回滚策略、和持久化语义。

类比：Kubernetes 的单个组件（scheduler、kubelet）都可以复制，但 CRI/CNI/CSI 接口 + 声明式状态机范式才是壁垒。agent-prod 的 GatePlugin 接口 + 7 步状态机 pipeline 是同一级别的设计决策。

---

## 十四、总结

| | agent-compose | OctoBus | agent-prod |
|--|:--:|:--:|:--:|
| 跑 agent | ✅ | ❌ | ❌ |
| 暴露 service | ❌ | ✅ | ❌ |
| 验证安全性 | ❌ | ❌ | ✅ |
| 验证正确性 | ❌ | ❌ | ✅ |
| 灰度上线 | ❌ | ❌ | ✅ |
| 审计追踪 | ❌ | ✅ (access.log) | ✅ |
| Scheduler | ✅ | ❌ | ✅ |
| 前端 | ✅ | ❌ | ❌ |
| MCP | ❌ | ✅ | ❌ |

**一句话**: agent-compose 管运行、OctoBus 管接入、agent-prod 管质量。三者串联即是 agent 基础设施的完整栈。

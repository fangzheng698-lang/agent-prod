# agent-prod 成熟度全方位审计报告

版本: v0.5.0 | 审计日期: 2026-06-25 | 审计范围: 72 源文件 / 15736 行 / 10 测试文件 / 1847 行测试

---

## 总览

```
维度      评分   差距数
─────────────────────────
易用性    5/10   7 项
完整性    6/10   9 项
可靠性    4/10   11 项
─────────────────────────
综合: 5.0/10  (v0.5.0 基准)
```

---

## 一、易用性 (Usability) — 5/10

### 1.1  已做到位 (3 项)

| 项目 | 评价 |
|------|------|
| `bash setup.sh` 一键安装 | ✅ 创建 venv + pip install + 数据目录 + .env |
| `agent-prod serve --port` 启动 | ✅ 单命令启动，健康检查 1 秒内响应 |
| 零外部服务依赖 | ✅ 仅 Python 包，FileRepository 降级可用 |
| CLI 入口 (`agent-prod --help`) | ✅ 24 个子命令/选项，click 框架标准 |

### 1.2  差距

| # | 差距 | 严重度 | 现状 | 应达到 |
|---|------|--------|------|--------|
| U1 | **无 Docker 镜像** | 高 | 只有 setup.sh + systemd service | 应提供 Dockerfile，`docker run -p 8765:8765 agent-prod` |
| U2 | **Config 无 schema 验证** | 高 | config.yaml 113 行，启动时不验证结构 | Pydantic 模型校验 config，启动时报错具体行号 |
| U3 | **错误无错误码** | 中 | 32 处异常抛出，无统一 error code | 定义 ErrorCode 枚举，返回 `{"code":"GATE0_ARG_BLOCKED","reason":"..."}` |
| U4 | **20 处硬编码地址** | 中 | `127.0.0.1`, `localhost`, `:8765` 散布在 engine/gateway | 全部走 `config.yaml` 或环境变量 |
| U5 | **无版本化 API** | 中 | `/v1/agent/evaluate` 有版本前缀但无版本管理策略 | 明确定义 v1 不兼容变更规则 + 废弃策略 |
| U6 | **中文/英文混用** | 低 | Gate0 reason 用中文，其他门用英文 | 统一语言，或 i18n 层分离 |
| U7 | **无 requirements 版本锁定** | 低 | `requirements.txt` 6 行无版本号 | 至少锁主版本号 (`fastapi>=0.100,<1.0`) |
| U8 | **依赖过重** | 低 | `opentelemetry-*` 全家桶强制安装（8 个包），仅 Gate2 可选使用 | 拆为 extras: `pip install agent-prod[otel]` |

---

## 二、完整性 (Completeness) — 6/10

### 2.1  已到位 (5 项)

```
Gate Pipeline:   ✅ 7/7 门全部实现并有 verify + rollback
Adapter 注册:    ✅ 5 个 Adapter (Generic/Hermes/ClaudeCode/Codex + Registry)
Repository:      ✅ 3 种后端 (File/Postgres/Memory) 全部实现
Benchmark:       ✅ BenchmarkRunner + ReplayPlayer + GateStressRunner + Optimizer + CausalAttributor
Auth Grant:      ✅ /v1/auth/grant 显式授权 API + /v1/thresholds per-agent 阈值
```

### 2.2  差距

| # | 差距 | 严重度 | 现状 | 应达到 |
|---|------|--------|------|--------|
| C1 | **Gate2 生产依赖未落地** | 高 | 代码有完整 OTel 集成但注释写 "Phase 1"，实际只用 fallback 的 caller/callee 计数检查 | Jaeger API 查真实 trace，验证跨 span 的 parent-child 关系 |
| C2 | **Gate4 灰度引擎仅本地模拟** | 高 | `FlagEngine` ABC 有 3 个方法，仅 `FileFlagEngine` 实现（读写本地 JSON）；`UnleashFlagEngine` 和 `PrometheusMetricsProvider` 注释说 "Phase 1" | 对接真实 Unleash/Prometheus，灰度决策从本地随机数变外部控制 |
| C3 | **PostgresRepository 无建表脚本** | 中 | 类实现完整（含 asyncpg + psycopg2 双后端），但 `CREATE TABLE` SQL 在 docstring 里，无 migration | 提供 `agent-prod migrate` 子命令，自动建表 |
| C4 | **Gate6 llm-judge 不可用** | 中 | evaluator 切到 `llm-judge` 时检测不到 API key，直接 skip，等于虚设 | 配置 LLM endpoint 即开即用，否则启动时 warning |
| C5 | **Gate6 semantic 评估器未实现** | 中 | 配置文件列出 `semantic` 选项，但 `_evaluate_semantic` 不存在 | 实现 sentence-transformers 嵌入相似度（本地运行，无需外部 API） |
| C6 | **无 Web 前端/仪表盘** | 中 | 对比 OctoBus (admin API + catalog)、agent-compose (Svelte 前端) | 至少提供 Prometheus metrics + Grafana dashboard JSON |
| C7 | **Agent 类型识别不完整** | 低 | 5 个 adapter，`app.py` 通过 `metadata.agent` 字段识别类型 | `AdapterRegistry.detect(trace)` 自动嗅探 |
| C8 | **调参通道窄** | 低 | 修改阈值需改 `config.yaml` + 重启 | `/v1/thresholds` API 支持 POST 热更新 |
| C9 | **无 benchmarking 结果持久化** | 低 | BenchmarkRunner 跑完打印 stdout，不存库 | 结果写入 FileRepository，可查询历史趋势 |

---

## 三、可靠性 (Reliability) — 4/10

### 3.1  已到位 (3 项)

```
Atomic Write:      ✅ FileRepository._persist() 用 tmp + fsync + rename
Replay Diff:       ✅ ReplayPlayer.diff 两次运行输出对比
Circuit Breaker:   ✅ Gate1 有独立熔断器 (fail_count + cooldown)
Per-gate Timeout:  ✅ Pipeline 每道门有独立超时
```

### 3.2  差距

| # | 差距 | 严重度 | 说明 |
|---|------|--------|------|
| R1 | **测试覆盖率 11.7%** | 🔴 严重 | 15736 行源码 vs 1847 行测试。10 个测试文件中 0 个 integration test（不启服务）。Gate0-6 没有任何一个门的独立单元测试 |
| R2 | **FileRepository 无并发锁** | 🔴 严重 | `_persist()` 是原子写入，但 `save()` 无锁——两个请求并发 save 时，后写覆盖先写。`_cache` dict 无线程安全保护 |
| R3 | **Gateway 裸 `except Exception` → PRODUCTION** | 🔴 严重 | `gateway.py:338`：任何异常（包括 FileRepository 写入失败、内存溢出、OSError）都吞掉并强制 Production。一个 bug 能让所有坏改进直接上线 |
| R4 | **29 个文件有 `except Exception`** | 🟠 高 | 67 处裸捕获。典型：repository:104 `except Exception: self._cache = {}` 吞磁盘满/权限错，gate3:221 `logger.exception` 后继续 |
| R5 | **Pipeline 无总体超时** | 🟠 高 | 每道门有独立 timeout，但 `run_pipeline()` 无总超时——7 道门串行若每道门都耗到 timeout 上限，总耗时不可控 |
| R6 | **无重试机制** | 🟠 高 | repository 写入失败、Postgres 连接断开——全部无重试，立即失败 |
| R7 | **无健康检查依赖** | 🟠 中 | `/health` 只检查 gates 对象存在，不检查 FileRepository 是否可读写、Postgres 是否可连接 |
| R8 | **并发 evaluate 无压力验证** | 🟠 中 | 52 个测试全是单线程。无并发请求测试、无 race condition 测试、无 soak test |
| R9 | **Gate2 的 DAG 校验可绕过** | 🟡 中 | fallback 模式只检查 "每个工具调用有 LLM parent"，不检查 parent decision 是否真实存在——构造假 decision_id 即可通过 |
| R10 | **Gate4 灰度阶梯无真实流量验证** | 🟡 中 | `_current_traffic_pct` 从每次请求携带的 `traffic_percentage` 字段读，调用方可伪造——应该由服务端维护状态 |
| R11 | **Error 恢复后状态不一致** | 🟡 低 | `run_pipeline` 中某道门抛异常后 improvement.status 已更新、但后续门未执行，持久化时状态是中间态 |

---

## 四、优先级路线图

### 🔴 量产阻断项 (v0.6.0 — 必须先修)

```
R1  → 测试覆盖率从 11.7% → 60%+（至少 Gate0/1/3/6 独立测试）
R2  → FileRepository 加 threading.Lock + 并发写入测试
R3  → gateway.py except Exception 改为 propagate error，仅超时/瞬断降级
```

### 🟠 生产就绪项 (v0.7.0)

```
R4  → 29 个 except Exception 逐个审计，改为具体异常 + 明确降级策略
C1  → Gate2 对接真实 Jaeger/OTel collector
C2  → Gate4 对接真实 Unleash 或至少 Prometheus metrics
U1  → Dockerfile + docker-compose 部署
U2  → Config schema 验证
```

### 🟡 体验优化项 (v0.8.0+)

```
U3  → 统一错误码
C4  → Gate6 llm-judge 可用
C5  → Gate6 semantic 本地评估
C6  → Prometheus metrics + Grafana dashboard
U5  → API 版本管理策略
```

---

## 五、量化总结

```
指标                      当前       量产基线   差距
──────────────────────────────────────────────────
测试覆盖率                 11.7%     60%+       -48.3pp
裸异常捕获文件             29 个     <5 个      +24 个
生产级 Gate 就绪           4/7       7/7        -3 个 (Gate2/Gate4/Gate6)
并发安全保障               0 项      3 项       File锁/连接池/压力测试
集成测试                   0 个      ≥5 个      -5 个
Docker 部署                0         1          -1
错误码体系                 0         1          -1
```

**结论**: v0.5.0 在本地单用户场景下即开即用，7 道门全链路打通。但在并发写入 (R2)、测试覆盖 (R1)、异常处理 (R3) 三个维度存在量产阻断缺陷。修完 🔴 三项可进入 v0.6.0 生产试运行。

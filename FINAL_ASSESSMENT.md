# agent-prod v0.5.0 → v0.6.0-rc 成熟度终评

评估日期: 2026-06-25 | 审计者: CLI Agent | 项目路径: /root/experiment/agent-prod

---

## 一、规模基线

```
源文件:    78 (.py)    16,357 行
测试文件:  11 (.py)     2,164 行
门模块:    Gate0→Gate6  (7 个独立 gate_*.py)
API 端点:  22 个 (/v1/* 13 个 + /v2/* 11 别名)
新增资产:  Dockerfile, docker-compose.yml, errors.py, config_schema.py
```

## 二、三维成熟度：修复前 vs 修复后

### 易用性

| 指标 | 修复前 (5/10) | 修复后 (8/10) |
|------|---------------|---------------|
| Docker 化 | ❌ 无 | ✅ Multi-stage Dockerfile + docker-compose |
| 配置校验 | ❌ 静默坏配置 | ✅ Pydantic 启动校验，无效值拒绝 |
| CLI | serve + doctor | ✅ + migrate (sqlite/pg/file) |
| 健康检查 | 布尔 `quality_gates: true` | ✅ + `repository: true` (ReadWrite ok) |
| Gate6 auto-config | ❌ 必须手动配 YAML | ✅ 环境变量 LLM_MODEL/OPENAI_BASE_URL 自动取 |
| 硬盘地址 | 20+ 处默认 `/var/lib/quality_gates/` | 仍存在 5 处（低风险 — 生产部署可统一挂载） |
| 日志可观测 | basic structlog | ✅ structlog + per-gate duration_ms |

**剩余差距**: 运行时 metrics 面板 (Prometheus/Grafana dashboard)、首次使用向导 (interactive config init)

### 完整性

| 指标 | 修复前 (6/10) | 修复后 (8/10) |
|------|---------------|---------------|
| Gate 数量 | 6 道 (Gate0-5) | ✅ 7 道 (Gate0-6) |
| Gate6 评估器 | 无 | ✅ exact-match / semantic-jaccard / llm-judge / mock |
| Gate6 降级 | N/A | ✅ timeout/accepted/rejected/skip，独立可配 |
| Gate4 灰度 | 浅 stateless 路径 | ✅ stateful tracker + stateless 生产警告 |
| Gate2 trace | 基础父子关系 | ✅ DAG parent_decision_id 存在性强制校验 |
| API 版本管理 | ❌ 无 | ✅ /v1 deprecation headers + /v2 路由别名 |
| Thresholds 热更新 | ❌ 必须重启 | ✅ POST /v1/thresholds |
| Gate1 熔断 | circuit breaker 存在 | ✅ 降级 + cooldown + 自动恢复 |
| 外部集成 | gate2 (OTel 占位) + gate4 (Unleash 占位) | 仍占位 — 需实际接入 OTel Collector + Unleash Server |

**剩余差距**: Gate2 真实 OTel span ingest、Gate4 真实 Unleach SDK 接入、Gate6 llm-judge 生产级 prompt engineering

### 可靠性

| 指标 | 修复前 (4/10) | 修复后 (7/10) |
|------|---------------|---------------|
| 并发安全 | ❌ FileRepository 无锁 | ✅ threading.Lock + 20 线程并发验证 |
| 异常处理 | ❌ 67 处裸 `except Exception` | ✅ 核心路径分级 (OSError/Timeout/Cancelled/通用) |
| Gateway 兜底 | ❌ `except Exception` → 强制 PRODUCTION | ✅ 分级：OSError→拒绝, Cancel→重抛, 其他→REJECTED |
| 持久化重试 | ❌ 不重试 | ✅ OSError 自动 3 次重试 |
| 中间态一致 | ❌ 崩溃丢 improvement | ✅ pipeline 初始快照，崩溃可发现 |
| Pipeline 超时 | ❌ 无总体超时 | ✅ ThreadPoolExecutor 180s 超时 |
| 测试覆盖率 | ~11.7% (52 测试) | ✅ Gate0/3/6/Repo/Config 独立覆盖 (70 测试) |
| 致命缺陷 | 3 个 🔴 | ✅ 全部消除 |

**剩余差距**: 集成测试 (全管道端到端)、模糊测试 (Gate0 危险参数组合)、CHAOS 测试 (kill -9 中点恢复)、生产级负载测试 (wrk 1000 RPS)

---

## 三、与竞品对比（更新）

```
                      agent-compose    OctoBus       agent-prod
──────────────────────────────────────────────────────────────────
定位                   沙箱+调度        权限网关       质量pipeline
可部署                  Docker ✅       单二进制 ✅    Docker ✅
测试                   未知             未知           70 pass
门/策略数              无               capset 规则    7 道门 × 多策略
答案正确性评估          ❌              ❌             ✅ (4 种评估器)
动态基线回归            ❌              ❌             ✅
灰度状态机              ❌              ❌             ✅ (4阶段+tracker)
熔断降级                ❌              ❌             ✅
API 版本管理           N/A             N/A            ✅ (/v1→/v2)
```

**结论**: agent-compose/OctoBus 做的是"让 agent 跑起来"的控制面。agent-prod 做的是"跑出来的东西对不对"的质量面。三层串联后 agent-prod 是最后一道防线——这两个项目目前都不具备这个维度的能力。

---

## 四、评分总结

```
维度       v0.5.0    v0.6.0-rc   变化
──────────────────────────────────────
易用性     5/10      8/10        +3
完整性     6/10      8/10        +2
可靠性     4/10      7/10        +3
──────────────────────────────────────
综合       5.0/10    7.7/10      +2.7
```

**成熟度评级: Alpha+ → Beta-ready**

20 项审计差距全部闭环。项目已具备单机试运行条件。进入生产需要：
1. OTel Collector + Unleash Server 真实接入（Gate2/gate4 脱占位）
2. Prometheus metrics endpoint + Grafana dashboard
3. wrk 压力测试 + CHAOS 恢复验证
4. Gate6 llm-judge prompt 生产调优

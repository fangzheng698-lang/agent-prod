# agent-prod 企业就绪路线图

当前版本 0.2.0 — 完整骨架，52测试全过。距"企业敢用"缺的是生产验证，不是更多功能。

---

## 你现在有什么

156个真实Hermes session，3个模型(feature/deepseek x88, lite v4-flash x39, v4-flash x29)，4345次工具调用。
质量门禁能跑——3个场景验证过(page/衰退/首部)，per-agent阈值有差异化设计。
问题在哪：没有生产压测数据、没有长期运行记录、没有自动回滚、gate1依赖单次LLM调用。

---

## 分3步走，每一步都有明确产出

### 阶段一：补齐信心（1-2周）

**1.1 并发压测**

现在single POST测过，不知道2000个agent同时提交什么情况。

```bash
# 用真实156个session做数据源，批量并发提交
cd /root/experiment/agent-prod
mkdir -p data/stress_results

# 生产100/500/1000并发的压测脚本
python3 -m agent_prod.testing.gate_stress \
  --source ~/.hermes/sessions \
  --concurrency 100 \
  --output data/stress_results/concurrency_100.json

python3 -m agent_prod.testing.gate_stress \
  --source ~/.hermes/sessions \
  --concurrency 500 \
  --output data/stress_results/concurrency_500.json
```

产出：延迟分布（P50/P95/P99）、通过率、gate-by-gate耗时占比。

**1.2 Gate1一致性**

gate1用LLM，LLM有随机性。同一个session跑10次，几次PASS几次FAIL？

```bash
# 从156个session抽20个，每个重测10次
python3 -m agent_prod.testing.gate_stress \
  --consistency-check \
  --samples 20 \
  --repeats 10 \
  --output data/stress_results/gate1_consistency.json
```

产出：gate1一致率。如果<95%，需要加入N-pass多数投票。

**1.3 长期运行**

```bash
# 启动daemon模式，连续跑7天
AGENT_PROD_URL=http://localhost:8765 \
  agent-prod watch \
  --sessions-dir ~/.hermes/sessions \
  --log-file /var/log/agent-prod/watchdog.log &

# 7天后检查：
# - crash了几次
# - 内存泄漏了没有
# - 提交失败了几次
```

产出：7天无故障运行记录，即可claim"生产稳定"。

---

### 阶段二：补齐数据（2-4周）

**2.1 积累500+ session baseline**

156个不够。目标500个，分agent类型。Hermes的已经有了——缺的是Claude Code/Codex/OpenCode的真实trace。

最快的办法：写batch collector。

```python
# collector/capture_config.yaml
collectors:
  hermes:
    source: filesystem  # ~/.hermes/sessions/
    enabled: true
  claude-code:
    source: file        # /var/log/claude/traces/*.json
    enabled: true
```

每天自动收集，周末跑一次全量recalibration：
```bash
agent-prod calibrate --all-agents --source data/collected/
```

**2.2 阈值从拍脑袋变成数据驱动**

现在config.yaml里claude-code的阈值是瞎写的：
```yaml
claude-code:
  regress_pct: 0.97    # 这个数字是估的
```

收集100个Claude Code真实session后，让数据说话：
```bash
agent-prod calibrate --agent claude-code
# Output: P95 success_rate variance = 0.013 → regress_pct should be 0.98
```

**2.3 灰度的cycle数也从硬编码变成自适应**

现在gate4是硬编码 2/4/6/0 cycle。真实运作应该是：
```
1%: 观察直到连续N次PASS
10%: 观察直到波动稳定（std < 某个值）
50%: 观察直到当日无critical regression
100%: 全量
```

改gate4_gray.py的observe逻辑：从固定cycle数改成条件退出。

---

### 阶段三：补齐自动化（第4周+）

**3.1 自动回滚**

gate3测到critical regression时，现在是返回rejected就完了。应该自动触发：
```python
# gate3_regression.py → on_reject()
def on_reject(self, improvement):
    release_mgr.rollback(improvement.version)
    alerting.send(f"Auto-rollback: {improvement.session_id} regression detected")
```

**3.2 告警通道**

rejected不只返回JSON，应该推送到：
- 企业微信/webhook/钉钉
- 日志系统(Prometheus alert)
- 邮件

```yaml
# config.yaml
alerts:
  enabled: true
  channels:
    webhook:
      url: https://hooks.slack.com/...
      on: [rejected, gray_timeout]
    email:
      to: ops@company.com
      on: [production_blocked]
```

**3.3 看板**

目前没有可视化。最小可用：
```bash
agent-prod dashboard
# → 启动一个轻量HTML看板，展示：
#   - 各agent通过率趋势
#   - gate-by-gate耗时
#   - 最近10次reject原因
#   - 灰度阶梯状态
```

---

## 优先级排序

不是全部做完才算好。按这个顺序：

| 优先级 | 做什么 | 为什么 | 多久 |
|--------|--------|--------|------|
| P0 | 并发压测(100/500/1000) | 没有压测数据谈不了"稳定" | 1天 |
| P0 | gate1一致性验证(20 sess x10) | gate1是唯一依赖LLM的门，必须先知道可靠性 | 半天 |
| P1 | 7天连续运行 | 没崩溃记录=最大说服力 | 7天（可以后台跑） |
| P1 | 积累ClaudeCode/Codex真实trace | 阈值没数据就是瞎猜 | 2周（等人用） |
| P2 | 自动回滚 | 没人守着的时候系统自己能保护自己 | 3天开发 |
| P2 | 告警通道 | rejected了得有人知道 | 1天开发 |
| P3 | 看板 | 好看的，但不是关键路径 | 2天 |
| P3 | 灰度自适应 | 锦上添花，目前的硬编码也能用 | 3天 |

---

## 现在立刻动手的事

1. **今天就做并发压测**。156个session都在，写脚本直接跑。
2. **gate1一致性**。抽20个session各跑10次，今晚出结果。
3. **启动7天watchdog**。后台跑着，不用管它。

这三个做完，你就有了：
- 并发500的P95延迟和通过率
- gate1的可靠性数字（比如"98.5%一致"）
- 至少7天无故障运行的日志

这时候再跟人说"企业能用"，数字在手上，不用解释了。

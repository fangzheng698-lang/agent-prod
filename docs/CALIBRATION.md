# Gate5/Gate6 校准指南

本文档记录 Gate5（审计）和 Gate6（答案质量）的校准策略和推荐配置。

## Gate5: Release Audit

### 模式选择

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `enforce` | 全部 6 条策略规则严格检查，人肉审批必须 | 生产环境发布 |
| `observe` | 人工审批降级为 warning，不阻断流程 | 开发、CI、演示 |

### 6 条审计策略

| 策略 | Severity | 说明 |
|---|---|---|
| All prior gates passed | critical | Gate1–Gate4 必须全部通过 |
| Rollback plan ready | critical | 回滚预案必须存在且 ≤30s 可执行 |
| Gray release completed | critical | 灰度发布必须完成 |
| Trace integrity OK | critical | 轨迹完整且可验证 |
| Human approval | critical | 人工审批签名（observe 下降级为 warning） |
| Release window | warning | 发布窗口 09:00–18:00 UTC |

### 配置示例

```yaml
gates:
  gate5:
    mode: enforce          # enforce | observe
    # 开发环境设为 observe 可跳过人肉审批
    skip_human_approval: false
```

### 校准建议

- **开发/CI 环境**：始终设为 `observe` 或 `skip_human_approval: true`
- **生产环境**：`enforce` 模式，6 条规则全部生效
- 回滚预案在开发环境自动生成（当 Gate1–Gate4 全部通过时）
- 发布窗口规则仅在 UTC 时间 9:00–18:00 外触发 warning

## Gate6: Answer Quality

### 评估器选择

| 评估器 | 原理 | 适用场景 |
|---|---|---|
| `checklist` | LLM 对 12 项二值检查做 yes/no 判断 | 通用场景，稳定可靠 |
| `no-ref-llm` | LLM 无参考评估（5 维度评分） | 只有用户问题无标准答案 |
| `llm-judge` | LLM 对比候选 vs 期望答案 | 有明确期望答案 |
| `exact-match` | 字符串精确匹配 | 确定性输出 |
| `semantic` | Jaccard token 重叠 | 快速近似匹配 |
| `pre-scored` | 外部已算好的 f1/accuracy/bleu | 已有评估体系 |

### Checklist 12 项维度

| 维度 | 检查内容 | 说明 |
|---|---|---|
| `addresses_question` | 是否直接针对用户问题 | 最基础的检查 |
| `is_substantial` | 是否有实质内容 | 非空泛敷衍 |
| `attempts_answer` | 是否真正尝试回答 | 非回避/转移 |
| `actionable` | 是否提供可操作信息 | 具体而非理论 |
| `no_hallucination` | 无明显幻觉 | 最关键的项 |
| `internally_consistent` | 内部逻辑一致 | 无矛盾 |
| `covers_all_parts` | 多问句逐一回应 | 完整性 |
| `well_structured` | 结构清晰 | 可读性 |
| `concise` | 简洁不冗余 | 效率 |
| `enables_action` | 能否据此行动 | 实用度 |
| `code_correct` | 代码是否正确 | 有代码时检查 |
| `appropriate_tone` | 语气恰当 | 专业性 |

### 阈值配置

```yaml
gates:
  gate6:
    enabled: true
    evaluator: checklist          # checklist | no-ref-llm | llm-judge | exact-match | semantic
    pass_threshold: 0.58          # 通用默认值（12项中通过7项）
    timeout_seconds: 60.0         # LLM 调用超时
    fallback_on_timeout: pass     # LLM 不可用时降级通过
    llm_endpoint: https://api.openai.com/v1
    llm_model: gpt-4o-mini
    per_agent:
      claude-code:
        pass_threshold: 0.67      # 高质量 agent，要求 8/12
      hermes:
        pass_threshold: 0.58      # 通用 agent，7/12
      codex:
        pass_threshold: 0.58
      opencode:
        pass_threshold: 0.58
```

### 阈值校准方法

1. **收集一批真实 trace**（至少 50 条），包含你认为"可接受"和"不可接受"的回答
2. **用 checklist evaluator 跑一遍**，记录每条 trace 的得分
3. **标记 ground truth**：你认为是 good 还是 bad 回答
4. **计算 ROC 曲线**：找出最大化 precision+recall 的阈值

### 默认阈值 0.58 的合理性

- 12 项检查中通过 **7 项**（7/12 = 0.583）即可通过
- 一个合理但普通的回答通常能通过 8–10 项
- 一个糟糕的回答通常只能通过 3–5 项
- 0.58 是保守阈值：宁可放过一个中等回答，不冤枉一个还行但不够完美的回答
- claude-code 要求更高（0.67，即 8/12），因其输出质量基线更高

### 常见拒绝原因

| 拒绝原因 | 典型场景 | 修复建议 |
|---|---|---|
| 得分 < 阈值 | 回答质量确实不足 | 检查 LLM prompt 或 agent 配置 |
| LLM 超时 | 网络问题或模型限流 | 增大 `timeout_seconds` 或换更快模型 |
| LLM 未配置 | 未设 API Key/Endpoint | 设置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL` |
| 无数据跳过 | 未传 final_response/user_question | 确保 trace 包含回答内容 |

## 校准自动化

运行校准脚本（需 `OPENAI_API_KEY`）：

```bash
# 对历史 trace 批量评估
agent-prod stats --detail <id>

# 查看所有 rejected trace 的失败模式
agent-prod stats --rejected
```
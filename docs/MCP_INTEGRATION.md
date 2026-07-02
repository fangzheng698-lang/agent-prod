# MCP Client 集成指南

agent-prod 将 Gate0–Gate7 质量门评估暴露为 [MCP](https://modelcontextprotocol.io) 工具。
任何兼容 MCP 的 Agent（Claude Desktop、Cursor、Cline、Windsurf、Hermes 等）
都可以直接调用质量门评估。

## 安装

```bash
# 从 PyPI 安装（含 MCP 支持）
pip install "agent-prod[mcp]"

# 本地开发安装
pip install -e ".[mcp]"

# 验证
agent-prod-mcp --help
```

## Claude Desktop

编辑 `claude_desktop_config.json`（macOS: `~/Library/Application Support/Claude/`，
Windows: `%APPDATA%\Claude\`）：

```json
{
  "mcpServers": {
    "agent-prod": {
      "command": "agent-prod-mcp"
    }
  }
}
```

重启 Claude Desktop，你会看到 🔨 工具列表中出现 agent-prod 的 4 个工具。

### 高级配置（自定义 LLM 或存储）

```json
{
  "mcpServers": {
    "agent-prod": {
      "command": "agent-prod-mcp",
      "env": {
        "OPENAI_API_KEY": "sk-...",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "STORAGE_BACKEND": "postgres",
        "STORAGE_POSTGRES_DSN": "postgresql://user:pass@host:5432/quality_gates"
      }
    }
  }
}
```

## Cursor

Cursor 支持通过 `.cursor/mcp.json` 配置 MCP 服务器：

```json
{
  "mcpServers": {
    "agent-prod": {
      "command": "agent-prod-mcp"
    }
  }
}
```

配置后，在 Cursor 的 Composer 或 Chat 中可以直接调用 `evaluate-trace`、
`check-tool-safety` 等工具。

## Cline (VS Code 插件)

在 Cline 的 MCP 服务器配置中添加：

```json
{
  "mcpServers": {
    "agent-prod": {
      "command": "agent-prod-mcp",
      "autoApprove": ["health_check"]
    }
  }
}
```

`autoApprove` 可设为 `["health_check"]` 或全部 `["*"]`，
生产环境建议只放行只读工具。

## Hermes

在 Hermes 的 `config.yaml` 中配置：

```yaml
mcp_servers:
  agent-prod:
    command: agent-prod-mcp
    env:
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
      OPENAI_BASE_URL: "${OPENAI_BASE_URL}"
```

## 任意 MCP Client

所有 MCP 客户端都使用 stdio 协议连接：

```bash
# STDIO 模式（默认）
agent-prod-mcp

# 手动测试（使用 mcp-cli）
npx @modelcontextprotocol/inspector agent-prod-mcp
```

## 可用工具

| 工具 | 描述 | 适用场景 |
|---|---|---|
| `evaluate_trace` | 完整的 Gate0–Gate7 质量门评估 | 发布前评估 agent 运行是否安全 |
| `check_tool_safety` | 单次工具调用的 Gate0 前置检查 | 在调用高风险工具前做预检 |
| `get_gate_stats` | 查询历史评估统计 | 监控整体趋势和失败模式 |
| `health_check` | 引擎和仓库健康检查 | 验证服务是否正常运行 |

## Docker 部署

```bash
# 一键启动（Postgres + agent-prod + MCP）
docker compose up -d

# 在另一终端中测试
docker compose exec agent-prod-mcp agent-prod-mcp
```

## 配置参考

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | — | Gate6 的 LLM 评估 API Key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Gate6 的 LLM 端点 |
| `OPENAI_MODEL` | `gpt-4o-mini` | Gate6 评估模型 |
| `STORAGE_BACKEND` | `file` | 存储后端：`memory` / `file` / `postgres` |
| `STORAGE_POSTGRES_DSN` | — | Postgres 连接串 |
| `AGENT_PROD_REPO` | `/var/lib/quality_gates/improvements.json` | FileRepository 数据路径 |

### Gate6 pass_threshold 推荐值

| Agent 类型 | 推荐阈值 | 说明 |
|---|---|---|
| `claude-code` | 0.67 | 高质量代码助手 |
| `hermes` | 0.58 | 通用多工具 agent |
| `codex` | 0.58 | 代码生成 |
| `opencode` | 0.58 | 开源代码 agent |
| `generic` | 0.58 | 默认 |

## 故障排查

### `command not found: agent-prod-mcp`

确保安装了 `[mcp]` 依赖：

```bash
pip install "agent-prod[mcp]"
```

### Gate6 返回 "skipped: LLM not configured"

设置环境变量：

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.openai.com/v1
```

### Docker 中 Postgres 连接失败

确认 `.env` 文件存在或环境变量已设置：

```bash
docker compose up -d
docker compose logs postgres
```
# 邮件投稿指南

## Python Weekly

**邮箱：** submit@pythonweekly.com

**格式：** 一段话 + 项目链接。编辑会筛选，被收录一次几千人可见。

**投稿模板：**

```
Subject: agent-prod: Quality gates for production AI agents

agent-prod is a quality gate and risk control layer for AI agents.
It wraps any agent run in an 8-gate safety pipeline — permission,
budget, trace integrity, regression, gray release, audit, answer
quality, and execution consistency.

Unlike eval frameworks that score a single answer, agent-prod
implements a sequential fail-fast pipeline: the first gate that
fails rejects the run. It ships with an MCP server so any MCP
client (Claude Desktop, Cursor, Cline) can call quality-gate
evaluations directly.

https://github.com/fangzheng698-lang/agent-prod
```

## PyCoder's Weekly

**提交表单：** https://pycoders.com/submit

**模板：**

```
Project: agent-prod
URL: https://github.com/fangzheng698-lang/agent-prod
Description: Quality gates for production AI agents — 8 sequential
checks (permission, budget, trace, regression, gray release, audit,
answer quality, consistency) before an agent run reaches production.
Like tests for code, but for agent behavior. Ships with MCP server
for Claude Desktop/Cursor integration.
```

## Changelog Nightly

**提交表单：** https://changelog.com/news/submit（需要登录 GitHub）

**填写内容：**

- URL: https://github.com/fangzheng698-lang/agent-prod
- Title: agent-prod — Quality gates for production AI agents
- Description (markdown):

```
**SonarQube for AI agents.** agent-prod wraps any agent run in an
8-gate pipeline — permission, budget, trace integrity, regression,
gray release, audit, answer quality, and execution consistency.

First failure rejects the run. Unlike eval frameworks that score
one answer, agent-prod gates the full run lifecycle.

- 194 tests, 217 real agent sessions validated
- MCP server → works with Claude Desktop, Cursor, Cline
- `pip install agent-prod` / `agent-prod-mcp`
- MIT license
```

## Hacker News (optional)

如果上述任何一个收录了，可以考虑发 Show HN。但不要自己发——等别人替你发。
"""工具注册和执行。纯 Python，无框架依赖。"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict  # JSON Schema
    timeout: float = 30.0


class Tool:
    """一个可执行工具。子类只需实现 execute()。"""

    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}
    timeout: float = 30.0

    async def execute(self, **kwargs) -> str:
        raise NotImplementedError

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            timeout=self.timeout,
        )

    def openai_schema(self) -> dict:
        s = self.spec()
        return {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }


class ToolRegistry:
    """线程安全的工具注册器。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_schemas(self) -> list[dict]:
        return [t.openai_schema() for t in self._tools.values()]

    async def execute(self, name: str, arguments: dict) -> str:
        tool = self.get(name)
        if not tool:
            return f"Error: tool '{name}' not found"
        try:
            return await asyncio.wait_for(
                tool.execute(**arguments),
                timeout=tool.timeout,
            )
        except TimeoutError:
            return f"Error: tool '{name}' timed out after {tool.timeout}s"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"


def register_extended_tools(registry: ToolRegistry) -> ToolRegistry:
    """Phase 8.2: 注册扩展工具到 ToolRegistry。

    Returns the registry for chaining.
    """
    from agent_prod.agent.tools_extended import FileReadTool, ShellExecTool, WebSearchTool
    registry.register(WebSearchTool())
    registry.register(FileReadTool())
    registry.register(ShellExecTool())
    return registry

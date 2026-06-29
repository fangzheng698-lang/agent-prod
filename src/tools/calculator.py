"""tools — agent-prod 内置工具集。

在运行时注册到 ToolRegistry：
  - CalculatorTool: 数学表达式求值
  - WebSearchTool: 网页搜索（agent.agent.tools_extended 中）
  - FileReadTool: 文件读取（agent.agent.tools_extended 中）
  - ShellExecTool: 安全 shell 执行（agent.agent.tools_extended 中）

外部工具可以通过 tools/ 目录注入，无需修改 agent 核心代码。
"""

from __future__ import annotations

import ast
import math
import operator
from typing import ClassVar

from agent_prod.agent.tools import Tool

# 安全的数学运算集合
_SAFE_OPS: dict[type, callable] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_FUNCS: dict[str, callable] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "len": len,
    "int": int,
    "float": float,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "pi": lambda: math.pi,
    "e": lambda: math.e,
}

_SAFE_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


def _safe_eval(expr: str) -> float | int:
    """安全地求值数学表达式。不支持赋值/调用/属性访问。"""
    tree = ast.parse(expr.strip(), mode="eval")

    def _eval(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            return _SAFE_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            return _SAFE_OPS[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Name):
            if node.id in _SAFE_CONSTANTS:
                return _SAFE_CONSTANTS[node.id]
            raise ValueError(f"Unknown identifier: {node.id}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCS:
                args = [_eval(a) for a in node.args]
                return _SAFE_FUNCS[node.func.id](*args)
            raise ValueError(f"Unsupported function call: {ast.dump(node.func)}")
        raise ValueError(f"Unsupported expression: {type(node).__name__}")

    return _eval(tree.body)


class CalculatorTool(Tool):
    """安全的数学表达式求值工具。

    支持: + - * / ** abs() sqrt() sin() cos() log() pi e 等。
    不支持: 赋值语句、属性访问、任意函数调用。
    """

    name: ClassVar[str] = "calculator"
    description: ClassVar[str] = (
        "计算数学表达式。支持 + - * / ** abs() sqrt() sin() cos() log() round() 等。"
        "示例: '2+2' → 4, 'sqrt(16)' → 4.0, '2**10' → 1024"
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式（如 '2+2'、'sqrt(16)'、'sin(pi/2)'）",
            },
        },
        "required": ["expression"],
    }
    timeout: float = 5.0

    async def execute(self, expression: str) -> str:
        """计算表达式并返回结果。"""
        try:
            result = _safe_eval(expression)
            return str(result)
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

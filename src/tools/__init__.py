"""tools — agent-prod 内置工具集。

外部工具注入目录：在此目录下创建 Tool 子类，
import 时通过 ToolRegistry.register() 注册即可被 agent 调用。
"""

__all__ = ["CalculatorTool"]

# 延迟导入以避免循环依赖
def __getattr__(name):
    if name == "CalculatorTool":
        from tools.calculator import CalculatorTool
        return CalculatorTool
    raise AttributeError(f"module 'tools' has no attribute '{name}'")

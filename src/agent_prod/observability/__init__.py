# Copyright (c) 2026 fang.zheng
# License: MIT (see LICENSE file in root)

"""Observability — embedded metrics + structured execution logging + OpenTelemetry agent spans."""

from .metrics import get_registry

__all__ = ["get_registry"]

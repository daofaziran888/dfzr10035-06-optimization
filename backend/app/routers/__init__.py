"""业务路由汇总：分页试采固定路径在前，通用资源路由在后，避免动态路径抢先匹配。"""
from __future__ import annotations

from app.routers import pagination, resource

ROUTERS = [pagination, resource]

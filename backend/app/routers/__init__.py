"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, snapshot_restore

# 专用路由放在通用资源路由之前，避免 /api/{module} 动态前缀抢先匹配固定路径。
ROUTERS = [snapshot_restore, resource]

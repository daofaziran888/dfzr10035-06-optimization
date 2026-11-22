"""分页试采接口：试采、续采、边界校验、验收与清单查询。

这些是固定路径，必须早于通用资源路由 ``/api/{module}`` 注册。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.pagination_trial import (
    PaginationTrialService,
    acceptance_view,
    run_pagination_check,
    run_probe_trial,
    PROBE_ENDPOINT,
)

router = APIRouter(prefix="/api/pagination", tags=["分页试采"])

_service = PaginationTrialService()


class TrialStartPayload(BaseModel):
    trial_id: str | None = None
    endpoint_id: str = ""
    rule_id: str | int | None = None
    token_field: str | None = None
    json_path: str | None = None
    data_field: str | None = None
    start_token: str | None = None
    max_pages: int | None = None
    repeat_limit: int | None = None
    samples: list[Any] = Field(default_factory=list)
    use_probe: bool = False


class TrialResumePayload(BaseModel):
    samples: list[Any] = Field(default_factory=list)


class BoundaryCheckPayload(BaseModel):
    samples: list[Any] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


@router.get("/probe")
def probe_sample() -> dict[str, Any]:
    """内置探针端点的剧本：1 条记录 → 空页（带 token）→ 后续数据 → 重复 token。"""
    from app.services.pagination_trial import PROBE_PAGES
    return {"endpoint_id": PROBE_ENDPOINT, "pages": PROBE_PAGES}


@router.post("/trials")
def start_trial(payload: TrialStartPayload) -> dict[str, Any]:
    config = payload.model_dump(exclude={"samples", "use_probe"})
    config = {key: value for key, value in config.items() if value is not None}
    if payload.use_probe or (not payload.samples and payload.endpoint_id == PROBE_ENDPOINT):
        result = run_probe_trial(config, service=_service)
    else:
        if not payload.samples:
            raise HTTPException(status_code=400, detail="缺少分页试采响应样本")
        result = _service.run_trial(config, payload.samples)
    return result


@router.get("/trials/{trial_id}")
def get_trial(trial_id: str) -> dict[str, Any]:
    session = _service.describe_session(trial_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"分页试采 {trial_id} 不存在")
    return session


@router.post("/trials/{trial_id}/resume")
def resume_trial(trial_id: str, payload: TrialResumePayload) -> dict[str, Any]:
    result = _service.resume_trial(trial_id, payload.samples or None)
    if result is None:
        raise HTTPException(status_code=404, detail=f"分页试采 {trial_id} 不存在")
    return result


@router.post("/trials/{trial_id}/accept")
def accept_trial(trial_id: str) -> dict[str, Any]:
    result = _service.accept_trial(trial_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"分页试采 {trial_id} 不存在")
    return result


@router.get("/trials/{trial_id}/acceptance")
def trial_acceptance(trial_id: str) -> dict[str, Any]:
    view = acceptance_view(trial_id, service=_service)
    if view is None:
        raise HTTPException(status_code=404, detail=f"分页试采 {trial_id} 不存在")
    return view


@router.get("/trials/{trial_id}/checklist")
def trial_checklist(trial_id: str) -> dict[str, Any]:
    if _service.describe_session(trial_id) is None:
        raise HTTPException(status_code=404, detail=f"分页试采 {trial_id} 不存在")
    return _service.pagination_checklist(trial_id)


@router.post("/check")
def check_boundary(payload: BoundaryCheckPayload) -> dict[str, Any]:
    """纯样本边界校验：不落库，直接返回判定、采集结果与恢复位置。"""
    if not payload.samples:
        raise HTTPException(status_code=400, detail="缺少分页试采响应样本")
    return run_pagination_check(payload.samples, payload.config)

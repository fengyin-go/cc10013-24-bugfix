"""采集监控接口：监控指标、阈值版本、阈值窗口、异常待处理区与告警恢复。

路由顺序刻意把固定路径放在 `/{entry_id}` 之前，避免 `/windows`、`/abnormal`
被当成整数编号解析。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.monitor import MonitorService

router = APIRouter(prefix="/api/monitor", tags=["采集监控"])

service = MonitorService()

LIST_FIELDS = ["指标编号", "关联端点", "成功率阈值", "延迟阈值", "错误率告警", "检测频率", "通知渠道", "静默分钟"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按指标编号或关联端点检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按指标编号与状态过滤采集监控列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集监控清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "monitor", "total": total, "items": items}


@router.get("/windows", response_model=PageResult[dict])
def list_windows(
    rule: str | None = Query(default=None, description="按监控指标 id 或指标编号过滤"),
    status: str | None = Query(default=None, description="进行中、通过、失败、已恢复"),
    keyword: str | None = Query(default=None, description="按窗口编号或指标编号检索"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """阈值窗口清单：窗口始终携带启动阈值版本，供指标详情和异常待处理区引用。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_windows(rule=rule, status=status, keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/windows/{window_ref}", response_model=dict)
def get_window(window_ref: str) -> dict:
    """读取单个阈值窗口及其判定版本、阈值快照和恢复记录。"""
    window = service.get_window(window_ref)
    if window is None:
        raise HTTPException(status_code=404, detail=f"阈值窗口 {window_ref} 不存在")
    return window


@router.post("/windows/{window_ref}/judge", response_model=ActionResult)
def judge_window(window_ref: str, payload: EntryPayload) -> ActionResult:
    """按窗口启动版本判定；跨窗样本标记实际判定版本，但不参与本窗口结论。"""
    values = payload.values
    window, message = service.judge_window(
        window_ref,
        samples=values.get("样本记录") or values.get("samples"),
        version_no=str(values.get("阈值版本") or values.get("version") or ""),
        values=values,
    )
    if window is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=window)


@router.post("/windows/{window_ref}/recover", response_model=ActionResult)
def recover_window_alert(window_ref: str, payload: EntryPayload) -> ActionResult:
    """恢复告警：重复提交只记一次，恢复前核对窗口阈值版本。"""
    values = payload.values
    window, message = service.recover_alert(
        window_ref,
        version_no=str(values.get("阈值版本") or values.get("version") or ""),
        operator=str(values.get("operator") or values.get("操作人") or ""),
        reason=str(values.get("恢复原因") or values.get("reason") or ""),
        values=values,
    )
    if window is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=window)


@router.get("/summary", response_model=dict)
def summary() -> dict[str, Any]:
    """指标页卡片：指标总量、已启用、异常待处理窗口和待确认阈值版本。"""
    return service.summary()


@router.get("/abnormal", response_model=dict)
def list_abnormal(
    rule: str | None = Query(default=None, description="按监控指标 id 或指标编号过滤"),
    page: int = 1,
    size: int = 50,
) -> dict[str, Any]:
    """监控异常待处理区：聚拢失败窗口，并带上判定使用的阈值版本。"""
    items, total = service.abnormal_windows(rule=rule, page=page, size=size)
    return {
        "module": "monitor",
        "area": "监控异常待处理区",
        "items": items,
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/by-code/{code}", response_model=dict)
def get_entry_by_code(code: str) -> dict:
    """按指标编号（如 MON-408）读取规则详情，指标抽屉与复测都走这里。"""
    entry = service.get_rule(code)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"监控指标 {code} 不存在或已归档")
    return entry


@router.get("/{entry_id}/versions", response_model=PageResult[dict])
def list_versions(entry_id: str) -> PageResult[dict]:
    """阈值版本清单：每个版本都带阈值、检测频率、静默时间和判定口径。"""
    result = service.list_versions(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"监控指标 {entry_id} 不存在或已归档")
    items, total = result
    return PageResult(items=items, total=total, page=1, size=max(total, 1))


@router.post("/{entry_id}/versions", response_model=ActionResult)
def publish_version(entry_id: str, payload: EntryPayload) -> ActionResult:
    """发布阈值版本：阈值、检测频率和静默时间随版本一起写入；跨窗时标记待确认。"""
    values = payload.values
    entry, message = service.publish_version(
        entry_id,
        values,
        operator=str(values.get("operator") or values.get("操作人") or ""),
        confirm=bool(values.get("confirm") or values.get("确认")),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/versions/{version_no}/confirm", response_model=ActionResult)
def confirm_version(entry_id: str, version_no: str, payload: EntryPayload) -> ActionResult:
    """确认待确认的阈值版本；确认后只影响之后新登记的窗口。"""
    entry, message = service.confirm_version(entry_id, version_no)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/windows", response_model=ActionResult)
def register_window(entry_id: str, payload: EntryPayload) -> ActionResult:
    """登记阈值窗口；同一窗口编号重复登记复用原窗口，不新增第二个阈值窗口。"""
    values = payload.values
    window, message = service.register_window(
        entry_id,
        window_no=str(values.get("窗口编号") or values.get("window_id") or ""),
        version_no=str(values.get("阈值版本") or values.get("version") or ""),
        values=values,
        operator=str(values.get("operator") or values.get("操作人") or ""),
    )
    if window is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=window)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: str) -> dict:
    """读取单条监控指标明细；指标详情页引用与窗口相同的阈值版本。"""
    entry = service.get_rule(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"监控指标 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条监控指标，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="监控指标已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: str, payload: EntryPayload) -> ActionResult:
    """对单条监控指标执行提交校验、启用监控、停用监控、发布阈值版本等动作。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

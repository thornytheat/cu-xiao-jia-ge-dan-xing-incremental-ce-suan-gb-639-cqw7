"""M3 入口：原因码分布、隔离行筛选、隔离区 CSV、退货台账、隔离重放与隔离页。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ... import reasons, web
from ..m5_lift import service as lift_service
from . import quarantine

router = APIRouter()


class ReplayIn(BaseModel):
    dataset: str = "main"
    zero_mode: str = ""


def _batch_id(request: Request) -> str | None:
    ctx = web.get_ctx(request)
    batch = ctx.current_batch(request.query_params.get("batch_id"))
    return batch["batch_id"] if batch else None


@router.get("/api/quarantine")
def api_quarantine(request: Request) -> dict:
    ctx = web.get_ctx(request)
    batch_id = _batch_id(request)
    if batch_id is None:
        return {"batch_id": None, "rows": [], "distribution": [], "counts": {},
                "levels": reasons.LEVEL_LABELS}
    params = request.query_params
    rows = quarantine.enrich(ctx.store.quarantine(batch_id))
    filtered = quarantine.filter_rows(rows, level=params.get("level") or None,
                                      stage=params.get("stage") or None,
                                      code=params.get("code") or None)
    return {
        "batch_id": batch_id,
        "rows": filtered[:400],
        "total": len(filtered),
        "distribution": quarantine.distribution(rows),
        "counts": quarantine.level_counts(rows),
        "levels": reasons.LEVEL_LABELS,
    }


@router.get("/api/quarantine.csv")
def api_quarantine_csv(request: Request) -> Response:
    ctx = web.get_ctx(request)
    batch_id = _batch_id(request)
    if batch_id is None:
        body = quarantine.to_csv([])
    else:
        body = quarantine.to_csv(quarantine.enrich(ctx.store.quarantine(batch_id)))
    filename = f"pricelift-quarantine-{batch_id or 'empty'}.csv"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/returns")
def api_returns(request: Request) -> dict:
    """退货台账：原售与退款分开留账，applied/quarantined 都能逐笔看到。"""
    ctx = web.get_ctx(request)
    batch_id = _batch_id(request)
    if batch_id is None:
        return {"batch_id": None, "rows": [], "applied": 0, "quarantined": 0,
                "refund_total": "0.00"}
    rows = ctx.store.returns(batch_id)
    applied = [r for r in rows if r["status"] == "applied"]
    refund_total = sum((Decimal(r["refund_amount"]) for r in applied), Decimal("0"))
    return {
        "batch_id": batch_id,
        "rows": rows,
        "applied": len(applied),
        "quarantined": len(rows) - len(applied),
        "returned_qty": sum(r["qty"] for r in applied),
        "refund_total": str(refund_total.quantize(Decimal("0.01"))),
    }


@router.post("/api/quarantine/replay")
def api_replay(body: ReplayIn, request: Request) -> dict:
    """隔离到重放：补齐原售后用同一份输入重跑流水线。

    幂等由批次键保证——输入未变时复用已完成批次；输入已变（补了原售/退货编号）
    时算出新键并落新批次。退货按物理行号定序处理，重放不会重复扣减。
    """
    ctx = web.get_ctx(request)
    result = lift_service.run(
        settings=ctx.settings, store=ctx.store,
        dataset=body.dataset or "main",
        zero_mode=body.zero_mode or None)
    batch_id = result["batch"]["batch_id"]
    rows = quarantine.enrich(ctx.store.quarantine(batch_id))
    return_rows = ctx.store.returns(batch_id)
    return {
        "status": result["batch"]["status"],
        "batch_id": batch_id,
        "replay": result["replay"],
        "note": result["note"],
        "counters": result["counters"],
        "quarantine_total": len(rows),
        "returns_applied": sum(1 for r in return_rows if r["status"] == "applied"),
        "returns_quarantined": sum(1 for r in return_rows
                                   if r["status"] == "quarantined"),
    }


@router.get("/quarantine")
def page_quarantine(request: Request):
    ctx = web.get_ctx(request)
    batch_id = _batch_id(request)
    rows = quarantine.enrich(ctx.store.quarantine(batch_id)) if batch_id else []
    return_rows = ctx.store.returns(batch_id) if batch_id else []
    return web.render(
        request, "quarantine.html",
        rows=rows[:400],
        total=len(rows),
        distribution=quarantine.distribution(rows),
        counts=quarantine.level_counts(rows),
        return_rows=return_rows,
        dataset=(ctx.current_batch(batch_id)["dataset"]
                 if batch_id and ctx.current_batch(batch_id) else "main"),
    )

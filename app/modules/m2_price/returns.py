"""分次退货：原售与退款分账，入模用剩余销量。

业务语义（与 docs/DATA-DICTIONARY.md 第 11 节一致）：
- 退货行 = 日销文件里的负销量行，必须带「退货编号」与「原销售日」；
- 退款记在**退货发生日**的销售额里（当天 net_amount 减少，可能为负）；
- 购买量扣回**原销售日**（当天 net_qty 减少），退货当天不会被当成需求暴跌；
- 弹性、基线、增量与实验设计一律吃净口径（net_qty / net_amount）；
- 超额退货（累计超过原售日购买量）与无原售退货先隔离，补齐原售后可重放；
- 按物理行号定序处理，结果只取决于输入内容，重跑不重复扣减（幂等）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from . import coerce


@dataclass
class ReturnRecord:
    line_no: int
    return_id: str
    sku: str
    channel: str
    sku_name: str
    refund_date: object
    origin_date: object
    qty: int
    price_incl: Decimal
    price_excl: Decimal
    tax_rate: Decimal
    refund_amount: Decimal
    status: str                      # applied / quarantined
    code: str = ""
    detail: str = ""
    origin_line_no: int | None = None

    def as_dict(self) -> dict:
        return {
            "line_no": self.line_no,
            "return_id": self.return_id,
            "sku": self.sku,
            "channel": self.channel,
            "sku_name": self.sku_name,
            "refund_date": self.refund_date.isoformat() if self.refund_date else "",
            "origin_date": self.origin_date.isoformat() if self.origin_date else "",
            "qty": self.qty,
            "price_incl": str(self.price_incl),
            "price_excl": str(self.price_excl),
            "tax_rate": str(self.tax_rate),
            "refund_amount": str(self.refund_amount),
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
            "origin_line_no": self.origin_line_no,
        }


def _q(candidate: dict, code: str, detail: str, field_name: str = "") -> dict:
    return {
        "code": code, "stage": "quality", "source": candidate.get("source", "daily_sales"),
        "line_no": candidate["line_no"], "field": field_name or "退货",
        "raw_value": (f"退货编号={candidate.get('return_id') or '—'} "
                      f"原销售日={candidate.get('origin_raw') or '—'} "
                      f"qty={candidate['qty']}"),
        "detail": detail,
    }


def _index_origin(rows) -> dict[tuple[str, str, object], list]:
    """原售日可能仍带着未去重的多行，按行号定序排成列表。"""
    index: dict[tuple[str, str, object], list] = {}
    for row in sorted(rows, key=lambda r: r.line_no):
        index.setdefault((row.sku, row.channel, row.sale_date), []).append(row)
    return index


def _index_refund_day(rows) -> dict[tuple[str, str, object], list]:
    index: dict[tuple[str, str, object], list] = {}
    for row in rows:
        index.setdefault((row.sku, row.channel, row.sale_date), []).append(row)
    return index


def apply_returns(rows, candidates: list[dict]) -> dict:
    """把退货候选对照原售日逐条校验并入账。

    返回：
    - ledger：全部退货记录（applied 与 quarantined 都留账，原售与退款分开）；
    - quarantine：隔离原因行（含每条已入账退货的 Q_RETURN_APPLIED 提示）；
    - refund_only：退货发生日没有销售行时的纯退款账（负金额，给增量/设计用）；
    - applied / quarantined：两类退货编号清单。
    """
    origin_index = _index_origin(rows)
    refund_index = _index_refund_day(rows)
    capacity: dict[tuple[str, str, object], int] = {
        key: sum(r.qty for r in group) for key, group in origin_index.items()}
    allocated: dict[tuple[str, str, object], int] = {}

    ledger: list[ReturnRecord] = []
    quarantine: list[dict] = []
    refund_only: list[dict] = []
    applied_ids: list[str] = []
    quarantined_ids: list[str] = []
    seen_ids: set[str] = set()

    for cand in sorted(candidates, key=lambda c: c["line_no"]):
        refund_amount = coerce.money(cand["price_excl"] * Decimal(cand["qty"]))
        base_record = ReturnRecord(
            line_no=cand["line_no"], return_id=cand["return_id"], sku=cand["sku"],
            channel=cand["channel"], sku_name=cand.get("sku_name", ""),
            refund_date=cand["sale_date"], origin_date=cand.get("origin_date"),
            qty=cand["qty"], price_incl=cand["price_incl"],
            price_excl=cand["price_excl"], tax_rate=cand["tax_rate"],
            refund_amount=refund_amount, status="quarantined")

        def _reject(code: str, detail: str, field_name: str = "") -> None:
            base_record.status = "quarantined"
            base_record.code = code
            base_record.detail = detail
            ledger.append(base_record)
            quarantine.append(_q(cand, code, detail, field_name))
            if cand["return_id"]:
                quarantined_ids.append(cand["return_id"])

        if not cand["return_id"]:
            _reject("Q_RETURN_NO_ID", "负销量退货行缺少退货编号，无法分次留账", "退货编号")
            continue
        if cand["return_id"] in seen_ids:
            _reject("Q_RETURN_DUP_ID",
                    f"退货编号 {cand['return_id']} 重复，同一退货只能入账一次",
                    "退货编号")
            continue
        if cand.get("origin_date") is None:
            _reject("Q_RETURN_BAD_ORIGIN_DATE",
                    f"原销售日 {cand.get('origin_raw') or '空'} 无法解析为自然日",
                    "原销售日")
            continue
        # 编号在通过全部前置校验、确认要入账时才占用：被隔离的编号不会污染后续判断
        seen_ids.add(cand["return_id"])

        key = (cand["sku"], cand["channel"], cand["origin_date"])
        origin_rows = origin_index.get(key)
        if not origin_rows:
            _reject("Q_RETURN_NO_ORIGIN",
                    f"{cand['sku']}/{cand['channel']} 在原销售日 "
                    f"{cand['origin_date'].isoformat()} 没有销售记录，补齐原售后可重放",
                    "原销售日")
            continue
        used = allocated.get(key, 0)
        if used + cand["qty"] > capacity[key]:
            _reject("Q_RETURN_EXCEED",
                    f"原售日购买量 {capacity[key]} 件，已退 {used} 件，"
                    f"本次再退 {cand['qty']} 件超出剩余可退量 {capacity[key] - used} 件",
                    "销量")
            continue

        # 入账：购买量扣回原销售日（多行时按行号顺序分摊，保持总量守恒）
        remain = cand["qty"]
        origin_line = None
        for origin in origin_rows:
            room = origin.qty - origin.returned_qty
            if room <= 0:
                continue
            take = min(room, remain)
            origin.returned_qty += take
            origin.net_qty = origin.qty - origin.returned_qty
            origin.return_ids.append(cand["return_id"])
            origin_line = origin.line_no
            remain -= take
            if remain == 0:
                break
        allocated[key] = used + cand["qty"]

        # 退款记在退货发生日：当天有销售行就冲减该行净额，否则进纯退款账
        refund_key = (cand["sku"], cand["channel"], cand["sale_date"])
        day_rows = refund_index.get(refund_key)
        if day_rows:
            target = day_rows[0]
            target.refund_excl = (target.refund_excl + refund_amount).quantize(
                Decimal("0.01"))
            target.net_amount = coerce.money(target.amount_excl - target.refund_excl)
            target.return_ids.append(cand["return_id"])
        else:
            refund_only.append({
                "sku": cand["sku"], "channel": cand["channel"],
                "sale_date": cand["sale_date"], "amount": -refund_amount,
                "line_no": cand["line_no"], "return_id": cand["return_id"],
            })

        base_record.status = "applied"
        base_record.origin_line_no = origin_line
        base_record.detail = (
            f"退款 {refund_amount} 元记 {cand['sale_date'].isoformat()}，"
            f"购买量 {cand['qty']} 件扣回 {cand['origin_date'].isoformat()}")
        ledger.append(base_record)
        applied_ids.append(cand["return_id"])
        quarantine.append(_q(
            cand, "Q_RETURN_APPLIED",
            f"{cand['return_id']}：退 {cand['qty']} 件，原售日 "
            f"{cand['origin_date'].isoformat()} 扣量，退款日落 "
            f"{cand['sale_date'].isoformat()}"))

    # 原售日净金额随净销量同步（按原行不含税单价折算）。
    # 若退款恰好也落在原售日，refund_excl 已在上面冲减过，这里保留同一份扣减不重复算。
    for group in origin_index.values():
        for row in group:
            if row.returned_qty:
                kept_amount = coerce.money(row.price_excl * Decimal(row.net_qty))
                row.net_amount = kept_amount - row.refund_excl

    ledger.sort(key=lambda r: r.line_no)
    return {
        "ledger": ledger,
        "quarantine": quarantine,
        "refund_only": refund_only,
        "applied": applied_ids,
        "quarantined": quarantined_ids,
    }

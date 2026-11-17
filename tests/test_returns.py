"""分次退货：原售/退款分账、剩余销量入模、超额与无原售隔离、重放与幂等、中断恢复。

覆盖需求里的每一句承诺：
- 退款记退货发生日的销售额，弹性扣回原销售日的购买量，退货当天不是需求暴跌；
- 带退货编号与原销售日的分次退货，原售与退款分开留账，入模用剩余销量；
- 超额及无原售退货隔离，补齐原售后可重放；
- 重试不重复扣减；写入中断后恢复，账目与样本一致。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app import config, reasons
from app.modules.m2_price import returns as returns_mod, service as price_service
from app.modules.m5_lift import service as lift_service
from app.modules.m6_report import service as report_service
from fixtures import dataset as fx


def _sale(day, sku="MY-0012", channel="电商", qty=100, price="12.80", tax="0.13",
          promo=""):
    return [day, channel, sku, "测试商品", price, "元/件", tax, str(qty), promo, "", ""]


def _ret(day, sku, qty, return_id, origin, channel="电商", price="12.80", tax="0.13"):
    return [day, channel, sku, "测试商品", price, "元/件", tax, str(-qty), "",
            return_id, origin]


RETURNS_HEADER = ["日期", "渠道", "商品编码", "商品名称", "含税单价", "单价单位", "税率",
                  "销量", "促销编号", "退货编号", "原销售日"]


def _write(tmp_path, sales_rows, *, promo_rows=(), control_rows=()):
    control = control_rows or [
        fx.control("2026-09-17", "MY-0019", "电商", "13.40", 80),
        fx.control("2026-09-18", "MY-0019", "电商", "13.40", 82),
        fx.control("2026-09-19", "MY-0019", "电商", "13.40", 84),
        fx.control("2026-09-20", "MY-0019", "电商", "13.40", 86),
    ]
    return fx.write_dataset(
        tmp_path, sales_rows=sales_rows, sales_header=RETURNS_HEADER,
        promo_rows=promo_rows, control_rows=control_rows)


def _run(settings, store, dataset="main", zero_mode=None):
    return lift_service.run(settings=settings, store=store, dataset=dataset,
                            zero_mode=zero_mode)


# ---------- 单元：returns 模块 ----------

def _sale_row(day, qty, line_no):
    from app.modules.m2_price.service import SaleRow
    price = Decimal("12.80")
    amount = (price * Decimal(qty)).quantize(config.MONEY_QUANT)
    return SaleRow(line_no=line_no, sale_date=dt.date.fromisoformat(day),
                   channel="电商", sku="MY-0012", sku_name="测试",
                   price_incl=price, price_excl=price, tax_rate=Decimal("0.13"),
                   qty=qty, promo_id="", price_unit="元/件", amount_excl=amount)


def _candidate(line_no, refund_day, origin_day, qty, return_id="RT-1"):
    price = Decimal("12.80")
    return {
        "source": "daily_sales", "line_no": line_no,
        "sale_date": dt.date.fromisoformat(refund_day), "channel": "电商",
        "sku": "MY-0012", "sku_name": "测试", "price_incl": price,
        "price_excl": price, "tax_rate": Decimal("0.13"), "qty": qty,
        "return_id": return_id, "origin_raw": origin_day,
        "origin_date": dt.date.fromisoformat(origin_day) if origin_day else None,
        "price_unit": "元/件",
    }


def test_split_returns_deduct_origin_but_book_refund_on_refund_day():
    rows = [_sale_row("2026-09-20", 100, 2), _sale_row("2026-09-25", 90, 3)]
    result = returns_mod.apply_returns(
        rows, [_candidate(10, "2026-09-25", "2026-09-20", 7)])
    origin, refund_day = rows
    # 原售日：购买量扣回，净销量 = 100 - 7
    assert origin.qty == 100 and origin.net_qty == 93
    assert origin.returned_qty == 7 and origin.return_ids == ["RT-1"]
    # 退货发生日：销量不被当成暴跌（仍是 90），只冲减当天净销售额
    assert refund_day.qty == 90 and refund_day.net_qty == 90
    assert refund_day.refund_excl == Decimal("89.60")
    assert refund_day.net_amount == refund_day.amount_excl - Decimal("89.60")
    assert result["applied"] == ["RT-1"]
    ledger = result["ledger"][0]
    assert ledger.status == "applied" and ledger.origin_line_no == 2
    assert ledger.refund_amount == Decimal("89.60")


def test_multi_part_returns_accumulate_against_same_origin():
    rows = [_sale_row("2026-09-20", 10, 2)]
    candidates = [
        _candidate(11, "2026-09-26", "2026-09-20", 4, "RT-1"),
        _candidate(12, "2026-09-27", "2026-09-20", 3, "RT-2"),
    ]
    result = returns_mod.apply_returns(rows, candidates)
    assert rows[0].returned_qty == 7 and rows[0].net_qty == 3
    assert result["applied"] == ["RT-1", "RT-2"]


def test_over_return_is_quarantined_and_changes_nothing():
    rows = [_sale_row("2026-09-20", 5, 2)]
    result = returns_mod.apply_returns(
        rows, [_candidate(10, "2026-09-25", "2026-09-20", 6)])
    assert result["quarantined"] == ["RT-1"]
    assert result["ledger"][0].status == "quarantined"
    assert result["ledger"][0].code == "Q_RETURN_EXCEED"
    # 隔离退货不碰任何账目
    assert rows[0].returned_qty == 0 and rows[0].net_qty == 5
    assert rows[0].net_amount == rows[0].amount_excl
    code = result["quarantine"][0]["code"]
    assert reasons.level_of(code) == reasons.ERROR


def test_partial_then_exceed_keeps_first_application():
    rows = [_sale_row("2026-09-20", 5, 2)]
    candidates = [
        _candidate(11, "2026-09-26", "2026-09-20", 3, "RT-1"),
        _candidate(12, "2026-09-27", "2026-09-20", 3, "RT-2"),
    ]
    result = returns_mod.apply_returns(rows, candidates)
    assert result["applied"] == ["RT-1"] and result["quarantined"] == ["RT-2"]
    assert rows[0].returned_qty == 3 and rows[0].net_qty == 2
    assert result["ledger"][1].code == "Q_RETURN_EXCEED"


def test_return_without_origin_sale_is_quarantined():
    rows = [_sale_row("2026-09-20", 5, 2)]
    result = returns_mod.apply_returns(
        rows, [_candidate(10, "2026-09-25", "2026-09-10", 1)])
    assert result["quarantined"] == ["RT-1"]
    assert result["ledger"][0].code == "Q_RETURN_NO_ORIGIN"
    assert rows[0].returned_qty == 0


def test_missing_id_dup_id_and_bad_date():
    rows = [_sale_row("2026-09-20", 50, 2)]
    candidates = [
        _candidate(10, "2026-09-25", "2026-09-20", 1, "RT-1"),
        _candidate(11, "2026-09-26", "2026-09-20", 1, "RT-1"),
        _candidate(12, "2026-09-27", "2026-09-20", 1, ""),
    ]
    candidates[2]["origin_date"] = None
    bad_date = _candidate(13, "2026-09-28", "2026-09-20", 1, "RT-3")
    bad_date["origin_date"] = None
    bad_date["origin_raw"] = "不是日期"
    candidates.append(bad_date)
    result = returns_mod.apply_returns(rows, candidates)
    codes = {r.code for r in result["ledger"] if r.status == "quarantined"}
    assert codes == {"Q_RETURN_DUP_ID", "Q_RETURN_NO_ID", "Q_RETURN_BAD_ORIGIN_DATE"}
    assert rows[0].returned_qty == 1  # 只有第一笔入账


def test_same_day_refund_keeps_both_deductions_on_one_row():
    # 退款日恰好等于原售日：净量扣减与退款金额扣减落在同一行，两个都要保留
    rows = [_sale_row("2026-09-20", 10, 2)]
    result = returns_mod.apply_returns(
        rows, [_candidate(9, "2026-09-20", "2026-09-20", 4, "RT-S")])
    row = rows[0]
    assert result["applied"] == ["RT-S"]
    assert row.net_qty == 6
    assert row.refund_excl == (Decimal("12.80") * 4).quantize(Decimal("0.01"))
    assert row.net_amount == (Decimal("12.80") * 6).quantize(Decimal("0.01")) \
        - row.refund_excl


def test_refund_only_day_kept_as_separate_ledger_entry():
    # 退货发生日没有任何销售行：不能凭空造需求点，退款进 refund_only
    rows = [_sale_row("2026-09-20", 10, 2)]
    result = returns_mod.apply_returns(
        rows, [_candidate(10, "2026-11-01", "2026-09-20", 2)])
    assert len(result["refund_only"]) == 1
    item = result["refund_only"][0]
    assert item["sale_date"] == dt.date(2026, 11, 1)
    assert item["amount"] == -Decimal("25.60")
    assert rows[0].net_qty == 8


def test_quarantined_return_id_does_not_block_later_valid_entry():
    # 第一笔因坏日期被隔离，编号不被占用；修正日期后的同编号新行应当正常入账
    rows = [_sale_row("2026-09-20", 50, 2)]
    bad = _candidate(10, "2026-09-25", "2026-09-20", 1, "RT-X")
    bad["origin_date"] = None
    bad["origin_raw"] = "坏日期"
    good = _candidate(11, "2026-09-26", "2026-09-20", 2, "RT-X")
    result = returns_mod.apply_returns(rows, [bad, good])
    statuses = {(r.line_no, r.status, r.code) for r in result["ledger"]}
    assert (10, "quarantined", "Q_RETURN_BAD_ORIGIN_DATE") in statuses
    assert (11, "applied", "") in statuses
    assert rows[0].returned_qty == 2


def test_returns_are_deterministic_and_idempotent():
    # 同一输入跑两遍：行号定序，结果逐字段一致，不会重复扣减
    def build():
        rows = [_sale_row("2026-09-20", 10, 2)]
        return rows, returns_mod.apply_returns(
            rows, [_candidate(10, "2026-09-25", "2026-09-20", 4)])
    rows_a, result_a = build()
    rows_b, result_b = build()
    assert rows_a[0].net_qty == rows_b[0].net_qty == 6
    assert result_a["ledger"][0].as_dict() == result_b["ledger"][0].as_dict()


def test_bare_negative_qty_without_return_fields_uses_old_code(tmp_path):
    settings = _write(tmp_path, [
        _sale("2026-09-17"), _sale("2026-09-18"),
        ["2026-09-19", "电商", "MY-0012", "测试", "12.80", "元/件", "0.13",
         "-3", "", "", ""],
    ])
    from app.store import Store
    store = Store(settings.db_path)
    result = _run(settings, store)
    codes = {q["code"] for q in store.quarantine(result["batch"]["batch_id"])}
    assert "P_QTY_NEGATIVE" in codes
    assert store.returns_count(result["batch"]["batch_id"]) == 0


# ---------- 流水线：净口径入模 ----------

def test_elasticity_and_lift_consume_net_quantity(tmp_path):
    promo = [fx.promo("P1", "MY-0012", "电商", 1, "2026-09-21", "2026-09-24",
                      "9.90")]
    sales = [
        _sale("2026-09-17", qty=100), _sale("2026-09-18", qty=100),
        _sale("2026-09-19", qty=100), _sale("2026-09-20", qty=100),
        _sale("2026-09-21", qty=120, promo="P1", price="9.90"),
        _sale("2026-09-22", qty=120, promo="P1", price="9.90"),
        _sale("2026-09-23", qty=120, promo="P1", price="9.90"),
        # 退原售日 09-17 的 30 件：09-17 净销量降到 70，退款落在 09-22
        _ret("2026-09-22", "MY-0012", 30, "RT-1", "2026-09-17", price="12.80"),
    ]
    settings = _write(tmp_path, sales, promo_rows=promo)
    from app.store import Store
    store = Store(settings.db_path)
    result = _run(settings, store)
    bid = result["batch"]["batch_id"]
    sales_rows = {r["sale_date"]: r for r in store.sales(bid)
                  if r["sku"] == "MY-0012"}
    assert sales_rows["2026-09-17"]["net_qty"] == 70
    assert sales_rows["2026-09-17"]["qty"] == 100  # 原售口径不变
    # 退货当天销量仍是 120，没有被当成需求暴跌
    assert sales_rows["2026-09-22"]["net_qty"] == 120
    refund = Decimal("12.80") / Decimal("1.13")
    refund = refund.quantize(Decimal("0.0001")) * Decimal(30)
    assert Decimal(sales_rows["2026-09-22"]["refund_excl"]) == refund.quantize(
        Decimal("0.01"))
    # 弹性样本数不变（退货不造新点、不删点），但 y 用净销量
    elastic = {r["group_key"]: r for r in store.elasticity(bid)}
    assert elastic["MY-0012/电商"]["n"] == 7
    # 退货台账与六账
    assert store.returns_count(bid) == 1
    assert store.returns_count(bid, status="applied") == 1
    lines = {r["account"]: r for r in report_service.reconcile(store, bid)}
    assert lines["退货账"]["verdict"] == "平"
    assert report_service.report_payload(store, bid)["balanced"] is True


def test_quarantined_returns_can_be_replayed_after_origin_completed(tmp_path):
    sales = [
        _sale("2026-09-17", qty=10), _sale("2026-09-18", qty=10),
        _sale("2026-09-19", qty=10), _sale("2026-09-20", qty=10),
        # 原售日 09-16 不存在 → 先隔离
        _ret("2026-09-21", "MY-0012", 2, "RT-9", "2026-09-16"),
    ]
    settings = _write(tmp_path, sales)
    from app.store import Store
    store = Store(settings.db_path)
    first = _run(settings, store)
    first_id = first["batch"]["batch_id"]
    ledger = store.returns(first_id)
    assert ledger[0]["status"] == "quarantined"
    assert ledger[0]["code"] == "Q_RETURN_NO_ORIGIN"
    assert first["counters"]["returns_applied"] == 0

    # 补齐原售：在同目录重写输入文件（模拟业务回源补数），再重放
    fixed = sales + [_sale("2026-09-16", qty=20)]
    _write(tmp_path, fixed)
    second = _run(settings, store)
    second_id = second["batch"]["batch_id"]
    assert second_id != first_id  # 输入摘要变了，新批次
    ledger = store.returns(second_id)
    assert ledger[0]["status"] == "applied"
    assert second["counters"]["returns_applied"] == 1
    assert second["counters"]["returns_quarantined"] == 0
    origin = next(r for r in store.sales(second_id)
                  if r["sale_date"] == "2026-09-16")
    assert origin["net_qty"] == 18 and origin["returned_qty"] == 2


def test_replay_same_input_is_idempotent_and_deducts_once(tmp_path):
    sales = [
        _sale("2026-09-17", qty=10), _sale("2026-09-18", qty=10),
        _sale("2026-09-19", qty=10),
        _ret("2026-09-20", "MY-0012", 4, "RT-1", "2026-09-17"),
    ]
    settings = _write(tmp_path, sales)
    from app.store import Store
    store = Store(settings.db_path)
    first = _run(settings, store)
    second = _run(settings, store)
    assert first["batch"]["batch_id"] == second["batch"]["batch_id"]
    assert second["replay"] is True
    bid = first["batch"]["batch_id"]
    # 重放没有重复扣减：原售日净销量仍是 6，退货仍只有 1 笔
    origin = next(r for r in store.sales(bid) if r["sale_date"] == "2026-09-17")
    assert origin["net_qty"] == 6 and origin["returned_qty"] == 4
    assert store.returns_count(bid, status="applied") == 1


def test_write_interruption_recovers_with_consistent_ledger(tmp_path, monkeypatch):
    sales = [_sale(f"2026-09-{17 + i:02d}", qty=10) for i in range(4)]
    sales.append(_ret("2026-09-21", "MY-0012", 2, "RT-1", "2026-09-17"))
    settings = _write(tmp_path, sales)
    from app.store import Store
    store = Store(settings.db_path)

    real_write = store.write_batch
    calls = {"n": 0}

    def failing_write(batch_id, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            # 模拟写账中途进程被杀：事务回滚后批次头停在 running
            raise RuntimeError("simulated crash mid-write")
        return real_write(batch_id, payload)

    monkeypatch.setattr(store, "write_batch", failing_write)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _run(settings, store)
    # 残留批次是 failed，且没有任何半批账目
    stale = store.unfinished_batches()
    assert len(stale) == 1 and stale[0]["status"] == "failed"
    assert store.sales_count(stale[0]["batch_id"]) == 0
    assert store.returns_count(stale[0]["batch_id"]) == 0

    monkeypatch.undo()
    recovered = _run(settings, store)
    bid = recovered["batch"]["batch_id"]
    assert recovered["replay"] is False
    assert store.unfinished_batches() == []
    # 恢复后账目与样本一致：退货账守恒、净销量正确
    totals = store.totals(bid)
    assert int(totals["gross_qty"]) == int(totals["net_qty_total"]) + int(
        totals["returned_qty"])
    origin = next(r for r in store.sales(bid) if r["sale_date"] == "2026-09-17")
    assert origin["net_qty"] == 8
    assert store.returns_count(bid, status="applied") == 1
    assert report_service.report_payload(store, bid)["balanced"] is True


# ---------- API 与页面：隔离到重放 ----------

def test_returns_api_and_replay_endpoint(client, tmp_path):
    # 用 returns 样例集走完整 HTTP 流程
    imported = client.post("/api/import", json={"dataset": "returns"})
    assert imported.status_code == 200
    payload = imported.json()
    assert payload["counters"]["returns_applied"] == 3
    assert payload["counters"]["returns_quarantined"] == 4

    returns_api = client.get("/api/returns").json()
    assert returns_api["applied"] == 3 and returns_api["quarantined"] == 4
    statuses = {row["status"] for row in returns_api["rows"]}
    assert statuses == {"applied", "quarantined"}
    codes = {row["code"] for row in returns_api["rows"] if row["status"] == "quarantined"}
    assert {"Q_RETURN_EXCEED", "Q_RETURN_NO_ORIGIN", "Q_RETURN_DUP_ID",
            "Q_RETURN_NO_ID"} <= codes

    # 隔离页能看到退货台账与重放表单
    page = client.get("/quarantine").text
    assert "退货台账" in page and "补齐后重放" in page and "RT-04" in page

    # 重放同一份输入：幂等，不新增批次、不重复扣减
    replay = client.post("/api/quarantine/replay",
                         json={"dataset": "returns", "zero_mode": ""})
    assert replay.status_code == 200
    assert replay.json()["replay"] is True
    assert replay.json()["returns_applied"] == 3
    again = client.get("/api/returns").json()
    assert again["applied"] == 3

    # 退货台账导出
    csv_body = client.get("/api/export?kind=returns")
    assert csv_body.status_code == 200
    assert "退货编号" in csv_body.text and "RT-01" in csv_body.text


def test_quarantine_distribution_includes_return_reasons(client):
    client.post("/api/import", json={"dataset": "returns"})
    distribution = client.get("/api/quarantine").json()["distribution"]
    codes = {row["code"] for row in distribution}
    assert "Q_RETURN_APPLIED" in codes
    assert "Q_RETURN_EXCEED" in codes and "Q_RETURN_NO_ORIGIN" in codes
    applied = next(row for row in distribution if row["code"] == "Q_RETURN_APPLIED")
    assert applied["level"] == reasons.INFO
    exceeded = next(row for row in distribution if row["code"] == "Q_RETURN_EXCEED")
    assert exceeded["level"] == reasons.ERROR


def test_refund_only_day_flows_into_design_series(client):
    client.post("/api/import", json={"dataset": "returns"})
    body = client.get("/api/design?sku=MY-0021&channel=商超").json()
    plan = body["plan"]
    # RT-03 是落在 2026-11-01 的纯退款（退货日没有销售行）：
    # 设计序列必须能在不报错的前提下吃下这笔负金额，历史窗不被截断
    assert plan["history_days"] >= 7
    refund_rows = [
        row for row in client.get("/api/returns").json()["rows"]
        if row["return_id"] == "RT-03" and row["status"] == "applied"]
    assert refund_rows and refund_rows[0]["refund_date"] == "2026-11-01"
    # 纯退款通过 store 重建后能进 build_series，且标 present 而不是缺日
    from app.modules.m7_design import power
    from app.modules.m7_design import service as design_service
    store = client.app.state.store
    batch = store.latest_batch()
    sales_rows = store.sales(batch["batch_id"])
    refund_only = design_service._refund_only_days(store, batch["batch_id"], sales_rows)
    assert any(item["return_id"] == "RT-03" for item in refund_only)
    series = power.build_series(sales_rows, "MY-0021", "商超", refund_only)
    slot = next(day for day in series if day["date"] == "2026-11-01")
    assert slot["present"] is True and slot["gmv"] < 0

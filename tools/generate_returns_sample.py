"""生成分次退货样例集 daily_sales_returns.csv（开发期工具，非运行时代码）。

样例刻意覆盖：
- RT-01/RT-02：同一原售日的分次退货，累计不超额，正常入账；
- RT-03：退货发生日没有销售行的纯退款（退款单独落账）；
- RT-04：退货量超过原售日剩余可退量，隔离 Q_RETURN_EXCEED；
- RT-05：原销售日没有对应销售记录，隔离 Q_RETURN_NO_ORIGIN；
- RT-06：退货编号与 RT-01 重复，隔离 Q_RETURN_DUP_ID；
- RT-07：缺退货编号，隔离 Q_RETURN_NO_ID；
- 裸负销量（无退货编号也无原销售日）：隔离 P_QTY_NEGATIVE。
输出 UTF-8（与主样例的 GB18030 故意区分），列尾追加退货两列。
"""
from __future__ import annotations

import csv
import datetime as dt
import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "sample_data" / "daily_sales_returns.csv"

HEADER = ["日期", "渠道", "商品编码", "商品名称", "含税单价", "单价单位", "税率",
          "销量", "促销编号", "退货编号", "原销售日"]

SKUS = {
    "MY-0012": ("原味酸奶 200g", "12.80", "0.13"),
    "MY-0019": ("原味酸奶 200g 竞品对照", "13.40", "0.13"),
    "MY-0021": ("低脂鲜奶 950ml", "18.60", "0.09"),
    "MY-0028": ("低脂鲜奶 950ml 常规对照", "19.20", "0.09"),
}
CHANNELS = ("电商", "商超")
START = dt.date(2026, 9, 17)
DAYS = 45

# 促销窗与 promo_calendar.csv 对齐：P2601 MY-0012/电商 10-05~10-25；P2602 MY-0021/商超 10-08~10-22
P2601 = (dt.date(2026, 10, 5), dt.date(2026, 10, 25))
P2602 = (dt.date(2026, 10, 8), dt.date(2026, 10, 22))


def _promo(day: dt.date, sku: str, channel: str) -> str:
    if sku == "MY-0012" and channel == "电商" and P2601[0] <= day < P2601[1]:
        return "P2601"
    if sku == "MY-0021" and channel == "商超" and P2602[0] <= day < P2602[1]:
        return "P2602"
    return ""


def _price(day: dt.date, sku: str, channel: str, default_price: str) -> str:
    """促销日直接写活动价（与主样例口径一致），保证价格有两个水平可估弹性。"""
    if sku == "MY-0012" and channel == "电商":
        if dt.date(2026, 10, 5) <= day < dt.date(2026, 10, 12):
            return "9.90"
        if dt.date(2026, 10, 12) <= day < dt.date(2026, 10, 25):
            return "8.90"
    if (sku == "MY-0021" and channel == "商超"
            and dt.date(2026, 10, 8) <= day < dt.date(2026, 10, 22)):
        return "15.80"
    return default_price


def _qty(day: dt.date, sku: str, channel: str) -> int:
    base = 120 if channel == "电商" else 90
    wave = ((day - START).days * 7 + int(sku[-2:])) % 17
    return base + wave


def main() -> None:
    rows = []
    for offset in range(DAYS):
        day = START + dt.timedelta(days=offset)
        for channel in CHANNELS:
            for sku, (name, price, tax) in SKUS.items():
                rows.append([
                    day.isoformat(), channel, sku, name,
                    _price(day, sku, channel, price), "元/件", tax,
                    str(_qty(day, sku, channel)), _promo(day, sku, channel), "", "",
                ])

    def negative(date_text, channel, sku, qty, return_id="", origin="", price=None):
        name, default_price, tax = SKUS[sku]
        rows.append([date_text, channel, sku, name, price or default_price, "元/件",
                     tax, str(-qty), "", return_id, origin])

    # 原售日 2026-09-20 的 MY-0012/电商 有足够库存承接分次退货
    negative("2026-09-25", "电商", "MY-0012", 5, "RT-01", "2026-09-20")
    negative("2026-09-27", "电商", "MY-0012", 3, "RT-02", "2026-09-20")
    # 纯退款日：2026-11-01 超出日销窗口，当天没有销售行
    negative("2026-11-01", "商超", "MY-0021", 2, "RT-03", "2026-10-10")
    # 超额：RT-01/RT-02 已退 8 件，再退 500 件远超原售日购买量
    negative("2026-09-28", "电商", "MY-0012", 500, "RT-04", "2026-09-20")
    # 无原售：该商品该渠道在 2026-09-10（窗口开始前）没有记录
    negative("2026-09-29", "电商", "MY-0012", 1, "RT-05", "2026-09-10")
    # 退货编号重复
    negative("2026-09-30", "电商", "MY-0012", 1, "RT-01", "2026-09-21")
    # 缺退货编号（有原销售日）
    negative("2026-10-02", "商超", "MY-0021", 1, "", "2026-09-25")
    # 裸负销量：两列都空，走 P_QTY_NEGATIVE
    negative("2026-10-03", "商超", "MY-0021", 1)

    with OUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\r\n")
        writer.writerow(HEADER)
        writer.writerows(rows)
    print(f"wrote {OUT} with {len(rows)} data rows")


if __name__ == "__main__":
    main()

"""Fetch the latest trading day's data for the 13 FHC stocks + indices.

Writes data/_today.json, consumed by update_history.py. Safe to run on
non-trading days (e.g. weekend cron retry) — it just re-fetches whatever
the most recent available trading day is; update_history.py dedupes.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "_today.json"

WINDOW_DAYS = 15  # calendar days back — comfortably covers holidays/weekends


def main() -> None:
    window_start = (dt.date.today() - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")

    per_stock_price: dict[str, list[dict]] = {}
    per_stock_inst: dict[str, dict[str, dict]] = {}
    per_stock_hold: dict[str, list[dict]] = {}

    for code, name in common.STOCKS:
        print(f"[fetch_today] {code} {name} ...", flush=True)
        per_stock_price[code] = common.fetch_finmind("TaiwanStockPrice", code, window_start)
        time.sleep(0.2)
        inst_rows = common.fetch_finmind("TaiwanStockInstitutionalInvestorsBuySell", code, window_start)
        per_stock_inst[code] = common.split_institutional(inst_rows)
        time.sleep(0.2)
        per_stock_hold[code] = common.fetch_finmind("TaiwanStockShareholding", code, window_start)
        time.sleep(0.2)

    # Latest date that ALL 13 stocks have price data for.
    common_dates = None
    for code in common.STOCK_CODES:
        dates = {r["date"].replace("-", "") for r in per_stock_price[code]}
        common_dates = dates if common_dates is None else (common_dates & dates)
    if not common_dates:
        raise RuntimeError("No common trading date found across all 13 stocks")
    sorted_dates = sorted(common_dates)
    latest_date = sorted_dates[-1]
    prev_date = sorted_dates[-2] if len(sorted_dates) >= 2 else None

    def price_row(code: str, date: str) -> dict | None:
        for r in per_stock_price[code]:
            if r["date"].replace("-", "") == date:
                return r
        return None

    def hold_ratio(code: str, on_or_before: str) -> float | None:
        rows = sorted(per_stock_hold[code], key=lambda r: r["date"])
        best = None
        for r in rows:
            d = r["date"].replace("-", "")
            if d <= on_or_before:
                best = r
            else:
                break
        return best["ForeignInvestmentSharesRatio"] if best else None

    rows_out = []
    for code, name in common.STOCKS:
        today_p = price_row(code, latest_date)
        prev_p = price_row(code, prev_date) if prev_date else None
        if today_p is None:
            raise RuntimeError(f"{code} missing price row for {latest_date}")
        close = today_p["close"]
        vol = today_p["Trading_Volume"] / 1000.0  # 張
        pct = ((close - prev_p["close"]) / prev_p["close"] * 100.0) if prev_p else 0.0
        inst_today = per_stock_inst[code].get(latest_date, {"fL": 0.0, "tL": 0.0, "dL": 0.0, "totL": 0.0})
        f_hold = hold_ratio(code, latest_date)
        f_hold_prev = hold_ratio(code, prev_date) if prev_date else None
        rows_out.append({
            "code": code, "name": name,
            "close": close, "vol": vol, "pct": pct,
            "fL": inst_today["fL"], "tL": inst_today["tL"], "dL": inst_today["dL"], "totL": inst_today["totL"],
            "fHold": f_hold, "fHoldPrev": f_hold_prev,
        })

    idx_date, idx_today = common.find_latest_index_date(latest_date)
    taiex_pct = fin_pct = 0.0
    if prev_date:
        prev_idx_date, idx_prev = common.find_latest_index_date(prev_date)
        taiex_pct = (idx_today["taiex"] - idx_prev["taiex"]) / idx_prev["taiex"] * 100.0
        fin_pct = (idx_today["fin"] - idx_prev["fin"]) / idx_prev["fin"] * 100.0

    out = {
        "date": latest_date,
        "rows": rows_out,
        "taiex": idx_today["taiex"],
        "fin": idx_today["fin"],
        "taiexPct": taiex_pct,
        "finPct": fin_pct,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[fetch_today] wrote {OUT_PATH} for date {latest_date}")


if __name__ == "__main__":
    main()

"""One-off: extend data/history_cmoney.json backwards to 2021-01-01.

Pulls 2021-01-01 ~ 2024-12-31 per-stock history from FinMind (price,
volume, institutional net buy/sell, foreign holding ratio) and prepends
it to the existing CMoney-sourced history. FinMind and CMoney values
were cross-validated as identical, so mixing sources is safe.

Columns FinMind cannot provide for 2021-2024 (投信/自營商/法人合計
持股比率, real 買賣超金額) stay None/missing until the CMoney quota
allows backfilling them (fetch_cmoney.py --backfill-holdings).

Run: python scripts/backfill_finmind.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = REPO_ROOT / "data" / "history_cmoney.json"

START = "2021-01-01"
END = "2024-12-31"


def main() -> None:
    history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))

    for code, name in common.STOCKS:
        days = history["stocks"][code]["days"]
        earliest = days[0][0] if days else "99999999"

        print(f"[backfill_finmind] {code} {name} ...", flush=True)
        prices = common.fetch_finmind("TaiwanStockPrice", code, START, END)
        inst_rows = common.fetch_finmind("TaiwanStockInstitutionalInvestorsBuySell", code, START, END)
        hold_rows = common.fetch_finmind("TaiwanStockShareholding", code, START, END)

        inst_by_date = common.split_institutional(inst_rows)
        hold_by_date = {r["date"].replace("-", ""): r["ForeignInvestmentSharesRatio"]
                        for r in hold_rows}

        new_days = []
        for p in prices:
            date = p["date"].replace("-", "")
            if date >= earliest:
                continue
            inst = inst_by_date.get(date, {})
            fL = inst.get("fL", 0.0)
            tL = inst.get("tL", 0.0)
            dL = inst.get("dL", 0.0)
            totL = inst.get("totL", fL + tL + dL)
            fHold = hold_by_date.get(date)
            new_days.append([
                date, p["close"], p["Trading_Volume"] / 1000.0,
                fL, tL, dL, totL,
                round(fHold, 2) if fHold is not None else None,
                None, None, None,
            ])

        new_days.sort(key=lambda d: d[0])
        history["stocks"][code]["days"] = new_days + days
        print(f"  +{len(new_days)} days (now {len(history['stocks'][code]['days'])})")

    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[backfill_finmind] wrote {HISTORY_PATH}")


if __name__ == "__main__":
    main()

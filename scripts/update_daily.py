"""Daily update for the main page history: append missing trading days
from FinMind (stocks) + TWSE MI_INDEX (indices) — the primary public data
sources as of 2026-07-16. 投信/自營商/合計持股比率 and real amounts are
CMoney-only columns and stay None for new days.

Run after ~16:00 Taipei (institutional data publish time):
  python scripts/update_daily.py
then rebuild:
  python scripts/build_index.py --data data/history_cmoney.json --out index.html --source ...
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common

HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "history_cmoney.json"

history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
last_date = history["index"][-1]["d"]
start = (dt.datetime.strptime(last_date, "%Y%m%d").date() + dt.timedelta(days=1))
today = dt.date.today()
if start > today:
    print("already up to date")
    sys.exit(0)
start_s = start.strftime("%Y-%m-%d")
end_s = today.strftime("%Y-%m-%d")
print(f"appending {start_s} ~ {end_s}")

added_dates = set()
for code, name in common.STOCKS:
    prices = common.fetch_finmind("TaiwanStockPrice", code, start_s, end_s)
    inst_rows = common.fetch_finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start_s, end_s)
    hold_rows = common.fetch_finmind("TaiwanStockShareholding", code, start_s, end_s)
    inst_by_date = common.split_institutional(inst_rows)
    hold_by_date = {r["date"].replace("-", ""): r["ForeignInvestmentSharesRatio"] for r in hold_rows}
    days = history["stocks"][code]["days"]
    existing = {d[0] for d in days}
    for p in prices:
        date = p["date"].replace("-", "")
        if date in existing:
            continue
        # 法人買賣超約 16:00 才公告；當日尚無法人資料就先不收，明天再補
        if date not in inst_by_date:
            continue
        inst = inst_by_date[date]
        fL, tL, dL = inst.get("fL", 0.0), inst.get("tL", 0.0), inst.get("dL", 0.0)
        fHold = hold_by_date.get(date)
        days.append([date, p["close"], p["Trading_Volume"] / 1000.0,
                     fL, tL, dL, inst.get("totL", fL + tL + dL),
                     round(fHold, 2) if fHold is not None else None, None, None, None])
        added_dates.add(date)
    days.sort(key=lambda d: d[0])
    print(f"  {code} {name}: +{len([d for d in days if d[0] in added_dates])} new")

# 指數：TWSE MI_INDEX 逐日
d = start
idx_existing = {e["d"] for e in history["index"]}
while d <= today:
    ds = d.strftime("%Y%m%d")
    if ds not in idx_existing and ds in added_dates:
        r = common.fetch_twse_index(ds)
        if r:
            history["index"].append({"d": ds, "taiex": r["taiex"], "fin": r["fin"]})
            print(f"  index {ds}: taiex={r['taiex']} fin={r['fin']}")
    d += dt.timedelta(days=1)
history["index"].sort(key=lambda e: e["d"])

HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"done. latest = {history['index'][-1]['d']}")

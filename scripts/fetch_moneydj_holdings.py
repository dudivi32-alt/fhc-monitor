"""Backfill 投信 / 自營商 / 三大法人 持股比率 from MoneyDJ.

Source page (per stock, Big5 encoded):
  https://5850web.moneydj.com/z/zc/zcl/zcl.djhtm?a=<code>&c=YYYY-M-D&d=YYYY-M-D

Each row is:
  日期(民國) | 買賣超: 外資 投信 自營商 單日合計
            | 持股張數: 外資 投信 自營商 單日合計
            | 持股比率: 外資% 三大法人%

MoneyDJ only publishes the 外資 and 三大法人 ratios, so the 投信 / 自營商
ratios are derived from the share counts, using 外資 to recover the
share base (發行股數):

    base   = 外資張數 / 外資%
    投信%  = 投信張數 / base
    自營商% = 自營商張數 / base

This base reproduces the 投信 ratios of the previous CMoney series
exactly. The 自營商 ratio comes out ~0.4pp lower than CMoney's, because
CMoney counts 避險部位 in 自營商持股 and MoneyDJ does not.

The day rows in data/history_cmoney.json are
  [date, close, vol, fL, tL, dL, totL, fHold, tHold, dHold, totHold]
and only slots 8/9/10 are written here. totHold is recomputed as
fHold + tHold + dHold using our own fHold when we have one, so the
"合計" line always matches the three components on the chart.

Usage:
  python scripts/fetch_moneydj_holdings.py                 # fill the gap
  python scripts/fetch_moneydj_holdings.py --start 20260709 --end 20260729
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common  # noqa: E402

URL = ("https://5850web.moneydj.com/z/zc/zcl/zcl.djhtm"
       "?a={code}&c={start}&d={end}")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fhc-monitor/1.0)"}
ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "history_cmoney.json"

ROW_RE = re.compile(
    r"<td[^>]*>(\d{3}/\d{2}/\d{2})</td>((?:\s*<td[^>]*>[^<]*</td>){10})",
    re.S)
CELL_RE = re.compile(r"<td[^>]*>([^<]*)</td>")


def _num(text: str) -> float | None:
    t = text.replace(",", "").replace("%", "").strip()
    if not t or t in {"-", "--", "&nbsp;"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fetch_stock(code: str, start: dt.date, end: dt.date, retries: int = 3) -> dict:
    """Return {YYYYMMDD: (tHold, dHold, fRatio)} for one stock."""
    url = URL.format(code=code,
                     start=f"{start.year}-{start.month}-{start.day}",
                     end=f"{end.year}-{end.month}-{end.day}")
    html = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("big5", "replace")
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                print(f"  [{code}] fetch failed: {exc}")
                return {}
            time.sleep(3 * (attempt + 1))

    out: dict[str, tuple] = {}
    for m in ROW_RE.finditer(html):
        roc = m.group(1)
        cells = [_num(c) for c in CELL_RE.findall(m.group(2))]
        if len(cells) != 10:
            continue
        f_shares, t_shares, d_shares = cells[4], cells[5], cells[6]
        f_ratio = cells[8]
        if None in (f_shares, t_shares, d_shares, f_ratio):
            continue
        if f_ratio <= 0 or f_shares <= 0:
            continue
        base = f_shares / f_ratio             # 發行股數（同 MoneyDJ 外資分母）
        y, mo, da = roc.split("/")
        date = f"{int(y) + 1911}{mo}{da}"
        out[date] = (round(t_shares / base, 2),
                     round(d_shares / base, 2),
                     f_ratio)
    return out


def parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y%m%d").date()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--start", help="YYYYMMDD (default: first day missing tHold)")
    ap.add_argument("--end", help="YYYYMMDD (default: today)")
    ap.add_argument("--sleep", type=float, default=1.5)
    args = ap.parse_args()

    path = pathlib.Path(args.data)
    history = json.loads(path.read_text(encoding="utf-8"))
    stocks = history["stocks"]

    end = parse_date(args.end) if args.end else dt.date.today()

    if args.start:
        start = parse_date(args.start)
    else:
        # resume from the last day that already has a 投信 ratio, so the
        # daily run only fetches the tail instead of rewriting history
        known = [d[0] for s in stocks.values() for d in s["days"]
                 if len(d) > 8 and d[8] is not None]
        start = (parse_date(max(known)) if known
                 else end - dt.timedelta(days=30))
    # MoneyDJ 自訂區間僅提供一年內資料
    start = max(start, end - dt.timedelta(days=360))
    print(f"[moneydj] range {start} ~ {end}")

    patched = total = 0
    for code in common.STOCK_CODES:
        if code not in stocks:
            continue
        rows = fetch_stock(code, start, end)
        n = 0
        for day in stocks[code]["days"]:
            if not (start.strftime("%Y%m%d") <= day[0] <= end.strftime("%Y%m%d")):
                continue
            hit = rows.get(day[0])
            if not hit:
                continue
            t_hold, d_hold, f_ratio = hit
            while len(day) < 11:
                day.append(None)
            f = day[7] if day[7] is not None else f_ratio
            day[8], day[9] = t_hold, d_hold
            day[10] = round(f + t_hold + d_hold, 2)
            n += 1
        print(f"  [{code}] {stocks[code]['name']}: {len(rows)} rows fetched, {n} patched")
        patched += n
        total += len(rows)
        time.sleep(args.sleep)

    path.write_text(json.dumps(history, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"[moneydj] DONE → {path} ({patched} day rows patched)")


if __name__ == "__main__":
    main()

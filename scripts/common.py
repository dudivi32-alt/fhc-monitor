"""Shared constants and helpers for the 13家金控 data pipeline.

Data sources:
- FinMind (https://api.finmindtrade.com) for stock price, institutional
  buy/sell, and foreign shareholding ratio. No API token required for
  these datasets at the volumes this project uses.
- TWSE MI_INDEX (https://www.twse.com.tw/exchangeReport/MI_INDEX) for the
  TAIEX (加權指數) and 金融保險類指數 closing values.
"""
from __future__ import annotations

import datetime as dt
import time
import urllib.error
import urllib.parse
import urllib.request
import json

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"
TWSE_MI_INDEX = "https://www.twse.com.tw/exchangeReport/MI_INDEX"

# 13家金控，代號: 名稱（順序沿用現有頁面的 stock-selector）
STOCKS = [
    ("2880", "華南金"),
    ("2881", "富邦金"),
    ("2882", "國泰金"),
    ("2883", "凱基金"),
    ("2884", "玉山金"),
    ("2885", "元大金"),
    ("2886", "兆豐金"),
    ("2887", "台新新光金"),
    ("2889", "國票金"),
    ("2890", "永豐金"),
    ("2891", "中信金"),
    ("2892", "第一金"),
    ("5880", "合庫金"),
]
STOCK_CODES = [c for c, _ in STOCKS]
STOCK_NAMES = dict(STOCKS)

# 歷史資料回溯起始日（HISTORY 陣列的最早日期）
HISTORY_START_DATE = "2025-01-01"

# 外資持股 ≥ 此門檻視為「高持股族」（每日觀察用）
HIGH_HOLD_THRESHOLD = 25.0

FOREIGN_NAMES = {"Foreign_Investor", "Foreign_Dealer_Self"}
TRUST_NAMES = {"Investment_Trust"}
DEALER_NAMES = {"Dealer_self", "Dealer_Hedging"}


def _http_get_json(url: str, params: dict, retries: int = 3, timeout: int = 20) -> dict:
    qs = urllib.parse.urlencode(params)
    full_url = f"{url}?{qs}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full_url, headers={"User-Agent": "fhc-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {full_url} failed after {retries} retries: {last_err}")


def fetch_finmind(dataset: str, data_id: str, start_date: str, end_date: str | None = None) -> list[dict]:
    """Call a FinMind v4 dataset endpoint and return the `data` list."""
    params = {"dataset": dataset, "data_id": data_id, "start_date": start_date}
    if end_date:
        params["end_date"] = end_date
    payload = _http_get_json(FINMIND_BASE, params)
    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind {dataset}/{data_id} error: {payload.get('msg')}")
    return payload.get("data", [])


def fetch_twse_index(date_yyyymmdd: str) -> dict | None:
    """Fetch TAIEX + 金融保險類指數 close for one date.

    Returns {"taiex": float, "fin": float} or None if the market was
    closed that day (TWSE returns an empty table).
    """
    payload = _http_get_json(TWSE_MI_INDEX, {"response": "json", "date": date_yyyymmdd, "type": "IND"})
    tables = payload.get("tables") or []
    taiex = fin = None
    for table in tables:
        if "價格指數(臺灣證券交易所)" in table.get("title", ""):
            for row in table.get("data", []):
                name = row[0]
                if name == "發行量加權股價指數":
                    taiex = float(row[1].replace(",", ""))
                elif name == "金融保險類指數":
                    fin = float(row[1].replace(",", ""))
    if taiex is None or fin is None:
        return None
    return {"taiex": taiex, "fin": fin}


def find_latest_index_date(start_date_yyyymmdd: str) -> tuple[str, dict]:
    """Walk backwards from start_date until MI_INDEX returns data."""
    d = dt.datetime.strptime(start_date_yyyymmdd, "%Y%m%d")
    for _ in range(10):
        ds = d.strftime("%Y%m%d")
        result = fetch_twse_index(ds)
        if result:
            return ds, result
        d -= dt.timedelta(days=1)
    raise RuntimeError(f"No MI_INDEX data found within 10 days before {start_date_yyyymmdd}")


def split_institutional(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Group FinMind institutional buy/sell rows by date -> {fL, tL, dL, totL} in 張 (net, shares/1000)."""
    by_date: dict[str, dict[str, float]] = {}
    for row in rows:
        date = row["date"].replace("-", "")
        net = (row["buy"] - row["sell"]) / 1000.0
        bucket = by_date.setdefault(date, {"fL": 0.0, "tL": 0.0, "dL": 0.0})
        if row["name"] in FOREIGN_NAMES:
            bucket["fL"] += net
        elif row["name"] in TRUST_NAMES:
            bucket["tL"] += net
        elif row["name"] in DEALER_NAMES:
            bucket["dL"] += net
    for bucket in by_date.values():
        bucket["totL"] = bucket["fL"] + bucket["tL"] + bucket["dL"]
    return by_date


def fmt_signed(n: float) -> str:
    if n is None:
        return "—"
    r = round(n)
    if r == 0:
        return "0"
    return f"{'+' if r > 0 else ''}{r:,}"


def fmt_pct(n: float) -> str:
    if n is None:
        return "—"
    return f"{'+' if n > 0 else ''}{n:.2f}%"


def cls_of(n: float) -> str:
    if n is None or n == 0:
        return "neu"
    return "pos" if n > 0 else "neg"


# 重點觀察的視角個股（focus 區塊）
FOCUS_CODE = "2883"


def _momentum_label(p1_tot: float, p3_tot: float, p5_tot: float) -> str:
    """Describe 1/3/5-day net buy/sell momentum for the focus stock."""
    s1, s3, s5 = (1 if v > 0 else (-1 if v < 0 else 0) for v in (p1_tot, p3_tot, p5_tot))
    if s1 >= 0 and s3 > 0 and s5 > 0:
        return "連續買超" + ("、力道加強" if p1_tot > p3_tot / 3 else "、力道趨緩")
    if s1 <= 0 and s3 < 0 and s5 < 0:
        return "連續賣超" + ("、力道加強" if p1_tot < p3_tot / 3 else "、力道趨緩")
    if s1 > 0 and s5 <= 0:
        return "短線由賣轉買"
    if s1 < 0 and s5 >= 0:
        return "短線由買轉賣"
    return "多空反覆"


def build_focus(focus: dict, idx: dict, rows: list[dict] | None = None) -> list:
    """Build the single '凱基金視角' observation block covering:
    (1) TAIEX vs 金融保險指數, (2) focus-stock 1/3/5-day flows,
    (3) 金控同業重點觀察 (peer comparison).

    `focus` = {"name","code","p1","p3","p5"} (period metric dicts from build_index)
    `idx`   = {"taiex": {1: pct, 3: pct, 5: pct}, "fin": {...}}
    `rows`  = p1 rows for all 13 stocks ({"code","name","pct","fL","tL","totL",...})
    Returns [(cls, html), ...] like the other observation lists.
    """
    out = []
    name = focus["name"]

    # (1) 加權指數 vs 金融保險指數（當日/3日/5日）
    spread = {n: (idx["fin"][n] - idx["taiex"][n]) if (idx["fin"].get(n) is not None and idx["taiex"].get(n) is not None) else None
              for n in (1, 3, 5)}
    sp1 = spread[1]
    verdict = "金融族群相對大盤強勢" if (sp1 or 0) > 0 else ("金融族群相對大盤弱勢" if (sp1 or 0) < 0 else "金融族群與大盤同步")
    idx_parts = "、".join(
        f'{lbl} <span class="{cls_of(spread[n])}">{fmt_pct(spread[n]) if spread[n] is not None else "—"}</span>'
        for n, lbl in ((1, "當日"), (3, "3日"), (5, "5日")))
    out.append((cls_of(sp1),
        f'<span class="obs-label">指數對比</span>金融保險指數相對加權指數：{idx_parts} — <b class="{cls_of(sp1)}">{verdict}</b>'
        f'（加權 {fmt_pct(idx["taiex"][1])} / 金融 {fmt_pct(idx["fin"][1])}）'))

    # (2) 凱基金 股價與 當日/3日/5日 買賣超（含同業排名）
    p1, p3, p5 = focus["p1"], focus["p3"], focus["p5"]
    rank_pct = rank_tot = None
    if rows:
        rank_pct = sorted(rows, key=lambda r: -r["pct"]).index(
            next(r for r in rows if r["code"] == focus["code"])) + 1
        rank_tot = sorted(rows, key=lambda r: -r["totL"]).index(
            next(r for r in rows if r["code"] == focus["code"])) + 1
    rank_pct_txt = f'（13家中第 <b>{rank_pct}</b> 強）' if rank_pct else ''
    rank_tot_txt = f'（買超排名第 <b>{rank_tot}</b>）' if rank_tot else ''
    out.append((cls_of(p1["pct"]),
        f'<span class="obs-label">股價表現</span>{name} 收盤 <b>{p1["close"]:.2f}</b>，'
        f'當日 <b class="{cls_of(p1["pct"])}">{fmt_pct(p1["pct"])}</b>{rank_pct_txt}、'
        f'3日 <span class="{cls_of(p3["pct"])}">{fmt_pct(p3["pct"])}</span>、'
        f'5日 <span class="{cls_of(p5["pct"])}">{fmt_pct(p5["pct"])}</span>'))

    momentum = _momentum_label(p1["totL"], p3["totL"], p5["totL"])
    out.append((cls_of(p1["totL"]),
        f'<span class="obs-label">法人動向</span>{name} 法人合計買賣超：'
        f'當日 <b class="{cls_of(p1["totL"])}">{fmt_signed(p1["totL"])}</b> 張{rank_tot_txt}、'
        f'3日 <span class="{cls_of(p3["totL"])}">{fmt_signed(p3["totL"])}</span> 張、'
        f'5日 <span class="{cls_of(p5["totL"])}">{fmt_signed(p5["totL"])}</span> 張 — <b>{momentum}</b>'))

    out.append(("neu",
        f'<span class="obs-label">三大法人</span>外資 當日 <span class="{cls_of(p1["fL"])}">{fmt_signed(p1["fL"])}</span>'
        f' / 3日 <span class="{cls_of(p3["fL"])}">{fmt_signed(p3["fL"])}</span>'
        f' / 5日 <span class="{cls_of(p5["fL"])}">{fmt_signed(p5["fL"])}</span> 張；'
        f'投信 當日 <span class="{cls_of(p1["tL"])}">{fmt_signed(p1["tL"])}</span>'
        f' / 3日 <span class="{cls_of(p3["tL"])}">{fmt_signed(p3["tL"])}</span>'
        f' / 5日 <span class="{cls_of(p5["tL"])}">{fmt_signed(p5["tL"])}</span> 張；'
        f'自營商 當日 <span class="{cls_of(p1["dL"])}">{fmt_signed(p1["dL"])}</span>'
        f' / 3日 <span class="{cls_of(p3["dL"])}">{fmt_signed(p3["dL"])}</span>'
        f' / 5日 <span class="{cls_of(p5["dL"])}">{fmt_signed(p5["dL"])}</span> 張'))

    # (3) 金控同業重點觀察
    if rows:
        peers = [r for r in rows if r["code"] != focus["code"]]
        sum_tot = sum(r["totL"] for r in rows)
        sum_f = sum(r["fL"] for r in rows)
        sum_t = sum(r["tL"] for r in rows)
        n_buy = sum(1 for r in rows if r["totL"] > 0)
        n_sell = sum(1 for r in rows if r["totL"] < 0)
        overall = "偏多" if sum_tot > 0 else ("偏空" if sum_tot < 0 else "持平")
        leader = max(peers, key=lambda r: r["pct"])
        laggard = min(peers, key=lambda r: r["pct"])
        top_buy = max(peers, key=lambda r: r["totL"])
        top_sell = min(peers, key=lambda r: r["totL"])
        out.append((cls_of(sum_tot),
            f'<span class="obs-label">同業風向</span>13家金控法人合計 '
            f'<b class="{cls_of(sum_tot)}">{fmt_signed(sum_tot)}</b> 張'
            f'（<b class="pos">{n_buy} 檔買超</b>、<b class="neg">{n_sell} 檔賣超</b>，整體 <b class="{cls_of(sum_tot)}">{overall}</b>；'
            f'外資 <span class="{cls_of(sum_f)}">{fmt_signed(sum_f)}</span> 張、'
            f'投信 <span class="{cls_of(sum_t)}">{fmt_signed(sum_t)}</span> 張）'))
        out.append(("neu",
            f'<span class="obs-label">同業焦點</span>同業領漲 <b class="{cls_of(leader["pct"])}">{leader["name"]} {fmt_pct(leader["pct"])}</b>、'
            f'領跌 <b class="{cls_of(laggard["pct"])}">{laggard["name"]} {fmt_pct(laggard["pct"])}</b>；'
            f'法人最捧 <b class="pos">{top_buy["name"]}（{fmt_signed(top_buy["totL"])} 張）</b>、'
            f'最棄 <b class="neg">{top_sell["name"]}（{fmt_signed(top_sell["totL"])} 張）</b>'))
        if rank_pct and rank_tot:
            half = len(rows) / 2
            stance = ("表現與籌碼同步領先同業" if (rank_pct <= half and rank_tot <= half) else
                      ("股價落後但法人先行卡位" if rank_tot <= half else
                       ("股價強但籌碼未跟上，留意追價風險" if rank_pct <= half else "表現與籌碼皆落後同業")))
            out.append((cls_of(half - max(rank_pct, rank_tot) + 0.5),
                f'<span class="obs-label">同業對照</span>{name} 當日漲幅第 <b>{rank_pct}</b>/13、'
                f'法人買超第 <b>{rank_tot}</b>/13 — <b>{stance}</b>'))

    return out

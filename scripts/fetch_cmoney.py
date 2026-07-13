"""Fetch full history for the 13 FHC stocks + indices from CMoney FarenMCP.

Talks MCP streamable-http (JSON-RPC) to the CMoney FarenMCP server and
calls its execute_sql tool in date-range batches (the server injects
TOP 2000 per query), then writes the same history.json structure the
FinMind pipeline uses, to data/history_cmoney.json.

Connection settings (URL + X-API-KEY) are read from the local Claude
config (~/.claude.json, mcpServers.faren-mcp) so no secret lives in this
repo. Run: python scripts/fetch_cmoney.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "history_cmoney.json"

START_DATE = "20250101"
TAIEX_CODE = "TWA00"   # 加權指數
FIN_CODE = "TWB28"     # 金融保險類指數
BATCH_DAYS = 100       # ~70 trading days × 13 stocks ≈ 910 rows,安全低於 2000 上限


def load_faren_config() -> tuple[str, dict]:
    """Read FarenMCP url + auth headers from ~/.claude.json (not committed)."""
    cfg_path = Path.home() / ".claude.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    server = cfg.get("mcpServers", {}).get("faren-mcp")
    if not server or not server.get("url"):
        raise SystemExit("faren-mcp not found in ~/.claude.json mcpServers")
    return server["url"], server.get("headers", {})


class McpClient:
    def __init__(self, url: str, extra_headers: dict | None = None):
        self.url = url
        self.extra_headers = extra_headers or {}
        self.session_id: str | None = None
        self._req_id = 0

    def _post(self, payload: dict) -> tuple[dict | None, dict]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.extra_headers,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_headers = dict(resp.headers)
            body = resp.read().decode("utf-8")
        sid = resp_headers.get("mcp-session-id") or resp_headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if not body.strip():
            return None, resp_headers
        # streamable-http may reply as SSE ("event: message\ndata: {...}") or plain JSON
        if body.lstrip().startswith("{"):
            return json.loads(body), resp_headers
        result = None
        for line in body.splitlines():
            if line.startswith("data:"):
                result = json.loads(line[5:].strip())
        return result, resp_headers

    def _rpc(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self._req_id += 1
            payload["id"] = self._req_id
        result, _ = self._post(payload)
        if result and "error" in result:
            raise RuntimeError(f"MCP {method} error: {result['error']}")
        return result

    def initialize(self) -> None:
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "fhc-monitor-fetch", "version": "1.0"},
        })
        self._rpc("notifications/initialized", {}, notify=True)

    def execute_sql(self, query: str, conversation: str) -> list[dict]:
        result = self._rpc("tools/call", {
            "name": "execute_sql",
            "arguments": {"query": query, "conversation": conversation},
        })
        content = result["result"]["content"]
        text = "".join(c["text"] for c in content if c.get("type") == "text")
        payload = json.loads(text)
        if not payload.get("success"):
            raise RuntimeError(f"execute_sql failed: {payload}")
        return payload["data"]


def date_batches(start: str, end: str, step_days: int):
    d0 = dt.datetime.strptime(start, "%Y%m%d").date()
    d1 = dt.datetime.strptime(end, "%Y%m%d").date()
    cur = d0
    while cur <= d1:
        nxt = min(cur + dt.timedelta(days=step_days - 1), d1)
        yield cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")
        cur = nxt + dt.timedelta(days=1)


CONVERSATION_NOTE = (
    "使用者已核准：13家金控監測網頁資料源切換至CMoney，"
    "批次抓取2025-01-01起13檔金控收盤價/成交量/法人買賣超/外資持股與加權、金融保險指數歷史。"
)


def backfill_holdings(start: str, end: str) -> None:
    """Patch existing day rows in [start, end] with 投信/自營商/法人 持股比率.

    Queries only 日法人持股估計. Mind the 10000-row daily quota: one year
    of 13 stocks ≈ 3.2k rows, so backfill at most ~3 years per day.
    Day rows are extended from [.., fHold] to [.., fHold, tHold, dHold, totHold]."""
    if not OUT_PATH.exists():
        raise SystemExit(f"{OUT_PATH} not found — run a full fetch first")
    history = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    codes_in = ",".join(f"'{c}'" for c in common.STOCK_CODES)

    url, headers = load_faren_config()
    client = McpClient(url, headers)
    client.initialize()

    hold_map: dict[tuple[str, str], dict] = {}
    # 批次不可過大：~70% 為交易日，13檔 × 交易日數必須 < 單次查詢 2000 筆上限
    for b_start, b_end in date_batches(start, end, BATCH_DAYS * 2):
        print(f"[backfill] batch {b_start}~{b_end} ...", flush=True)
        for r in client.execute_sql(
            f"SELECT 日期, 股票代號, [投信持股比率(%)], [自營商持股比率(%)], [法人持股比率(%)] "
            f"FROM 日法人持股估計 "
            f"WHERE 股票代號 IN ({codes_in}) AND 日期 BETWEEN '{b_start}' AND '{b_end}'",
            CONVERSATION_NOTE,
        ):
            hold_map[(r["日期"], r["股票代號"])] = r

    patched = 0
    for code in common.STOCK_CODES:
        for day in history["stocks"][code]["days"]:
            if not (start <= day[0] <= end):
                continue
            r = hold_map.get((day[0], code))
            vals = [
                r.get("投信持股比率(%)") if r else None,
                r.get("自營商持股比率(%)") if r else None,
                r.get("法人持股比率(%)") if r else None,
            ]
            day[8:] = vals
            if r:
                patched += 1
    OUT_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[backfill] patched {patched} day rows in {OUT_PATH}")


def main() -> None:
    if "--backfill-holdings" in sys.argv:
        i = sys.argv.index("--backfill-holdings")
        args = sys.argv[i + 1:i + 3]
        start = args[0] if len(args) > 0 else START_DATE
        end = args[1] if len(args) > 1 else dt.date.today().strftime("%Y%m%d")
        backfill_holdings(start, end)
        return
    full = "--full" in sys.argv
    today = dt.date.today().strftime("%Y%m%d")
    codes_in = ",".join(f"'{c}'" for c in common.STOCK_CODES)

    # 增量模式（預設）：沿用既有檔案，只抓最後一個指數日期之後的新資料，
    # 避免撞到 CMoney 單日查詢筆數上限（10000 筆）。--full 才全量重抓。
    history: dict
    fetch_start = START_DATE
    if not full and OUT_PATH.exists():
        history = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        history.setdefault("amounts", {code: {} for code in common.STOCK_CODES})
        if history.get("index"):
            last = history["index"][-1]["d"]
            fetch_start = (dt.datetime.strptime(last, "%Y%m%d").date()
                           + dt.timedelta(days=1)).strftime("%Y%m%d")
        print(f"[fetch_cmoney] incremental mode: fetching from {fetch_start}")
        if fetch_start > today:
            print("[fetch_cmoney] already up to date, nothing to fetch")
            return
    else:
        history = {
            "stocks": {code: {"name": name, "days": []} for code, name in common.STOCKS},
            "index": [],
            "amounts": {code: {} for code in common.STOCK_CODES},
        }
        print(f"[fetch_cmoney] full mode: fetching from {fetch_start}")

    url, headers = load_faren_config()
    client = McpClient(url, headers)
    client.initialize()

    price_rows: list[dict] = []
    inst_rows: list[dict] = []
    for b_start, b_end in date_batches(fetch_start, today, BATCH_DAYS):
        print(f"[fetch_cmoney] batch {b_start}~{b_end} ...", flush=True)
        price_rows += client.execute_sql(
            f"SELECT 日期, 股票代號, 收盤價, 成交量 FROM 日收盤表排行 "
            f"WHERE 股票代號 IN ({codes_in}) AND 日期 BETWEEN '{b_start}' AND '{b_end}' "
            f"ORDER BY 日期, 股票代號",
            CONVERSATION_NOTE,
        )
        inst_rows += client.execute_sql(
            f"SELECT 日期, 股票代號, 外資買賣超, 投信買賣超, 自營商買賣超, 買賣超合計, "
            f"[外資持股比率(%)], [投信持股比率(%)], [自營商持股比率(%)], [法人持股比率(%)], "
            f"[外資買賣超金額(千)], [投信買賣超金額(千)], "
            f"[自營商買賣超金額(千)], [法人買賣超金額(千)] FROM 日法人持股估計 "
            f"WHERE 股票代號 IN ({codes_in}) AND 日期 BETWEEN '{b_start}' AND '{b_end}' "
            f"ORDER BY 日期, 股票代號",
            CONVERSATION_NOTE,
        )

    idx_rows = client.execute_sql(
        f"SELECT 日期, 股票代號, 收盤價 FROM 日收盤表排行 "
        f"WHERE 股票代號 IN ('{TAIEX_CODE}','{FIN_CODE}') AND 日期 >= '{fetch_start}' "
        f"ORDER BY 日期",
        CONVERSATION_NOTE,
    )

    print(f"[fetch_cmoney] rows: price={len(price_rows)} inst={len(inst_rows)} index={len(idx_rows)}")

    inst_map = {(r["日期"], r["股票代號"]): r for r in inst_rows}
    existing_dates = {
        code: {d[0] for d in history["stocks"][code]["days"]}
        for code in common.STOCK_CODES
    }
    for r in price_rows:
        code, date = r["股票代號"], r["日期"]
        if r["收盤價"] is None or date in existing_dates[code]:
            continue
        inst = inst_map.get((date, code), {})
        fL = inst.get("外資買賣超") or 0.0
        tL = inst.get("投信買賣超") or 0.0
        dL = inst.get("自營商買賣超") or 0.0
        totL = inst.get("買賣超合計")
        if totL is None:
            totL = fL + tL + dL
        history["stocks"][code]["days"].append([
            date, r["收盤價"], r["成交量"] or 0.0, fL, tL, dL, totL,
            inst.get("外資持股比率(%)"), inst.get("投信持股比率(%)"),
            inst.get("自營商持股比率(%)"), inst.get("法人持股比率(%)"),
        ])
        # 真實買賣超金額（千元→百萬），供金額模式顯示,比用收盤價估算精確
        if inst.get("外資買賣超金額(千)") is not None:
            history["amounts"][code][date] = [
                inst["外資買賣超金額(千)"] / 1000.0,
                (inst.get("投信買賣超金額(千)") or 0) / 1000.0,
                (inst.get("自營商買賣超金額(千)") or 0) / 1000.0,
                (inst.get("法人買賣超金額(千)") or 0) / 1000.0,
            ]

    idx_by_date: dict[str, dict] = {}
    for r in idx_rows:
        if r["收盤價"] is None:
            continue
        e = idx_by_date.setdefault(r["日期"], {"d": r["日期"]})
        e["taiex" if r["股票代號"] == TAIEX_CODE else "fin"] = r["收盤價"]
    existing_idx_dates = {e["d"] for e in history["index"]}
    history["index"] += [
        e for e in idx_by_date.values()
        if "taiex" in e and "fin" in e and e["d"] not in existing_idx_dates
    ]
    history["index"].sort(key=lambda e: e["d"])

    for code in common.STOCK_CODES:
        history["stocks"][code]["days"].sort(key=lambda d: d[0])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    n_days = len(history["stocks"][common.STOCK_CODES[0]]["days"])
    print(f"[fetch_cmoney] wrote {OUT_PATH} ({n_days} trading days, {len(history['index'])} index days)")


if __name__ == "__main__":
    main()

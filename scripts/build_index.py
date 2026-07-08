"""Render a static snapshot page from a persisted history JSON file.

Reads the per-stock daily history, computes the same DATA/HISTORY JS
structures the original hand-built page used, generates the "每日觀察"
text, and fills scripts/template.html.

Usage:
  python scripts/build_index.py                       # FinMind history -> archive/index.html
  python scripts/build_index.py --data data/history_cmoney.json --out index.html \
      --source "CMoney（日收盤表排行、日法人持股估計）"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = Path(__file__).parent / "template.html"

DEFAULT_SOURCE = "FinMind（個股股價、法人買賣超、外資持股）、TWSE MI_INDEX（指數）"


def period_metrics(days: list[list], period: int, amounts: dict | None = None) -> dict | None:
    """days sorted ascending, each [date, close, vol, fL, tL, dL, totL, fHold].
    Returns p{period} metrics using the last `period` trading days. Degrades
    gracefully during history bootstrap (fewer than period+1 days on record)
    instead of dropping the stock entirely.

    `amounts` maps date -> [fM, tM, dM, totM] (實際買賣超金額, 百萬). When a
    window is fully covered, real amounts are used for fK/tK/dK/totK instead
    of the close-price estimate."""
    if not days:
        return None
    if len(days) > period:
        window = days[-period:]
        base = days[-period - 1]
    else:
        window = days
        base = days[0]
    close = window[-1][1]
    pct = (close - base[1]) / base[1] * 100.0
    vol = sum(d[2] for d in window)
    fL = sum(d[3] for d in window)
    tL = sum(d[4] for d in window)
    dL = sum(d[5] for d in window)
    totL = sum(d[6] for d in window)
    if amounts and all(d[0] in amounts for d in window):
        fK = sum(amounts[d[0]][0] for d in window)
        tK = sum(amounts[d[0]][1] for d in window)
        dK = sum(amounts[d[0]][2] for d in window)
        totK = sum(amounts[d[0]][3] for d in window)
    else:
        fK = sum(d[3] * d[1] / 1000.0 for d in window)
        tK = sum(d[4] * d[1] / 1000.0 for d in window)
        dK = sum(d[5] * d[1] / 1000.0 for d in window)
        totK = fK + tK + dK
    return {
        "close": close, "pct": pct, "vol": vol,
        "fL": fL, "tL": tL, "dL": dL, "totL": totL,
        "fK": fK, "tK": tK, "dK": dK, "totK": totK,
        "fHold": window[-1][7],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(REPO_ROOT / "data" / "history.json"))
    parser.add_argument("--out", default=str(REPO_ROOT / "archive" / "index.html"))
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    args = parser.parse_args()

    history_path = Path(args.data)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path

    if not history_path.exists():
        raise SystemExit(f"{history_path} not found — run the fetch step first")
    history = json.loads(history_path.read_text(encoding="utf-8"))
    all_amounts = history.get("amounts", {})

    all_results = []
    obs_rows = []
    for code, name in common.STOCKS:
        days = history["stocks"].get(code, {}).get("days", [])
        if not days:
            continue
        amounts = all_amounts.get(code)
        p1 = period_metrics(days, 1, amounts)
        p3 = period_metrics(days, 3, amounts)
        p5 = period_metrics(days, 5, amounts)
        if not p1:
            continue
        all_results.append({"code": code, "name": name, "p1": p1, "p3": p3, "p5": p5})
        prev_hold = days[-2][7] if len(days) >= 2 else None
        obs_rows.append({
            "code": code, "name": name,
            "close": p1["close"], "pct": p1["pct"],
            "fL": p1["fL"], "tL": p1["tL"], "dL": p1["dL"], "totL": p1["totL"],
            "fHold": p1["fHold"], "fHoldPrev": prev_hold,
        })

    idx_history = history["index"]
    if not idx_history:
        raise SystemExit(f"{history_path} has no index history yet")
    latest_date = idx_history[-1]["d"]

    # 單一觀察區塊（凱基金視角）：指數對比 + 焦點個股 1/3/5 日買賣超 + 金控同業重點
    focus_html = ""
    focus_result = next((r for r in all_results if r["code"] == common.FOCUS_CODE), None)
    if focus_result and len(idx_history) >= 2:
        def idx_pct(key: str, n: int) -> float | None:
            if len(idx_history) <= n:
                return None
            cur, base = idx_history[-1], idx_history[-1 - n]
            return (cur[key] - base[key]) / base[key] * 100.0
        idx_changes = {key: {n: idx_pct(key, n) for n in (1, 3, 5)} for key in ("taiex", "fin")}
        focus_items = common.build_focus(focus_result, idx_changes, obs_rows)
        focus_lis = "".join(f'<li class="{c}">{html}</li>' for c, html in focus_items)
        focus_html = (f'<div class="obs-group"><div class="obs-group-title">🎯 {focus_result["name"]}視角</div>'
                      f'<ul class="obs-list">{focus_lis}</ul></div>')

    data_json = {"all_results": all_results, "indexHistory": idx_history}
    history_json = {
        code: {"name": info["name"], "days": info["days"]}
        for code, info in history["stocks"].items()
    }

    date_display = f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:]}"
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    out = (template
           .replace("{{DATE}}", date_display)
           .replace("{{SOURCE}}", args.source)
           .replace("{{OBS_FOCUS}}", focus_html)
           .replace("{{HISTORY_JSON}}", json.dumps(history_json, ensure_ascii=False))
           .replace("{{DATA_JSON}}", json.dumps(data_json, ensure_ascii=False)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    print(f"[build_index] wrote {out_path} for date {latest_date}")


if __name__ == "__main__":
    main()

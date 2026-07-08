"""Render archive/index.html (static snapshot) from data/history.json.

Reads the persisted per-stock daily history, computes the same
DATA/HISTORY JS structures the original hand-built page used, generates
the "每日觀察" text, and fills scripts/template.html.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = REPO_ROOT / "data" / "history.json"
TEMPLATE_PATH = Path(__file__).parent / "template.html"
OUT_PATH = REPO_ROOT / "archive" / "index.html"


def period_metrics(days: list[list], period: int) -> dict | None:
    """days sorted ascending, each [date, close, vol, fL, tL, dL, totL, fHold].
    Returns p{period} metrics using the last `period` trading days. Degrades
    gracefully during history bootstrap (fewer than period+1 days on record)
    instead of dropping the stock entirely."""
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
    if not HISTORY_PATH.exists():
        raise SystemExit(f"{HISTORY_PATH} not found — run fetch_today.py + update_history.py first")
    history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))

    all_results = []
    obs_rows = []
    for code, name in common.STOCKS:
        days = history["stocks"].get(code, {}).get("days", [])
        if not days:
            continue
        p1 = period_metrics(days, 1)
        p3 = period_metrics(days, 3)
        p5 = period_metrics(days, 5)
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
        raise SystemExit("data/history.json has no index history yet")
    latest_date = idx_history[-1]["d"]
    taiex_pct = fin_pct = 0.0
    if len(idx_history) >= 2:
        cur, prev = idx_history[-1], idx_history[-2]
        taiex_pct = (cur["taiex"] - prev["taiex"]) / prev["taiex"] * 100.0
        fin_pct = (cur["fin"] - prev["fin"]) / prev["fin"] * 100.0

    obs = common.build_observations(obs_rows, taiex_pct, fin_pct)
    events_html = "\n".join(f'<li class="{c}">{html}</li>' for c, html in obs["events"]) + "\n"
    judgement_html = "".join(f'<li class="{c}">{html}</li>' for c, html in obs["judgement"])

    data_json = {"all_results": all_results, "indexHistory": idx_history}
    history_json = {
        code: {"name": info["name"], "days": info["days"]}
        for code, info in history["stocks"].items()
    }

    date_display = f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:]}"
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    out = (template
           .replace("{{DATE}}", date_display)
           .replace("{{OBS_EVENTS}}", events_html)
           .replace("{{OBS_JUDGEMENT}}", judgement_html)
           .replace("{{HISTORY_JSON}}", json.dumps(history_json, ensure_ascii=False))
           .replace("{{DATA_JSON}}", json.dumps(data_json, ensure_ascii=False)))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(out, encoding="utf-8")
    print(f"[build_index] wrote {OUT_PATH} for date {latest_date}")


if __name__ == "__main__":
    main()

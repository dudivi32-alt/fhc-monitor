"""Merge data/_today.json (from fetch_today.py) into data/history.json.

data/history.json shape:
{
  "stocks": {
    "<code>": {"name": "...", "days": [[date, close, vol, fL, tL, dL, totL, fHold], ...]},
    ...
  },
  "index": [{"d": "YYYYMMDD", "taiex": float, "fin": float}, ...]
}

Idempotent: re-running with the same _today.json for a date already
present just overwrites that date's row with identical values (no-op
from git's perspective).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TODAY_PATH = REPO_ROOT / "data" / "_today.json"
HISTORY_PATH = REPO_ROOT / "data" / "history.json"


def load_history() -> dict:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    return {
        "stocks": {code: {"name": name, "days": []} for code, name in common.STOCKS},
        "index": [],
    }


def upsert_day(days: list[list], date: str, row: list) -> None:
    for i, d in enumerate(days):
        if d[0] == date:
            days[i] = row
            return
    days.append(row)


def main() -> None:
    if not TODAY_PATH.exists():
        raise SystemExit(f"{TODAY_PATH} not found — run fetch_today.py first")

    today = json.loads(TODAY_PATH.read_text(encoding="utf-8"))
    history = load_history()

    for code, name in common.STOCKS:
        history["stocks"].setdefault(code, {"name": name, "days": []})

    for r in today["rows"]:
        code = r["code"]
        bucket = history["stocks"][code]
        row = [today["date"], r["close"], r["vol"], r["fL"], r["tL"], r["dL"], r["totL"], r["fHold"]]
        upsert_day(bucket["days"], today["date"], row)
        bucket["days"].sort(key=lambda d: d[0])

    idx_entry = {"d": today["date"], "taiex": today["taiex"], "fin": today["fin"]}
    idx_list = history["index"]
    for i, e in enumerate(idx_list):
        if e["d"] == today["date"]:
            idx_list[i] = idx_entry
            break
    else:
        idx_list.append(idx_entry)
    idx_list.sort(key=lambda e: e["d"])

    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[update_history] merged {today['date']} into {HISTORY_PATH}")


if __name__ == "__main__":
    main()

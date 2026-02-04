#!/usr/bin/env python3
"""
Collecte des snapshots de prix pour les events LoL ouverts (Polymarket).
À lancer régulièrement (ex. 1×/jour). Écrit dans data/calibration/snapshots.jsonl.

Usage: python scripts/polymarket_calibration_collect.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
_src_dir = _script_dir.parent / "src"
sys.path.insert(0, str(_src_dir))

from live.polymarket_client import (
    get_events_lol,
    get_prices_for_event,
    _select_winner_market,
)


def main() -> None:
    data_dir = _script_dir.parent / "data" / "calibration"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_file = data_dir / "snapshots.jsonl"

    events = get_events_lol(limit=200, upcoming_only=True, main_leagues_only=False)
    if not events:
        print("Aucun event LoL ouvert.")
        return

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0

    for ev in events:
        slug = ev.get("slug") or ""
        title = (ev.get("title") or "")[:120]
        end_date = ev.get("endDate") or ev.get("end_date") or ""
        markets = ev.get("markets") or []
        market = _select_winner_market(markets)
        if not market:
            continue
        condition_id = market.get("conditionId") or market.get("condition_id") or ""
        if not condition_id:
            continue

        prices = get_prices_for_event(ev)
        if len(prices) < 2:
            continue
        _, book0 = prices[0]
        _, book1 = prices[1]
        ask0 = book0.get("best_ask")
        ask1 = book1.get("best_ask")
        if ask0 is None and ask1 is None:
            continue
        ask0 = round(float(ask0), 4) if ask0 is not None else None
        ask1 = round(float(ask1), 4) if ask1 is not None else None

        row = {
            "event_slug": slug,
            "condition_id": condition_id,
            "outcome_0_ask": ask0,
            "outcome_1_ask": ask1,
            "snapshot_ts": now_iso,
            "end_date_iso": end_date,
            "title": title,
        }
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        count += 1

    print(f"Snapshot: {count} events écrits dans {out_file}")


if __name__ == "__main__":
    main()

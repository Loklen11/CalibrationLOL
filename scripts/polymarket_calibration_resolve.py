#!/usr/bin/env python3
"""
Résout les events dont la date de fin est passée : récupère le gagnant via Gamma
(outcomePrices = [1,0] ou [0,1]) et associe au dernier snapshot avant la fin.
Écrit dans data/calibration/resolved.jsonl. À lancer après la collecte (ex. 1×/jour).

Usage: python scripts/polymarket_calibration_resolve.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
_src_dir = _script_dir.parent / "src"
sys.path.insert(0, str(_src_dir))

from live.polymarket_client import (
    get_event_by_slug,
    _select_winner_market,
    _parse_outcome_prices,
    _parse_iso_ts,
)


def main() -> None:
    data_dir = _script_dir.parent / "data" / "calibration"
    snapshots_file = data_dir / "snapshots.jsonl"
    resolved_file = data_dir / "resolved.jsonl"

    if not snapshots_file.exists():
        print(f"Fichier absent: {snapshots_file}. Lancez d'abord polymarket_calibration_collect.py")
        return

    snapshots: list[dict] = []
    with open(snapshots_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snapshots.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not snapshots:
        print("Aucun snapshot.")
        return

    resolved_slugs: set[str] = set()
    if resolved_file.exists():
        with open(resolved_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    resolved_slugs.add(r.get("event_slug") or "")
                except json.JSONDecodeError:
                    continue

    now_ts = datetime.now(timezone.utc).timestamp()
    by_event: dict[tuple[str, str], list[dict]] = {}
    for s in snapshots:
        slug = s.get("event_slug") or ""
        cid = s.get("condition_id") or ""
        if not slug or not cid:
            continue
        key = (slug, cid)
        if key not in by_event:
            by_event[key] = []
        by_event[key].append(s)

    added = 0
    for (slug, condition_id), rows in by_event.items():
        if slug in resolved_slugs:
            continue
        if not rows:
            continue
        end_date_iso = rows[0].get("end_date_iso") or ""
        end_ts = _parse_iso_ts(end_date_iso)
        if end_ts <= 0 or end_ts > now_ts:
            continue

        try:
            ev = get_event_by_slug(slug)
        except Exception:
            continue
        if not ev:
            continue

        markets = ev.get("markets") or []
        market = _select_winner_market(markets)
        if not market:
            continue
        outcome_prices = _parse_outcome_prices(market)
        if not outcome_prices or len(outcome_prices) < 2:
            continue
        p0, p1 = outcome_prices[0], outcome_prices[1]
        if abs(p0 - 1) < 0.01 and abs(p1) < 0.01:
            winner_index = 0
        elif abs(p1 - 1) < 0.01 and abs(p0) < 0.01:
            winner_index = 1
        else:
            continue

        rows_sorted = sorted(rows, key=lambda r: _parse_iso_ts(r.get("snapshot_ts") or ""), reverse=True)
        snapshot = rows_sorted[0]
        price0 = snapshot.get("outcome_0_ask")
        price1 = snapshot.get("outcome_1_ask")
        if price0 is None and price1 is None:
            continue
        price0 = float(price0) if price0 is not None else None
        price1 = float(price1) if price1 is not None else None

        resolved_row = {
            "event_slug": slug,
            "condition_id": condition_id,
            "price_outcome_0": price0,
            "price_outcome_1": price1,
            "winner_index": winner_index,
            "snapshot_ts": snapshot.get("snapshot_ts"),
            "title": (snapshot.get("title") or "")[:120],
        }
        with open(resolved_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(resolved_row, ensure_ascii=False) + "\n")
        resolved_slugs.add(slug)
        added += 1

    print(f"Résolution: {added} nouvel(s) event(s) ajouté(s) à {resolved_file}")


if __name__ == "__main__":
    main()

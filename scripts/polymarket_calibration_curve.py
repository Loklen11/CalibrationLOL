#!/usr/bin/env python3
"""
Construit la courbe de calibration : tranches de prix (ex. 50–55c) → taux de victoire réel.
Lit data/calibration/resolved.jsonl, affiche la courbe et sauve data/calibration/curve.json.

Usage: python scripts/polymarket_calibration_curve.py
"""

import json
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
_src_dir = _script_dir.parent / "src"
sys.path.insert(0, str(_src_dir))


def main() -> None:
    data_dir = _script_dir.parent / "data" / "calibration"
    resolved_file = data_dir / "resolved.jsonl"
    curve_file = data_dir / "curve.json"

    if not resolved_file.exists():
        print(f"Fichier absent: {resolved_file}. Lancez d'abord collect + resolve.")
        return

    samples: list[tuple[float, int]] = []
    with open(resolved_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            winner = r.get("winner_index", 0)
            p0 = r.get("price_outcome_0")
            p1 = r.get("price_outcome_1")
            if p0 is not None:
                try:
                    p = float(p0)
                    if 0.01 <= p <= 0.99:
                        samples.append((p, 1 if winner == 0 else 0))
                except (TypeError, ValueError):
                    pass
            if p1 is not None:
                try:
                    p = float(p1)
                    if 0.01 <= p <= 0.99:
                        samples.append((p, 1 if winner == 1 else 0))
                except (TypeError, ValueError):
                    pass

    if not samples:
        print("Aucune donnée résolue (prix valides). Lancez collect + resolve puis attendez des matchs terminés.")
        return

    bin_size = 0.05
    bins: dict[str, list[int]] = {}
    for p, won in samples:
        low = int(p / bin_size) * bin_size
        high = low + bin_size
        key = f"{int(low*100)}-{int(high*100)}c"
        if key not in bins:
            bins[key] = [0, 0]
        bins[key][0] += won
        bins[key][1] += 1

    curve = []
    for key in sorted(bins.keys(), key=lambda k: int(k.split("-")[0].replace("c", ""))):
        wins, total = bins[key]
        wr = round(wins / total, 4) if total else 0
        curve.append({"bin": key, "wins": wins, "total": total, "win_rate": wr})
        print(f"  {key}: {wins}/{total} = {wr*100:.1f}%")

    with open(curve_file, "w", encoding="utf-8") as f:
        json.dump({"curve": curve, "n_samples": len(samples)}, f, indent=2, ensure_ascii=False)

    print(f"\nCourbe enregistrée dans {curve_file} ({len(samples)} points)")


if __name__ == "__main__":
    main()

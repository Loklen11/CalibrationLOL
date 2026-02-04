# Données de calibration

- **snapshots.jsonl** : prix (best ask) des events LoL ouverts, enregistrés à chaque collecte.
- **resolved.jsonl** : events résolus (prix avant fin + gagnant).
- **curve.json** : courbe prix → taux de victoire (générée par `polymarket_calibration_curve.py`).

Lancer depuis la racine du projet CalibrationLOL :
- `python scripts/polymarket_calibration_collect.py`
- `python scripts/polymarket_calibration_resolve.py`
- `python scripts/polymarket_calibration_curve.py`

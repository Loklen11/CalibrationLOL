# CalibrationLOL

Projet **autonome** pour la calibration des cotes Polymarket (LoL esport) : collecte des prix avant match, résolution après match, courbe prix → taux de victoire réel.

Aucune dépendance au reste du dossier "Bot arbitrage". Tout se fait depuis ce dossier.

## Structure

```
CalibrationLOL/
  scripts/           # collect, resolve, curve
  data/calibration/  # snapshots.jsonl, resolved.jsonl, curve.json
  src/live/          # client API Polymarket (Gamma + CLOB)
  .github/workflows/ # calibration.yml (2×/jour sur GitHub)
```

## En local

```bash
cd CalibrationLOL
pip install -r requirements.txt
python scripts/polymarket_calibration_collect.py
python scripts/polymarket_calibration_resolve.py
python scripts/polymarket_calibration_curve.py
```

## Sur GitHub (sans laisser le PC allumé)

1. Crée un **nouveau repo** sur GitHub (ex. `CalibrationLOL`).
2. Dans le dossier CalibrationLOL :
   ```bash
   git init
   git remote add origin https://github.com/TON-PSEUDO/CalibrationLOL.git
   git add .
   git commit -m "Initial: calibration Polymarket LoL"
   git branch -M main
   git push -u origin main
   ```
3. Onglet **Actions** → *Calibration Polymarket LoL* → **Run workflow**.

Les fichiers `data/calibration/*.jsonl` seront mis à jour automatiquement 2× par jour (8h et 20h UTC) et commités dans le repo.

## Pas de .env ici

Ce projet n’utilise **pas** de clés API (uniquement les API publiques Polymarket). Aucun fichier `.env` à ajouter, donc pas de risque de secret poussé sur GitHub.

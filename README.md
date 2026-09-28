# Kobr4 FX

Bot de trading forex automatisé. Voir [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) pour l'architecture et la feuille de route, et [maquette/](maquette/) pour la maquette du tableau de bord.

**État : backtest opérationnel (phases 0 à 3).** Le bot ne passe encore aucun ordre réel.

## Installation

Prérequis : Python 3.12 et [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Commandes

```bash
uv run kobr4 run --config config/paper.yaml   # vérifie la configuration et démarre
uv run kobr4 data download --symbols EUR/USD,GBP/USD --start 2023-01-01   # historique Dukascopy
uv run kobr4 data import-histdata DAT_ASCII_EURUSD_M1_2024.csv --symbol EUR/USD
uv run kobr4 data info                    # couverture et qualité de l'historique
uv run kobr4 backtest --config config/backtest.yaml --start 2023-01-01   # rapport dans reports/

# Sans données réelles : historique synthétique, pour tester la chaîne uniquement
uv run kobr4 data --store data-synth synth --symbols EUR/USD,GBP/USD,USD/JPY --start 2022-01-01 --end 2024-12-31
uv run kobr4 backtest --config config/backtest.yaml --store data-synth
uv run pytest                             # tests
uv run ruff check . && uv run ruff format --check .   # lint et format
uv run mypy src                           # typage strict
```

## Configuration

- `config/backtest.yaml` : backtest avec le courtier simulé.
- `config/paper.yaml` : compte démo du courtier.
- `config/live.yaml` : compte réel. Jamais versionné ; exige `confirm_live: true`.

La clé API du courtier n'est jamais écrite dans un fichier : elle est lue dans la variable d'environnement indiquée par `broker.api_key_env` (par défaut `KOBR4_BROKER_API_KEY`).

## Historique des prix

`kobr4 data download` télécharge les bougies M1 bid et ask depuis Dukascopy et les stocke dans `data/` (non versionné). Un téléchargement interrompu reprend là où il s'est arrêté : les jours déjà présents sont sautés.

## Organisation

```
src/kobr4/
├── core/     # modèles du domaine, ordres, événements, bus, horloge
├── config/   # chargement et validation de la configuration
├── marketdata/  # historique (Dukascopy, HistData, synthétique), stockage Parquet, bougies
├── strategies/  # indicateurs, croisement EMA, RSI, cassure
├── risk/        # gestionnaire de risque, calendrier des annonces, spreads
├── execution/   # interface courtier, courtier simulé, OMS
├── portfolio/   # positions, solde, équité, drawdown
├── backtest/    # moteur de rejeu, statistiques, rapport HTML
└── app.py    # point d'entrée
```

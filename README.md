# Kobr4 FX

Bot de trading forex automatisé. Voir [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) pour l'architecture et la feuille de route, et [maquette/](maquette/) pour la maquette du tableau de bord.

**État : phase 1 (données).** Le bot ne passe encore aucun ordre.

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
├── marketdata/  # historique (Dukascopy, HistData), stockage Parquet, bougies
└── app.py    # point d'entrée
```

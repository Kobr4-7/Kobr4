# Kobr4 FX

Bot de trading forex automatisé. Voir [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) pour l'architecture et la feuille de route, et [maquette/](maquette/) pour la maquette du tableau de bord.

**État : moteur complet (backtest, laboratoire, bot en direct sur OANDA).** Le bot ne passe encore aucun ordre réel.

## Installation

Prérequis : Python 3.12 et [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Commandes

```bash
uv run kobr4 run --config config/paper.yaml --check   # vérifie la configuration
uv run kobr4 run --config config/paper.yaml           # démarre un bot seul (sans la plateforme)
uv run kobr4 data download --symbols EUR/USD,GBP/USD --start 2023-01-01   # historique Dukascopy
uv run kobr4 data import-histdata DAT_ASCII_EURUSD_M1_2024.csv --symbol EUR/USD
uv run kobr4 data info                    # couverture et qualité de l'historique
uv run kobr4 backtest --config config/backtest.yaml --start 2023-01-01   # rapport dans reports/

# Laboratoire : chercher de meilleurs réglages, sans jamais toucher au bot en cours
uv run kobr4 lab optimize --config config/backtest.yaml --strategy ema-cross-h1 --start 2019-01-01 --end 2025-12-31
uv run kobr4 lab list
uv run kobr4 lab approve <id>              # puis :
uv run kobr4 lab apply <id> --config config/paper.yaml   # version +1, sauvegarde de l'ancienne

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

## Bot seul (`kobr4 run`)

Variables d'environnement lues :

| Variable | Rôle |
|---|---|
| `KOBR4_BROKER_API_KEY` | jeton API OANDA |
| `KOBR4_OANDA_ACCOUNT_ID` | identifiant du compte OANDA |
| `KOBR4_DATABASE_URL` | journal, ex. `postgresql+asyncpg://kobr4@localhost/kobr4` (facultatif) |
| `KOBR4_TELEGRAM_TOKEN`, `KOBR4_TELEGRAM_CHAT_ID` | alertes Telegram (facultatif) |

Arrêter le bot (Ctrl+C) ne ferme pas les positions : elles gardent leur stop chez le courtier.

## Tests sur PostgreSQL

Les tests de base de données tournent sur SQLite, et aussi sur PostgreSQL si `KOBR4_TEST_PG_URL` est défini :

```bash
KOBR4_TEST_PG_URL=postgresql+asyncpg://kobr4@127.0.0.1:5432/kobr4_test uv run pytest
```

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
├── execution/   # interface courtier, courtier simulé, OANDA, OMS
├── portfolio/   # positions, solde, équité, drawdown
├── backtest/    # moteur de rejeu, statistiques, rapport HTML
├── lab/         # optimisation walk-forward, stabilité, Sharpe dégonflé, propositions
├── live/        # bot en direct : flux de prix, journal, métriques, alertes
├── db/          # schéma de la base (plateforme et journal)
├── security/    # chiffrement des secrets
└── app.py    # point d'entrée
```

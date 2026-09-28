# Kobr4 FX

Bot de trading forex automatisé. Voir [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) pour l'architecture et la feuille de route, et [maquette/](maquette/) pour la maquette du tableau de bord.

**État : phase 0 (fondations).** Le bot ne passe encore aucun ordre.

## Installation

Prérequis : Python 3.12 et [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Commandes

```bash
uv run kobr4 --config config/paper.yaml   # vérifie la configuration et démarre
uv run pytest                             # tests
uv run ruff check . && uv run ruff format --check .   # lint et format
uv run mypy src                           # typage strict
```

## Configuration

- `config/backtest.yaml` : backtest avec le courtier simulé.
- `config/paper.yaml` : compte démo du courtier.
- `config/live.yaml` : compte réel. Jamais versionné ; exige `confirm_live: true`.

La clé API du courtier n'est jamais écrite dans un fichier : elle est lue dans la variable d'environnement indiquée par `broker.api_key_env` (par défaut `KOBR4_BROKER_API_KEY`).

## Organisation

```
src/kobr4/
├── core/     # modèles du domaine, ordres, événements, bus, horloge
├── config/   # chargement et validation de la configuration
└── app.py    # point d'entrée
```

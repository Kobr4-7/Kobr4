"""Dossiers par défaut, lus dans l'environnement.

Dans le conteneur, `KOBR4_DATA_DIR` et `KOBR4_LAB_DIR` pointent vers les volumes
persistants (/data, /lab) : les commandes `kobr4` y écrivent par défaut, comme le site.
"""

import os
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("KOBR4_DATA_DIR", "data"))


def lab_dir() -> Path:
    return Path(os.environ.get("KOBR4_LAB_DIR", "lab"))


def reports_dir() -> Path:
    value = os.environ.get("KOBR4_REPORTS_DIR")
    if value:
        return Path(value)
    return lab_dir() / "reports" if "KOBR4_LAB_DIR" in os.environ else Path("reports")

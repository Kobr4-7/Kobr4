"""Chiffrement des secrets stockés en base (jetons courtier, jeton Telegram…).

AES-256-GCM avec une clé maîtresse unique fournie au serveur par la variable
d'environnement `KOBR4_MASTER_KEY` (32 octets en base64). Chaque valeur chiffrée porte un
numéro de version de clé, pour pouvoir changer de clé plus tard sans tout casser.
"""

import base64
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENV_VAR = "KOBR4_MASTER_KEY"


class CryptoError(Exception):
    pass


def generate_master_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


class SecretBox:
    def __init__(self, master_key: str, key_id: str = "k1") -> None:
        try:
            key = base64.b64decode(master_key, validate=True)
        except ValueError as e:
            raise CryptoError("clé maîtresse invalide (base64 attendu)") from e
        if len(key) != 32:
            raise CryptoError("la clé maîtresse doit faire 32 octets")
        self._aead = AESGCM(key)
        self.key_id = key_id

    @classmethod
    def from_env(cls) -> "SecretBox":
        value = os.environ.get(ENV_VAR)
        if not value:
            raise CryptoError(
                f"{ENV_VAR} absente : générer une clé avec `kobr4 server gen-key` et la définir"
            )
        return cls(value)

    def encrypt(self, plaintext: str, context: str = "") -> str:
        """`context` (ex. identifiant de l'utilisateur) est lié au chiffré : une valeur
        copiée d'un compte à l'autre ne se déchiffre pas."""
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        return f"{self.key_id}:{base64.urlsafe_b64encode(nonce + ct).decode()}"

    def decrypt(self, token: str, context: str = "") -> str:
        key_id, _, payload = token.partition(":")
        if key_id != self.key_id or not payload:
            raise CryptoError("valeur chiffrée avec une autre clé ou mal formée")
        raw = base64.urlsafe_b64decode(payload)
        try:
            return self._aead.decrypt(raw[:12], raw[12:], context.encode()).decode()
        except InvalidTag as e:
            raise CryptoError("déchiffrement impossible (clé ou contexte incorrect)") from e

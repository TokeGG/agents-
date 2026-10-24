"""Vault: encrypted at-rest storage for per-service credentials.

Secrets are encrypted with Fernet (AES-128-CBC + HMAC-SHA256); the IV and
authentication tag live inside the token, so a single `ciphertext` column is
enough. A wrong key raises `cryptography.fernet.InvalidToken` on read rather
than returning garbage.
"""

from __future__ import annotations

from typing import Callable

from cryptography.fernet import Fernet

from app.models import VaultSecret


class Vault:
    def __init__(self, key: bytes, session_factory: Callable):
        self._fernet = Fernet(key)
        self.session_factory = session_factory

    @staticmethod
    def generate_key() -> bytes:
        return Fernet.generate_key()

    def get(self, service_name: str) -> str | None:
        session = self.session_factory()
        try:
            row = session.query(VaultSecret).filter_by(service_name=service_name).first()
            if row is None:
                return None
            return self._fernet.decrypt(row.ciphertext.encode()).decode()
        finally:
            session.close()

    def set(self, service_name: str, value: str) -> None:
        ciphertext = self._fernet.encrypt(value.encode()).decode()
        session = self.session_factory()
        try:
            row = session.query(VaultSecret).filter_by(service_name=service_name).first()
            if row is None:
                session.add(VaultSecret(service_name=service_name, ciphertext=ciphertext))
            else:
                row.ciphertext = ciphertext
            session.commit()
        finally:
            session.close()

    def list_service_names(self) -> list[str]:
        session = self.session_factory()
        try:
            rows = session.query(VaultSecret.service_name).order_by(VaultSecret.service_name).all()
            return [name for (name,) in rows]
        finally:
            session.close()

    def delete(self, service_name: str) -> None:
        session = self.session_factory()
        try:
            session.query(VaultSecret).filter_by(service_name=service_name).delete()
            session.commit()
        finally:
            session.close()

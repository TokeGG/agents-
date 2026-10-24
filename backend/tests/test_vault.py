import pytest

from app.models import VaultSecret
from app.vault import Vault


def test_vault_set_then_get_round_trips(db_session):
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "ghp_abc123")
    assert vault.get("github") == "ghp_abc123"
    assert vault.get("unknown_service") is None


def test_vault_set_overwrites_existing_secret(db_session):
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "old")
    vault.set("github", "new")
    assert vault.get("github") == "new"
    assert db_session.query(VaultSecret).filter_by(service_name="github").count() == 1


def test_vault_list_and_delete(db_session):
    vault = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault.set("github", "x")
    vault.set("slack", "y")
    assert sorted(vault.list_service_names()) == ["github", "slack"]
    vault.delete("github")
    assert vault.list_service_names() == ["slack"]
    assert vault.get("github") is None


def test_vault_wrong_key_raises_on_decrypt(db_session):
    vault_a = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    vault_a.set("github", "secret")
    vault_b = Vault(Vault.generate_key(), session_factory=lambda: db_session)
    with pytest.raises(Exception):  # cryptography.fernet.InvalidToken
        vault_b.get("github")

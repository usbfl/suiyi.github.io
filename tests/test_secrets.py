import pytest
from cryptography.fernet import Fernet
from app.secrets import seal, unseal


def test_vault_round_trip_and_wrong_key(monkeypatch):
    value = 'FIXTURE_ONLY_MODEL_CREDENTIAL'
    encrypted = seal(value)
    assert encrypted.startswith('fernet:') and value not in encrypted
    assert unseal(encrypted) == value
    monkeypatch.setenv('ALPHALAB_SECRET_KEY', Fernet.generate_key().decode())
    with pytest.raises(ValueError, match='解密'):
        unseal(encrypted)


def test_vault_rejects_malformed_key(monkeypatch):
    monkeypatch.setenv('ALPHALAB_SECRET_KEY', 'not-a-key')
    with pytest.raises(ValueError, match='Fernet'):
        seal('FIXTURE_ONLY')


def test_vault_rejects_tampered_ciphertext():
    encrypted = seal('FIXTURE_ONLY')
    with pytest.raises(ValueError, match='解密'):
        unseal(encrypted[:-8] + 'AAAAAAAA')

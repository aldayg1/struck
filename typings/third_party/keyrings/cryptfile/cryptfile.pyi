# Minimal stub for keyrings.cryptfile 1.3.9 (the package ships no py.typed):
# only the API struck/secrets.py uses, as defined in keyrings/cryptfile/cryptfile.py,
# file.py (EncryptedKeyring.keyring_key property) and file_base.py (Keyring).
from keyring.backend import KeyringBackend

class CryptFileKeyring(KeyringBackend):
    file_path: str
    keyring_key: str
    _keyring_key: str | None  # file.py EncryptedKeyring: the master, None once locked
    def __init__(self) -> None: ...
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...
    def _lock(self) -> None: ...  # file.py: ``del self.keyring_key`` (drops the master)
    def _check_file(self) -> bool: ...  # file.py: the file holds the password reference

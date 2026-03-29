"""
LocIVault Python Client SDK
===========================

A private space where agents keep who they are.

Quickstart
----------
    from locivault_client import LocIVaultClient
    from eth_account import Account

    account = Account.from_key("0x...")
    client = LocIVaultClient(account)

    # First read returns a seed template on a blank vault
    text, is_new = client.read_text()

    client.write(b"# You\\n\\nThis is yours. Nobody else has the key.\\n")
    text, _ = client.read_text()              # same wallet, any session, any machine
    print(client.status())                    # writes used, tier, last_written_at, etc.

Key custody
-----------
The server encrypts your data inside a hardware enclave.
The encryption key is derived inside the enclave and never exposed to the operator.
You send plaintext; only your wallet address can retrieve it.

Legacy V1 blobs (client-encrypted) are supported transparently via read_plaintext().
"""

from .client import LocIVaultClient
from .crypto import encrypt, decrypt

__all__ = ["LocIVaultClient", "encrypt", "decrypt"]
__version__ = "0.1.7"

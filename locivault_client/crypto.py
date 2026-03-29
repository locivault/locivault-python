"""
AES-256-GCM encryption helpers for LocIVault clients.

Key derivation: scrypt(wallet_private_key, salt) → 32-byte AES key
Blob format (v1):  4-byte magic | 1-byte version | 32-byte salt | 12-byte nonce | ciphertext+GCM-tag

The blob format is stable and versioned — any LocIVault-compatible client
using these helpers can round-trip data without external state.

Usage:
    from eth_account import Account
    from locivault_client.crypto import derive_key, encrypt, decrypt

    account = Account.from_key("0x...")
    key = derive_key(account)

    blob = encrypt(b"plaintext", key)
    text = decrypt(blob, key)
"""

import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.backends import default_backend

# Binary format constants
MAGIC   = b'\xA6\xE1\x4D\x00'
VERSION = b'\x01'
HEADER  = MAGIC + VERSION   # 5 bytes

# scrypt params — v1 format, do not change without bumping VERSION
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1


def derive_key(private_key_bytes: bytes, salt: bytes) -> bytes:
    """
    Derive a 32-byte AES key from a wallet private key + salt via scrypt.

    Args:
        private_key_bytes: raw 32-byte wallet private key
        salt:              32-byte random salt (generated fresh per encrypt call)

    Returns:
        32-byte AES-256 key
    """
    kdf = Scrypt(
        salt=salt,
        length=32,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        backend=default_backend(),
    )
    return kdf.derive(private_key_bytes)


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """
    Encrypt plaintext with AES-256-GCM. Returns a self-contained binary blob.

    The blob includes the salt and nonce — no external state needed to decrypt.
    Fresh salt per call = different derived key per call (forward secrecy).

    Args:
        plaintext: arbitrary bytes to encrypt
        key:       32-byte AES key (e.g. from derive_key())

    Returns:
        bytes: encrypted blob in LocIVault v1 format
    """
    salt  = os.urandom(32)
    nonce = os.urandom(12)
    derived = derive_key(key, salt)   # note: key here = private key seed
    ciphertext = AESGCM(derived).encrypt(nonce, plaintext, None)
    return HEADER + salt + nonce + ciphertext


def decrypt(blob: bytes, key: bytes) -> bytes:
    """
    Decrypt a LocIVault v1 encrypted blob.

    Args:
        blob: bytes from encrypt() or from the /read endpoint
        key:  32-byte AES key seed (same value passed to encrypt)

    Returns:
        bytes: original plaintext

    Raises:
        ValueError: if magic/version don't match, or AESGCM authentication fails
    """
    if len(blob) < 5 + 32 + 12 + 16:
        raise ValueError(f"Blob too short ({len(blob)} bytes)")
    if blob[:4] != MAGIC:
        raise ValueError("Not a LocIVault blob (bad magic)")
    if blob[4:5] != VERSION:
        raise ValueError(f"Unknown blob version: {blob[4]:#x}")

    salt       = blob[5:37]
    nonce      = blob[37:49]
    ciphertext = blob[49:]

    derived = derive_key(key, salt)
    try:
        return AESGCM(derived).decrypt(nonce, ciphertext, None)
    except Exception as e:
        raise ValueError(f"Decryption failed (wrong key or tampered blob): {e}") from e


# ── convenience wrappers that take an eth_account.Account directly ────────────

def encrypt_with_account(plaintext: bytes, account) -> bytes:
    """
    Encrypt using an eth_account Account's private key.

    Args:
        plaintext: bytes to encrypt
        account:   eth_account.Account instance (must have ._private_key)

    Returns:
        encrypted blob bytes
    """
    raw_key = _extract_private_key(account)
    return encrypt(plaintext, raw_key)


def decrypt_with_account(blob: bytes, account) -> bytes:
    """
    Decrypt using an eth_account Account's private key.

    Args:
        blob:    encrypted blob bytes
        account: eth_account.Account instance

    Returns:
        plaintext bytes
    """
    raw_key = _extract_private_key(account)
    return decrypt(blob, raw_key)


def _extract_private_key(account) -> bytes:
    """Extract raw 32-byte private key from an eth_account LocalAccount."""
    # eth_account stores it as _private_key (HexBytes or bytes)
    pk = getattr(account, '_private_key', None) or getattr(account, 'key', None)
    if pk is None:
        raise ValueError(
            "Could not extract private key from account object. "
            "Expected eth_account.LocalAccount with ._private_key attribute."
        )
    if isinstance(pk, (bytes, bytearray)):
        return bytes(pk)
    # HexBytes or other hex-string type
    return bytes.fromhex(str(pk).removeprefix("0x"))

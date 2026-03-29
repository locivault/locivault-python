"""
Request signing for LocIVault.

Every write/read request is signed with the agent's wallet key.
The server verifies the signature matches the X-Wallet-Address header.

WHY: Without signing, anyone who knows a wallet address can overwrite that
agent's memory (address is not secret). With signing, only the holder of
the private key can write/read — the address IS a verifiable identity.

Signature scheme:
  message = SHA256( wallet_address_lower + ":" + timestamp_str + ":" + method_upper + ":" + path )
  signature = eth_account sign_message(Ethereum signed message prefix + message)

Headers added to every authenticated request:
  X-Wallet-Address: 0x...   (already present)
  X-Timestamp:      1234567890   (Unix seconds, UTC)
  X-Signature:      0x...   (65-byte hex ECDSA sig)
"""

import hashlib
import time
from eth_account import Account
from eth_account.messages import encode_defunct


def _build_message(wallet: str, timestamp: int, method: str, path: str) -> bytes:
    """Build the canonical message bytes to sign/verify."""
    raw = f"{wallet.lower()}:{timestamp}:{method.upper()}:{path}"
    return hashlib.sha256(raw.encode()).digest()


def sign_request(account, method: str, path: str) -> dict:
    """
    Sign a request. Returns headers dict with X-Timestamp and X-Signature.

    Args:
        account:  eth_account LocalAccount
        method:   HTTP method string ("GET", "POST", etc.)
        path:     URL path being requested (e.g. "/write")

    Returns:
        dict of extra headers to merge into the request:
        {"X-Timestamp": "...", "X-Signature": "0x..."}
    """
    ts = int(time.time())
    msg_bytes = _build_message(account.address, ts, method, path)
    signable = encode_defunct(primitive=msg_bytes)
    signed = account.sign_message(signable)
    return {
        "X-Timestamp": str(ts),
        "X-Signature": signed.signature.hex(),
    }

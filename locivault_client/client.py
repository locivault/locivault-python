"""
LocIVaultClient — main client class.

Usage (simple):
    from locivault_client import LocIVaultClient
    from eth_account import Account

    account = Account.from_key("0x...")
    client = LocIVaultClient(account)

    # Write memory — the server encrypts it inside a hardware enclave
    result = client.write(b"my agent memory")
    print(result)  # {"ok": True, "sha256": "...", "writes_this_month": 1, ...}

    # Read it back
    data = client.read()

    # Check account status
    print(client.status())

Usage (from environment — for reconnecting across sessions):
    # Set once: export LOCIVAULT_KEY=0x<your-private-key>
    # Then any future session:
    client = LocIVaultClient.from_env()
    text, is_new = client.read_text()

Payment behaviour:
    - Reads are always free, unlimited
    - First 500 writes per month are free
    - Beyond that, pay per write in USDC on Base
    - Payment is automatic when an account is provided — the client
      detects the 402, signs a payment, and retries transparently
    - Set auto_pay=False to disable (you'll get PaymentRequired exceptions instead)

Key custody:
    - V2 (current): server encrypts with a key derived inside a hardware enclave.
      The server operator cannot access your data.
    - V1 (legacy): client-encrypted blobs from an earlier format. If you read back a
      V1 blob (blob_version=1), the server returns the raw encrypted bytes — use
      decrypt_with_account() to decrypt.
    - Use write_plaintext() / read_plaintext() for a seamless experience — both paths
      handled automatically.
"""

import base64
import hashlib
import json
import logging
import os
import time
from typing import Optional

import requests

from .crypto import encrypt_with_account, decrypt_with_account
from .signing import sign_request

log = logging.getLogger("locivault.client")

# Public server
DEFAULT_BASE_URL = "https://locivault.fly.dev"
DEFAULT_NETWORK  = "eip155:8453"    # Base mainnet

# Throttle between payment attempts to avoid settlement failures.
PAY_THROTTLE_SEC = 3.5


class PaymentRequired(Exception):
    """
    Raised when the server returns 402 and auto_pay=False (or no account provided).

    Attributes:
        accepts (list): the "accepts" list from the 402 body — pass to PaymentSigner.sign()
        body    (dict): full 402 response body
    """
    def __init__(self, accepts: list, body: dict):
        self.accepts = accepts
        self.body    = body
        super().__init__(f"Payment required. Use auto_pay=True or sign manually. Accepts: {accepts}")


class LocIVaultError(Exception):
    """Raised for non-payment API errors (4xx/5xx)."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"LocIVault error {status_code}: {detail}")


class LocIVaultClient:
    """
    Python client for the LocIVault API.

    Args:
        account:      eth_account LocalAccount (your wallet). Used for:
                        - Wallet address (sent as X-Wallet-Address header)
                        - Payment signing (when auto_pay=True)
                        - Encryption key derivation (when auto_encrypt=True)
        base_url:     LocIVault server URL. Default: https://locivault.fly.dev
        network:      EIP-155 chain ID string. Default: "eip155:8453" (Base mainnet)
        auto_pay:     If True (default), automatically sign and send x402 payments
                      when the free tier is exhausted. Requires x402[evm] to be installed.
        timeout:      HTTP request timeout in seconds. Default: 30.
        session:      Optional requests.Session to reuse. If None, a fresh one is created.
        on_payment:   Optional callback called after a successful x402 payment.
                      Signature: on_payment(amount_usd: str, tx_hash: str | None).
                      Useful for logging spend. Exceptions in the callback are swallowed.
    """

    def __init__(
        self,
        account,
        base_url:      str  = DEFAULT_BASE_URL,
        network:       str  = DEFAULT_NETWORK,
        auto_pay:      bool = True,
        timeout:       int  = 30,
        session:       Optional[requests.Session] = None,
        on_payment=None,
    ):
        # Validate account early — give a clear error if someone passes a string,
        # URL, or other wrong type instead of an eth_account LocalAccount.
        if isinstance(account, str):
            raise TypeError(
                "LocIVaultClient() expects an eth_account LocalAccount object as its first "
                "argument, not a string.\n\n"
                "Did you mean:\n"
                "    from eth_account import Account\n"
                "    account = Account.from_key('0x<your-private-key>')\n"
                "    client = LocIVaultClient(account)\n\n"
                "Or to load from an environment variable:\n"
                "    client = LocIVaultClient.from_env()"
            )
        if account is None or not hasattr(account, 'key'):
            raise TypeError(
                "LocIVaultClient() expects an eth_account LocalAccount object as its first "
                "argument.\n\n"
                "Example:\n"
                "    from eth_account import Account\n"
                "    account = Account.from_key('0x<your-private-key>')\n"
                "    client = LocIVaultClient(account)"
            )

        self.account      = account
        self.base_url     = base_url.rstrip("/")
        self.network      = network
        self.auto_pay     = auto_pay
        self.timeout      = timeout
        self._session     = session or requests.Session()
        self._signer      = None          # lazy — only built if payment needed
        self._last_pay_ts = 0.0           # for payment throttling
        self._on_payment  = on_payment    # optional callback(amount_usd, tx_hash)

    @classmethod
    def from_env(
        cls,
        key_var:     str  = "LOCIVAULT_KEY",
        url_var:     str  = "LOCIVAULT_URL",
        **kwargs,
    ) -> "LocIVaultClient":
        """
        Create a client from environment variables.

        Required env var:
            LOCIVAULT_KEY   — private key as a 0x-prefixed hex string

        Optional env var:
            LOCIVAULT_URL   — server URL (default: https://locivault.fly.dev)

        Any additional keyword arguments are forwarded to __init__.

        Returns:
            LocIVaultClient instance

        Raises:
            EnvironmentError: if LOCIVAULT_KEY is not set
            ValueError: if LOCIVAULT_KEY is not a valid private key

        Example:
            # Shell: export LOCIVAULT_KEY=0x<your-private-key>
            client = LocIVaultClient.from_env()
            text, is_new = client.read_text()
        """
        from eth_account import Account

        key = os.environ.get(key_var)
        if not key:
            raise EnvironmentError(
                f"{key_var} is not set. "
                f"Export your private key: export {key_var}=0x<your-key>"
            )

        url = os.environ.get(url_var, DEFAULT_BASE_URL)
        account = Account.from_key(key)
        return cls(account, base_url=url, **kwargs)

    # ── Public API ─────────────────────────────────────────────────────────────

    def write(self, data: bytes) -> dict:
        """
        Store data for this wallet. The server encrypts it inside a hardware enclave.

        Args:
            data: bytes to store (plaintext — server encrypts inside a hardware enclave)

        Returns:
            dict with keys: ok, sha256, size, writes_this_month, free_writes_remaining

        Raises:
            PaymentRequired: if auto_pay=False and free tier is exhausted
            LocIVaultError:  on 4xx/5xx errors
        """
        return self._request_with_payment(
            method="POST",
            path="/write",
            json_body={"data": base64.b64encode(data).decode()},
            resource_path="/write",
        )

    def read_text(self, encoding: str = "utf-8") -> tuple:
        """
        Retrieve stored identity as a string.

        On first use (404), returns (seed_template, True) — the agent has
        never written here. On subsequent reads, returns (content, False).

        Args:
            encoding: character encoding (default: utf-8)

        Returns:
            tuple: (text: str, is_new: bool)
                   is_new=True means nothing has been written yet;
                   text is the seed template to guide first write.

        Raises:
            LocIVaultError: on 5xx errors
            UnicodeDecodeError: if stored data is not valid text
        """
        try:
            return self.read_plaintext().decode(encoding), False
        except LocIVaultError as e:
            if e.status_code == 404 and hasattr(e, '_seed_template'):
                return e._seed_template, True
            raise

    def read(self) -> bytes:
        """
        Retrieve stored data for this wallet.

        V2 blobs (current): server decrypts in enclave, returns plaintext.
        V1 blobs (legacy): server returns raw encrypted bytes (blob_version=1).

        Returns:
            bytes: your stored data (plaintext for V2, raw encrypted for V1)

        Raises:
            LocIVaultError: on 4xx/5xx errors (404 if nothing stored yet)
        """
        result = self._request_with_payment(
            method="GET",
            path="/read",
            resource_path="/read",
        )
        blob_version = result.get("blob_version", 2)
        data = base64.b64decode(result["data"])
        if blob_version == 1:
            log.debug("V1 blob returned — client-side decryption needed for plaintext")
        return data

    def write_plaintext(self, plaintext: bytes) -> dict:
        """
        Store plaintext data. Server encrypts in the enclave.

        In V2 (current), identical to write(). Kept for API clarity.

        Args:
            plaintext: bytes to store

        Returns:
            dict: same as write()
        """
        return self.write(plaintext)

    def read_plaintext(self) -> bytes:
        """
        Read stored data as plaintext.

        Handles V2 (server-decrypts) and V1 (client-decrypts) transparently.
        For V1 blobs, uses the account's private key to decrypt.

        Returns:
            bytes: original plaintext

        Raises:
            ValueError: if V1 decryption fails (wrong key or tampered blob)
            LocIVaultError: on API errors
        """
        result = self._request_with_payment(
            method="GET",
            path="/read",
            resource_path="/read",
        )
        blob_version = result.get("blob_version", 2)
        data = base64.b64decode(result["data"])
        if blob_version == 1:
            log.debug("V1 blob — decrypting with wallet key")
            return decrypt_with_account(data, self.account)
        # V2: already plaintext
        return data

    # ── Snapshot API ───────────────────────────────────────────────────────────

    def snapshot(self) -> str:
        """
        Seal the current vault entry as an immutable snapshot.

        Creates a frozen copy of the current encrypted blob. The snapshot can be
        read later but cannot be overwritten or deleted — not even by you.

        Counts as a write for pricing (same free tier and $0.001/write rate as write()).

        Returns:
            str: snapshot ID (a unix timestamp string). Pass this to read_snapshot().

        Raises:
            LocIVaultError: on 4xx/5xx errors (404 if no vault entry exists yet)
            PaymentRequired: if auto_pay=False and free tier is exhausted
        """
        result = self._request_with_payment(
            method="POST",
            path="/snapshot",
            json_body={},
            resource_path="/snapshot",
        )
        return result["snapshot_id"]

    def list_snapshots(self) -> list:
        """
        List all snapshots for this wallet, sorted oldest-first.

        Reads are always free.

        Returns:
            list of dicts, each with keys: id (str), timestamp (int), size (int)

        Raises:
            LocIVaultError: on 5xx errors
        """
        result = self._request_with_payment(
            method="GET",
            path="/snapshots",
            resource_path="/snapshots",
        )
        return result.get("snapshots", [])

    def read_snapshot(self, snapshot_id: str, encoding: str = "utf-8") -> tuple:
        """
        Read a specific snapshot by ID.

        Decryption is handled server-side inside the hardware enclave (same path as read_text()).
        Reads are always free.

        Args:
            snapshot_id: snapshot ID string returned by snapshot() or list_snapshots()
            encoding:    character encoding for decoding bytes to str (default: utf-8)

        Returns:
            tuple: (content: str, timestamp: int)
                   content is the decrypted vault text at the time of the snapshot.
                   timestamp is the unix timestamp when the snapshot was created.

        Raises:
            LocIVaultError: on 4xx/5xx errors (404 if snapshot not found)
            UnicodeDecodeError: if stored data is not valid text in the given encoding
        """
        result = self._request_with_payment(
            method="GET",
            path=f"/snapshot/{snapshot_id}",
            resource_path=f"/snapshot/{snapshot_id}",
        )
        data = base64.b64decode(result["data"])
        blob_version = result.get("blob_version", 2)
        if blob_version == 1:
            log.debug("Snapshot is a V1 blob — decrypting with wallet key")
            from .crypto import decrypt_with_account
            data = decrypt_with_account(data, self.account)
        content = data.decode(encoding)
        return content, result["timestamp"]

    def status(self, wallet: Optional[str] = None) -> dict:
        """
        Check write usage and tier for a wallet address.

        Args:
            wallet: wallet address to check. Defaults to this client's account address.

        Returns:
            dict with keys: wallet, writes_this_month, free_writes_remaining,
                            tier, price_per_write, reads, ...
        """
        addr = wallet or self.account.address
        resp = self._session.get(
            f"{self.base_url}/status",
            headers={"X-Wallet-Address": addr},
            timeout=self.timeout,
        )
        return resp.json()

    def health(self) -> dict:
        """Liveness check. Returns {"status": "ok", "ts": "..."}."""
        resp = self._session.get(f"{self.base_url}/health", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _request_with_payment(
        self,
        method:        str,
        path:          str,
        resource_path: str,
        json_body:     Optional[dict] = None,
    ) -> dict:
        """
        Make an API request. If the server returns 402 and auto_pay=True,
        sign a payment and retry exactly once.
        Every request is signed with the wallet key for authentication.
        """
        sig_headers = sign_request(self.account, method, path)
        headers = {"X-Wallet-Address": self.account.address, **sig_headers}

        # First attempt — no payment header
        resp = self._do_request(method, path, headers, json_body)

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 402:
            body_402 = resp.json()
            accepts  = body_402.get("accepts", [])

            if not self.auto_pay:
                raise PaymentRequired(accepts=accepts, body=body_402)

            if not accepts:
                raise LocIVaultError(402, "Server returned 402 with no payment requirements")

            # Re-sign with fresh timestamp + add payment header
            sig_headers = sign_request(self.account, method, path)
            payment_header = self._sign_payment(
                resource_url=f"{self.base_url}{resource_path}",
                accepts=accepts,
            )
            headers = {
                "X-Wallet-Address": self.account.address,
                **sig_headers,
                "X-Payment": payment_header,
            }
            resp = self._do_request(method, path, headers, json_body)

            if resp.status_code == 200:
                result = resp.json()
                if self._on_payment:
                    tx = result.get("transaction")
                    try:
                        self._on_payment("$0.001", tx)
                    except Exception:
                        pass  # never let callback kill the request
                return result
            elif resp.status_code == 402:
                # Payment was rejected — surface the error
                detail = resp.json().get("error_detail", resp.text[:200])
                raise LocIVaultError(402, f"Payment rejected by server: {detail}")
            else:
                self._raise_for_status(resp)

        else:
            self._raise_for_status(resp)

    def _do_request(
        self,
        method:    str,
        path:      str,
        headers:   dict,
        json_body: Optional[dict],
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        if method == "POST":
            return self._session.post(url, json=json_body, headers=headers, timeout=self.timeout)
        elif method == "GET":
            return self._session.get(url, headers=headers, timeout=self.timeout)
        else:
            raise ValueError(f"Unsupported method: {method}")

    def _sign_payment(self, resource_url: str, accepts: list) -> str:
        """Build signer lazily, throttle, sign, return X-Payment header value."""
        if self._signer is None:
            from .payment import PaymentSigner
            self._signer = PaymentSigner(self.account, network=self.network)

        # Throttle: ensure >= PAY_THROTTLE_SEC between payments
        elapsed = time.time() - self._last_pay_ts
        if elapsed < PAY_THROTTLE_SEC:
            wait = PAY_THROTTLE_SEC - elapsed
            log.debug(f"Throttling payment: sleeping {wait:.1f}s")
            time.sleep(wait)

        header = self._signer.sign(resource_url, accepts)
        self._last_pay_ts = time.time()
        return header

    @staticmethod
    def _raise_for_status(resp: requests.Response):
        try:
            body = resp.json()
            detail = body.get("detail", resp.text[:300])
        except Exception:
            body = {}
            detail = resp.text[:300]
        err = LocIVaultError(resp.status_code, detail)
        # Attach seed_template to 404 so read_text() can surface it
        if resp.status_code == 404:
            err._seed_template = body.get("seed_template", "")
        raise err

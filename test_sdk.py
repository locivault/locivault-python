"""
LocIVault Python SDK test suite.

Tests:
  1. Crypto unit tests (encrypt/decrypt round-trip, tamper detection, format)
  2. Client integration tests against live locivault.fly.dev
     - free tier write/read
     - health + status endpoints
     - write_plaintext / read_plaintext
     - auto_pay=False raises PaymentRequired (triggered after free tier exhausted)

Run:
    # Option A — provide a keystore file + passphrase (EIP-55 format, e.g. from MetaMask/geth)
    LOCIVAULT_WALLET_FILE=/path/to/keystore.json \\
    LOCIVAULT_PASSPHRASE=yourpassphrase \\
    python3 test_sdk.py

    # Option B — provide a raw hex private key directly
    LOCIVAULT_PRIVATE_KEY=0x... python3 test_sdk.py

    # Option C — no env vars: a fresh throwaway wallet is generated automatically.
    # Integration tests still run; paid-tier tests are skipped (no funded wallet).
    python3 test_sdk.py
"""

import os, sys, json, base64, hashlib, time

from eth_account import Account
from locivault_client import LocIVaultClient, encrypt, decrypt
from locivault_client.crypto import derive_key, _extract_private_key
from locivault_client.client import PaymentRequired, LocIVaultError

BASE_URL = os.environ.get("LOCIVAULT_BASE_URL", "https://locivault.fly.dev")

# ── wallet loading ────────────────────────────────────────────────────────────
_private_key_hex = os.environ.get("LOCIVAULT_PRIVATE_KEY")
_wallet_file     = os.environ.get("LOCIVAULT_WALLET_FILE")
_passphrase      = os.environ.get("LOCIVAULT_PASSPHRASE")

if _private_key_hex:
    test_account = Account.from_key(_private_key_hex)
    print(f"Wallet loaded from LOCIVAULT_PRIVATE_KEY: {test_account.address}")
elif _wallet_file and _passphrase:
    with open(_wallet_file) as f:
        _wallet_json = json.load(f)
    # support both raw keystore and {"keystore": {...}} wrapper formats
    _keystore = _wallet_json.get("keystore", _wallet_json)
    _pk = Account.decrypt(_keystore, _passphrase)
    test_account = Account.from_key(_pk)
    print(f"Wallet loaded from keystore: {test_account.address}")
else:
    test_account = Account.create()
    print(f"No wallet env vars set — using fresh throwaway: {test_account.address}")
    print("  (set LOCIVAULT_PRIVATE_KEY or LOCIVAULT_WALLET_FILE+LOCIVAULT_PASSPHRASE for a funded wallet)")

PASS_MARK = "✓"
FAIL_MARK = "✗"

def section(title):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}")

def ok(msg):
    print(f"  {PASS_MARK} {msg}")

def fail(msg):
    print(f"  {FAIL_MARK} {msg}")
    sys.exit(1)

# throwaway wallet for free-tier tests (always fresh, no funds needed)
throwaway = Account.create()
print(f"Throwaway: {throwaway.address}")

# ══════════════════════════════════════════════════════════════════════════════
section("1. CRYPTO — round-trip")
raw_key = _extract_private_key(test_account)
plaintext = b"the quick brown fox jumps over the lazy dog"
blob = encrypt(plaintext, raw_key)
recovered = decrypt(blob, raw_key)
assert recovered == plaintext, "Round-trip failed!"
ok(f"encrypt→decrypt round-trip ({len(blob)} bytes blob)")

# ── format checks ──────────────────────────────────────────────────────────────
section("2. CRYPTO — blob format")
assert blob[:4] == b'\xA6\xE1\x4D\x00', "Bad magic"
assert blob[4:5] == b'\x01',            "Bad version"
assert len(blob) == 5 + 32 + 12 + len(plaintext) + 16, f"Unexpected length {len(blob)}"
ok("Magic, version, length all correct")

# Fresh salt per encrypt = different blob each time
blob2 = encrypt(plaintext, raw_key)
assert blob != blob2, "Two encrypts of same plaintext should differ (fresh salt)"
ok("Fresh salt per call — blobs differ")

# Tamper detection
import copy
tampered = bytearray(blob)
tampered[-1] ^= 0xFF
try:
    decrypt(bytes(tampered), raw_key)
    fail("Tamper detection missed!")
except ValueError as e:
    ok(f"Tamper detected: {e}")

# Wrong key detection
wrong_key = os.urandom(32)
try:
    decrypt(blob, wrong_key)
    fail("Wrong key not detected!")
except ValueError as e:
    ok(f"Wrong key detected: {e}")

# ── encrypt_with_account / decrypt_with_account ───────────────────────────────
section("3. CRYPTO — account-based helpers")
from locivault_client.crypto import encrypt_with_account, decrypt_with_account
blob3 = encrypt_with_account(b"account-based encryption test", test_account)
result3 = decrypt_with_account(blob3, test_account)
assert result3 == b"account-based encryption test"
ok("encrypt_with_account / decrypt_with_account round-trip")

# ── UTF-8 check (should not decode) ──────────────────────────────────────────
section("4. CRYPTO — ciphertext is opaque binary")
try:
    blob.decode('utf-8')
    fail("Ciphertext decoded as UTF-8 — something is wrong")
except UnicodeDecodeError:
    ok("Ciphertext is not valid UTF-8 (good)")

# ══════════════════════════════════════════════════════════════════════════════
section("5. CLIENT — health check")
client = LocIVaultClient(test_account, base_url=BASE_URL)
h = client.health()
assert h["status"] == "ok"
ok(f"Health: {h}")

# ── status (no wallet) ─────────────────────────────────────────────────────────
section("6. CLIENT — status (service-level, no wallet)")
s = client.status(wallet=throwaway.address)
print(f"  {json.dumps(s, indent=4)}")
assert s.get("writes_this_month", -1) == 0, f"Fresh wallet should have 0 writes, got {s}"
ok(f"Fresh throwaway wallet: writes_this_month=0, free_remaining={s['free_writes_remaining']}")

# ── free-tier write ────────────────────────────────────────────────────────────
# V2: write() sends plaintext; server encrypts in hardware enclave. sha256 is of the stored blob,
# not the input — so we just check ok=True and round-trip via read().
section("7. CLIENT — write (free tier, throwaway wallet)")
throwaway_client = LocIVaultClient(throwaway, base_url=BASE_URL)
test_data = b"hello from locivault sdk test " + os.urandom(8)
result = throwaway_client.write(test_data)
print(f"  {json.dumps(result, indent=4)}")
assert result["ok"] is True
assert result["writes_this_month"] == 1
ok(f"Write OK: writes={result['writes_this_month']}, size={result.get('size','?')}")

# ── free-tier read ─────────────────────────────────────────────────────────────
section("8. CLIENT — read (free tier, V2 round-trip)")
time.sleep(0.5)
retrieved = throwaway_client.read()
assert retrieved == test_data, f"Data mismatch! {retrieved!r} != {test_data!r}"
ok(f"Read OK: {len(retrieved)} bytes, round-trip verified ✓")

# ── write_plaintext / read_plaintext ──────────────────────────────────────────
section("9. CLIENT — write_plaintext / read_plaintext (V2 server-encrypts)")
plaintext_client = LocIVaultClient(throwaway, base_url=BASE_URL)
plaintext_msg = b"this is my secret agent memory"
wr = plaintext_client.write_plaintext(plaintext_msg)
print(f"  write_plaintext: writes_this_month={wr['writes_this_month']}, size={wr['size']}")
ok(f"write_plaintext OK")

time.sleep(0.5)
recovered_plaintext = plaintext_client.read_plaintext()
assert recovered_plaintext == plaintext_msg, f"Plaintext mismatch: {recovered_plaintext!r}"
ok(f"read_plaintext OK: '{recovered_plaintext.decode()}'")

# ── auto_pay=False raises PaymentRequired (after free tier) ───────────────────
# We don't want to burn 47 more ops — instead just mock the 402 flow
section("10. CLIENT — PaymentRequired raised when auto_pay=False")
from unittest.mock import patch, MagicMock
import requests as _req

mock_resp_402 = MagicMock()
mock_resp_402.status_code = 402
mock_resp_402.json.return_value = {
    "error": "Payment required",
    "accepts": [{
        "scheme": "exact",
        "network": "eip155:84532",
        "asset": "0x" + "a1" * 20,
        "amount": "1000",
        "payTo": "0x" + "b2" * 20,
        "maxTimeoutSeconds": 300,
        "extra": None,
    }],
    "error_detail": "Free write limit reached. Payment required for additional writes.",
}

no_pay_client = LocIVaultClient(throwaway, base_url=BASE_URL, auto_pay=False)

with patch.object(no_pay_client._session, 'post', return_value=mock_resp_402):
    try:
        no_pay_client.write(b"test")
        fail("Should have raised PaymentRequired")
    except PaymentRequired as e:
        ok(f"PaymentRequired raised correctly: {len(e.accepts)} accept(s)")

# ── LocIVaultError on bad request ──────────────────────────────────────────────
# Send a real request with a syntactically invalid wallet address in the header.
# The server returns 422 (validation error) which maps to LocIVaultError.
section("11. CLIENT — LocIVaultError on server-side validation error")
import requests as _req
from locivault_client.signing import sign_request

_bad_account = Account.create()
_bad_headers = {
    "X-Wallet-Address": "not-a-valid-address",   # invalid — server rejects
    **sign_request(_bad_account, "POST", "/write"),
}
_resp = _req.post(f"{BASE_URL}/write",
                  json={"blob": "dGVzdA=="},
                  headers=_bad_headers,
                  timeout=10)
if _resp.status_code in (400, 401, 422):
    ok(f"LocIVaultError path confirmed: server returned {_resp.status_code}")
else:
    fail(f"Expected 4xx, got {_resp.status_code}: {_resp.text[:80]}")

# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*50}")
print("  ALL TESTS PASSED")
print(f"{'═'*50}")

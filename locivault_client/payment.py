"""
x402 payment signing for LocIVault.

This module handles the client side of x402:
  1. Parse the 402 response body to get payment requirements
  2. Sign an EIP-3009 authorization off-chain (no gas needed)
  3. Return a JSON string suitable for the X-Payment header

The server verifies + settles on its end. The agent never submits a transaction.
Network: Base Sepolia (eip155:84532) for testnet, Base mainnet (eip155:8453) for prod.
"""

import time
import json
import logging

log = logging.getLogger("locivault.payment")

try:
    from x402.client import x402ClientSync
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.schemas.payments import PaymentRequired, PaymentRequirements
    _X402_AVAILABLE = True
except ImportError:
    _X402_AVAILABLE = False


class PaymentSigner:
    """
    Signs x402 payment authorizations for LocIVault ops.

    Args:
        account: eth_account LocalAccount (the payer)
        network: EIP-155 chain ID string, e.g. "eip155:8453" (Base mainnet)
    """

    def __init__(self, account, network: str = "eip155:8453"):
        if not _X402_AVAILABLE:
            raise ImportError(
                "x402 is required for payment signing. "
                "Install it with: pip install 'x402[evm]'"
            )
        self.network = network
        signer = EthAccountSigner(account)
        self._client = x402ClientSync()
        self._client.register(network, ExactEvmScheme(signer=signer))

    def sign(self, resource_url: str, accepts: list) -> str:
        """
        Sign a payment for the given resource.

        Args:
            resource_url: full URL of the resource being paid for (e.g. https://locivault.fly.dev/write)
            accepts:      the "accepts" list from the 402 response body

        Returns:
            JSON string to use as the X-Payment header value
        """
        acc = accepts[0]
        requirements = PaymentRequirements(
            scheme=acc["scheme"],
            network=acc["network"],
            asset=acc["asset"],
            amount=str(acc["amount"]),
            pay_to=acc["payTo"],
            max_timeout_seconds=acc.get("maxTimeoutSeconds", 300),
            extra=acc.get("extra"),
            resource=resource_url,
            description="LocIVault op",
            mime_type="application/json",
            output_schema=None,
            request_hash="",
        )
        payment_required = PaymentRequired(x402_version=2, accepts=[requirements])
        payload = self._client.create_payment_payload(payment_required)
        return payload.model_dump_json()

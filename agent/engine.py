"""The core engine — the one place the "brain vs hands" and "the limit
check happens in code, not in a prompt" rules (README section 2) are
actually implemented. The library, the FastAPI service (phase 4) and the
MCP tool server (phase 6) are all thin wrappers over this class; none of
them re-implement the limit check.
"""

from decimal import Decimal

from agent.adapters import ap2, erc20, mpp, pay_button as pay_button_adapter, x402
from agent.adapters.ap2 import Ap2Error
from agent.config import Settings
from agent.ledger import STATUS_FAILED, STATUS_REJECTED, STATUS_SETTLED, Ledger, PaymentRecord
from agent.limit_check import LimitChecker, LimitCheckResult
from agent.limits_client import MorambaAgentClient
from agent.signing import Wallet, load_wallet
from agent.sync import SyncClient


def _parse_caip2_chain_id(network: str) -> int | None:
    """"eip155:84532" -> 84532. Returns None for a network id this doesn't
    recognize rather than raising — chain_id is informational on the
    ledger row, not load-bearing for the limit check."""
    if ":" not in network:
        return None
    _, _, suffix = network.partition(":")
    return int(suffix) if suffix.isdigit() else None


class Agent:
    def __init__(self, settings: Settings):
        self._settings = settings
        self.wallet: Wallet = load_wallet(settings.wallet_private_key)
        self.ledger = Ledger(settings.db_path)
        self._agents_client = MorambaAgentClient(settings.moramba_api_base_url)
        self._limit_checker = LimitChecker(self.ledger, self._agents_client, settings.moramba_agent_id)
        self._sync_client = SyncClient(settings.moramba_api_base_url, settings.moramba_agent_id, self.wallet)

    def close(self) -> None:
        self._agents_client.close()
        self._sync_client.close()
        self.ledger.close()

    def check_spend_limits(self, *, recipient: str, token: str, amount: Decimal, rail: str) -> LimitCheckResult:
        """Dry run — same check a real payment would go through, without
        spending anything. This is what the MCP `check_spend_limits` tool
        and the FastAPI `GET /limits` route both call."""
        return self._limit_checker.check(
            wallet_address=self.wallet.address, recipient=recipient, token=token, amount=amount, rail=rail
        )

    def pay_via_mpp(
        self,
        *,
        receiver_base_url: str,
        receiver_agent_id: str,
        amount: Decimal,
        token: str,
        payment_via: str = "agent",
        payment_to: str = "agent",
        payout_agent_id: str | None = None,
        to_address: str | None = None,
    ) -> PaymentRecord:
        recipient = payout_agent_id or to_address or receiver_agent_id
        check = self.check_spend_limits(recipient=recipient, token=token, amount=amount, rail="mpp")
        if not check.allowed:
            return self.ledger.record(
                rail="mpp", recipient=recipient, token=token, amount=amount,
                status=STATUS_REJECTED, reason=check.reason,
            )

        result = mpp.pay(
            receiver_base_url=receiver_base_url,
            receiver_agent_id=receiver_agent_id,
            wallet=self.wallet,
            amount=amount,
            token=token,
            payment_via=payment_via,
            payment_to=payment_to,
            payout_agent_id=payout_agent_id,
            to_address=to_address,
        )
        record = self.ledger.record(
            rail="mpp", recipient=recipient, token=token, amount=amount,
            status=STATUS_SETTLED if result.success else STATUS_FAILED,
            tx_hash=result.tx_hash, signature=result.signature, reason=result.error,
            raw_request=result.raw_request, raw_response=result.raw_response,
        )
        self._sync_client.sync_pending(self.ledger)
        return record

    def transfer_erc20(
        self,
        *,
        to_address: str,
        amount: Decimal,
        token: str,
        token_contract_address: str | None = None,
    ) -> PaymentRecord:
        """Every token this agent is configured for is payable by name —
        not only the one contract address baked into
        `DEFAULT_TOKEN_CONTRACT` at setup time. `token_contract_address`
        still overrides both the contract and (implicitly) the network,
        for a token this agent doesn't have in its own payout_tokens."""
        chain_id = self._settings.chain_id
        rpc_url = self._settings.rpc_url
        contract_address = token_contract_address

        if contract_address is None:
            limits = self._agents_client.get_agent(self._settings.moramba_agent_id)
            resolved = limits.resolve_payout_token(token)
            if resolved is not None and resolved.token_address:
                contract_address = resolved.token_address
                if resolved.chain and resolved.rpc_url:
                    # A payout token can live on a different chain than
                    # this wallet's static .env network (same reasoning
                    # as pay_agent's destination-token lookup) — prefer
                    # the token's own chain/rpc when Moramba reports one.
                    chain_id = resolved.chain
                    rpc_url = resolved.rpc_url
            elif limits.payout_tokens:
                # The agent DOES have a specific, restricted set of
                # tokens and this one isn't in it — reject directly
                # rather than silently falling back to a default
                # contract that belongs to a different token entirely.
                accepted = [t.token_name for t in limits.payout_tokens]
                return self.ledger.record(
                    rail="erc20", recipient=to_address, token=token, amount=amount,
                    status=STATUS_REJECTED,
                    reason=f"token {token!r} is not supported by this agent — accepts: {accepted}",
                )
            else:
                # No restriction configured at all — fall back to the
                # single legacy default, same as before this method knew
                # how to resolve tokens by name.
                contract_address = self._settings.default_token_contract

        if not contract_address:
            return self.ledger.record(
                rail="erc20", recipient=to_address, token=token, amount=amount,
                status=STATUS_REJECTED, reason="no token_contract_address given and no default_token_contract configured",
            )

        check = self.check_spend_limits(recipient=to_address, token=token, amount=amount, rail="erc20")
        if not check.allowed:
            return self.ledger.record(
                rail="erc20", recipient=to_address, token=token, amount=amount,
                status=STATUS_REJECTED, reason=check.reason,
            )

        result = erc20.pay(
            rpc_url=rpc_url,
            chain_id=chain_id,
            account=self.wallet._account,
            token_contract_address=contract_address,
            to_address=to_address,
            amount=amount,
        )
        record = self.ledger.record(
            rail="erc20", recipient=to_address, token=token, amount=amount,
            chain_id=chain_id,
            status=STATUS_SETTLED if result.success else STATUS_FAILED,
            tx_hash=result.tx_hash, reason=result.error,
            raw_request=result.raw_request, raw_response=result.raw_response,
        )
        self._sync_client.sync_pending(self.ledger)
        return record

    def pay_agent(
        self,
        *,
        receiving_agent_id: str,
        amount: Decimal,
        token: str | None = None,
    ) -> PaymentRecord:
        """Pay another Moramba agent directly by its `agent_id` — a payout
        agent paying a receiving agent. The destination wallet and
        accepted tokens come from that agent's own DB record
        (`receiving_config`, resolved via `MorambaAgentClient.get_receiving_agent`),
        never from a caller-supplied address — a receiving agent can only
        ever be paid, never pay (enforced server-side too: see
        `sync_agent_payment_service`'s `is_receiving_agent` guard).
        """
        try:
            receiving_agent = self._agents_client.get_receiving_agent(receiving_agent_id)
        except Ap2Error as exc:
            return self.ledger.record(
                rail="agent_transfer", recipient=receiving_agent_id, token=token or "unknown", amount=amount,
                status=STATUS_REJECTED, reason=str(exc), receiving_agent_id=receiving_agent_id,
            )

        if not receiving_agent.is_active:
            return self.ledger.record(
                rail="agent_transfer", recipient=receiving_agent_id, token=token or "unknown", amount=amount,
                status=STATUS_REJECTED, reason=f"agent {receiving_agent_id} is not active",
                receiving_agent_id=receiving_agent_id,
            )

        if token is None:
            if len(receiving_agent.accepted_tokens) != 1:
                names = [t.token_name for t in receiving_agent.accepted_tokens]
                return self.ledger.record(
                    rail="agent_transfer", recipient=receiving_agent_id, token="unknown", amount=amount,
                    status=STATUS_REJECTED,
                    reason=f"token is required — agent accepts: {names}" if names
                    else "agent has no accepted tokens configured",
                    receiving_agent_id=receiving_agent_id,
                )
            resolved_token = receiving_agent.accepted_tokens[0]
        else:
            resolved_token = receiving_agent.resolve_token(token)
            if resolved_token is None:
                accepted = [t.token_name for t in receiving_agent.accepted_tokens]
                return self.ledger.record(
                    rail="agent_transfer", recipient=receiving_agent_id, token=token, amount=amount,
                    status=STATUS_REJECTED, reason=f"agent does not accept {token!r} — accepts: {accepted}",
                    receiving_agent_id=receiving_agent_id,
                )

        to_address = receiving_agent.receive_wallet_address
        check = self.check_spend_limits(
            recipient=to_address, token=resolved_token.token_name, amount=amount, rail="agent_transfer"
        )
        if not check.allowed:
            return self.ledger.record(
                rail="agent_transfer", recipient=to_address, token=resolved_token.token_name, amount=amount,
                status=STATUS_REJECTED, reason=check.reason, receiving_agent_id=receiving_agent_id,
            )

        result = erc20.pay(
            rpc_url=resolved_token.rpc_url,
            chain_id=resolved_token.chain,
            account=self.wallet._account,
            token_contract_address=resolved_token.token_address,
            to_address=to_address,
            amount=amount,
        )
        record = self.ledger.record(
            rail="agent_transfer", recipient=to_address, token=resolved_token.token_name, amount=amount,
            chain_id=resolved_token.chain,
            status=STATUS_SETTLED if result.success else STATUS_FAILED,
            tx_hash=result.tx_hash, reason=result.error,
            receiving_agent_id=receiving_agent_id,
            raw_request=result.raw_request, raw_response=result.raw_response,
        )
        self._sync_client.sync_pending(self.ledger)
        return record

    def pay_via_x402(
        self,
        *,
        url: str,
        method: str = "GET",
        token_decimals: int = 6,
        **request_kwargs,
    ) -> PaymentRecord | None:
        """Unlike the other rails, the amount/token/recipient here aren't
        known until the resource's own 402 challenge arrives — so the
        limit check (README section 2) runs *between* `x402.probe()` and
        `x402.settle()`, never before. `token_decimals` defaults to 6
        (USDC, the only token real x402 deployments in this family use);
        pass the right value for another asset.

        Returns `None` when the resource didn't actually require payment
        (no 402) — that's not a payment attempt, so nothing is recorded.
        """
        probe_result = x402.probe(url, method=method, **request_kwargs)
        if probe_result.requirement is None:
            return None

        req = probe_result.requirement
        amount = Decimal(req.amount_atomic) / (Decimal(10) ** token_decimals)
        token_label = req.extra.get("name") or req.asset
        chain_id = _parse_caip2_chain_id(req.network)

        check = self.check_spend_limits(recipient=req.pay_to, token=token_label, amount=amount, rail="x402")
        if not check.allowed:
            return self.ledger.record(
                rail="x402", recipient=req.pay_to, token=token_label, amount=amount,
                chain_id=chain_id, status=STATUS_REJECTED, reason=check.reason,
            )

        result = x402.settle(url, wallet=self.wallet, probe_result=probe_result, method=method, **request_kwargs)
        record = self.ledger.record(
            rail="x402", recipient=req.pay_to, token=token_label, amount=amount,
            chain_id=chain_id,
            status=STATUS_SETTLED if result.success else STATUS_FAILED,
            tx_hash=result.tx_hash, reason=result.error,
            raw_request=result.raw_request, raw_response=result.raw_response,
        )
        self._sync_client.sync_pending(self.ledger)
        return record

    def pay_via_ap2(
        self,
        *,
        items: list[dict],
        buyer_email: str,
        buyer: dict | None = None,
        delivery_address: dict | None = None,
        api_key: str | None = None,
    ) -> PaymentRecord:
        """Autonomous AP2 checkout — no human present. Like x402, two-phase:
        the checkout amount isn't known until `create_checkout_session`
        returns, so the limit check runs between session creation and the
        Closed Mandate signature (`authorize_autonomous`) — nothing is
        signed before the check passes.

        `token` for the limit check/ledger is the checkout's fiat currency
        (e.g. "USD"), not the crypto token actually settled on-chain — that
        isn't known until deep into the flow (`fetch_payrequest_init`,
        after the mandate is already signed). A `token_allowed` allow-list
        keyed on crypto symbols won't match a fiat currency code; leave it
        unrestricted, or include the currency code, if this rail is used.
        """
        key = api_key or self._settings.moramba_acp_api_key
        recipient = items[0].get("id", "unknown") if items else "unknown"
        if not key:
            return self.ledger.record(
                rail="ap2", recipient=recipient, token="USD", amount=Decimal("0"),
                status=STATUS_REJECTED, reason="no moramba_acp_api_key configured",
            )

        session = ap2.create_checkout_session(
            self._settings.moramba_api_base_url, key, items, buyer=buyer, delivery_address=delivery_address
        )
        amount = Decimal(session.amount) / 100
        recipient = items[0].get("id", session.session_id)

        check = self.check_spend_limits(recipient=recipient, token=session.currency, amount=amount, rail="ap2")
        if not check.allowed:
            return self.ledger.record(
                rail="ap2", recipient=recipient, token=session.currency, amount=amount,
                status=STATUS_REJECTED, reason=check.reason,
                raw_request={"session_id": session.session_id}, raw_response=session.raw,
            )

        result = ap2.settle_autonomous_checkout(
            base_url=self._settings.moramba_api_base_url, api_key=key, agent_id=self._settings.moramba_agent_id,
            wallet=self.wallet, session=session, buyer_email=buyer_email,
        )
        record = self.ledger.record(
            rail="ap2", recipient=recipient, token=session.currency, amount=amount,
            status=STATUS_SETTLED if result.success else STATUS_FAILED,
            tx_hash=result.tx_hash, reason=result.error,
            raw_request={"session_id": session.session_id, "flow": result.flow}, raw_response=session.raw,
        )
        self._sync_client.sync_pending(self.ledger)
        return record

    def pay_via_pay_button(
        self,
        *,
        button_id: str,
        network: str | None = None,
        amount: Decimal | None = None,
    ) -> PaymentRecord:
        """Pay a Moramba Pay Button directly by its `button_id` — no ACP
        checkout session or AP2 mandate involved. Two-phase like x402/AP2,
        for a slightly different reason: resolving the button's own
        amount/token/recipient (`resolve_payment_plan`) means scraping its
        payment page, which is read-only — but actually creating a payin
        (`pay_button_adapter.pay_button`) is a real, one-shot server-side
        side effect. The limit check has to run between those two steps,
        so a rejection never causes that side effect.
        """
        try:
            plan = pay_button_adapter.resolve_payment_plan(
                self._settings.moramba_api_base_url, button_id, network=network, amount=amount
            )
        except pay_button_adapter.Ap2Error as exc:
            return self.ledger.record(
                rail="pay_button", recipient=button_id, token="unknown", amount=amount or Decimal("0"),
                status=STATUS_REJECTED, reason=str(exc),
            )

        recipient = plan.method.to_wallet_address
        token = plan.method.token_name

        check = self.check_spend_limits(recipient=recipient, token=token, amount=plan.amount, rail="pay_button")
        if not check.allowed:
            return self.ledger.record(
                rail="pay_button", recipient=recipient, token=token, amount=plan.amount,
                status=STATUS_REJECTED, reason=check.reason,
                raw_request={"button_id": button_id, "network": plan.method.network},
            )

        result = pay_button_adapter.pay_button(self._settings.moramba_api_base_url, button_id, plan, self.wallet)
        record = self.ledger.record(
            rail="pay_button", recipient=recipient, token=token, amount=plan.amount,
            status=STATUS_SETTLED if result.success else STATUS_FAILED,
            tx_hash=result.tx_hash, reason=result.error,
            raw_request={
                "button_id": button_id, "network": plan.method.network,
                "flow": result.flow, "payin_id": result.payin_id,
            },
        )
        self._sync_client.sync_pending(self.ledger)
        return record

    def history(
        self, *, since: str | None = None, recipient: str | None = None, rail: str | None = None, limit: int = 100
    ) -> list[PaymentRecord]:
        return self.ledger.history(since=since, recipient=recipient, rail=rail, limit=limit)

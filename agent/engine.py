"""The core engine — the one place the "brain vs hands" and "the limit
check happens in code, not in a prompt" rules (README section 2) are
actually implemented. The library, the FastAPI service (phase 4) and the
MCP tool server (phase 6) are all thin wrappers over this class; none of
them re-implement the limit check.
"""

from decimal import Decimal

from agent.adapters import ap2, erc20, mpp, pay_button as pay_button_adapter, payin as payin_adapter, x402
from agent.adapters.ap2 import Ap2Error
from agent.config import Settings
from agent.ledger import STATUS_FAILED, STATUS_REJECTED, STATUS_SETTLED, Ledger, PaymentRecord
from agent.limit_check import LimitChecker, LimitCheckResult
from agent.limits_client import MorambaAgentClient
from agent.signing import Wallet, load_wallet
from agent.sync import SyncClient


# Same Tempo testnet/mainnet chain ids as setup.py's KNOWN_CHAIN_RPCS —
# used both to break a tie when a pay-button method exists for the same
# preferred token on more than one network, and to hard-reject a
# resolved method whose network doesn't match what this agent is
# actually configured for on that token (see pay_via_pay_button).
_CHAIN_NETWORK_HINT: dict[int, str] = {
    42431: "testnet",
    4217: "mainnet",
}


def _network_slug_hint(network_slug: str) -> str | None:
    """`ButtonPayoutMethod.network` is a slug like "tempo_testnet" with no
    chain id attached — this is the only way to tell which of
    _CHAIN_NETWORK_HINT's chains it corresponds to."""
    lowered = network_slug.lower()
    return next((hint for hint in _CHAIN_NETWORK_HINT.values() if hint in lowered), None)


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
        gas_limit: int | None = None,
    ) -> PaymentRecord:
        """Every token this agent is configured for is payable by name —
        the agent only ever pays what it's explicitly configured for,
        never an arbitrary token, so an unresolvable `token` is rejected
        outright rather than falling back to any single default contract
        (an unconfigured/empty payout_tokens list means "nothing is
        payable", not "anything is"). `token_contract_address` still
        overrides the contract address directly, but `check_spend_limits`
        below still enforces the same rule on `token`'s name regardless.

        `gas_limit` is normally left unset — `erc20.pay` estimates it live
        per-transaction. It exists as an explicit escape hatch for when
        that estimate itself isn't obtainable (some RPC nodes don't
        support `eth_estimateGas` reliably) and the safety-net fallback
        undershoots a specific token's real cost — a caller (or an AI
        agent, after getting a human's go-ahead, since this raises how
        much a single transfer can spend on gas) can then retry with an
        explicit value instead of failing the same way indefinitely."""
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
            else:
                accepted = [t.token_name for t in limits.payout_tokens]
                reason = (
                    f"token {token!r} is not supported by this agent — accepts: {accepted}"
                    if accepted
                    else f"token {token!r} is not supported — this agent has no payout tokens configured"
                )
                return self.ledger.record(
                    rail="erc20", recipient=to_address, token=token, amount=amount,
                    status=STATUS_REJECTED, reason=reason,
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
            gas_limit=gas_limit,
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

        `token` for the limit check/ledger is `session.currency` — the
        settlement token's own name (e.g. "PATHUSD"), uppercased, exactly
        as Moramba's own checkout session reports it
        (`currency_for` in acp_checkout_service.rs), not a fiat code. It's
        checked against `allowed_tokens` the same as every other rail.
        `session.amount` is converted using `session.decimals` (also read
        from the session's own `payment_options`, not assumed) — found
        live (2026-10-02): assuming a fixed 2 decimals (fiat cents) turned
        a real 1.5 pathUSD checkout into 15,000, which then looked like a
        spend-limit violation.
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
        amount = Decimal(session.amount) / (Decimal(10) ** session.decimals)
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
            tx_hash=result.tx_hash, reason=result.error, chain_id=result.chain_id,
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

        When `network` isn't given, a multi-token (and/or multi-network)
        button's method is chosen to match this agent's own
        `allowed_tokens` where possible — a button that accepts several
        tokens, or the same token on more than one network, shouldn't get
        paid via whichever method happens to be listed first if that one
        isn't actually usable by this agent. That preference is then
        enforced, not just hoped for: the resolved method's network must
        match this specific token's own configured chain, or the payment
        is rejected outright rather than settling on a network this agent
        isn't actually allowed to use it on (an explicit `network` is
        trusted as the caller's own deliberate choice and skips this).
        """
        limits = None
        preferred_tokens = None
        preferred_network_hint = None
        if network is None:
            limits = self._agents_client.get_agent(self._settings.moramba_agent_id)
            preferred_tokens = limits.allowed_tokens
            preferred_network_hint = _CHAIN_NETWORK_HINT.get(self._settings.chain_id)

        try:
            plan = pay_button_adapter.resolve_payment_plan(
                self._settings.moramba_api_base_url, button_id, network=network, amount=amount,
                preferred_tokens=preferred_tokens, preferred_network_hint=preferred_network_hint,
            )
        except pay_button_adapter.Ap2Error as exc:
            return self.ledger.record(
                rail="pay_button", recipient=button_id, token="unknown", amount=amount or Decimal("0"),
                status=STATUS_REJECTED, reason=str(exc),
            )

        recipient = plan.method.to_wallet_address
        token = plan.method.token_name

        if limits is not None:
            matched_token = limits.resolve_payout_token(token)
            if matched_token is not None:
                method_hint = _network_slug_hint(plan.method.network)
                token_hint = _CHAIN_NETWORK_HINT.get(matched_token.chain)
                if method_hint and token_hint and method_hint != token_hint:
                    return self.ledger.record(
                        rail="pay_button", recipient=recipient, token=token, amount=plan.amount,
                        status=STATUS_REJECTED,
                        reason=(
                            f"button's {token!r} method is on {plan.method.network!r}, but this agent's "
                            f"{token!r} is configured for chain {matched_token.chain} ({token_hint})"
                        ),
                        raw_request={"button_id": button_id, "network": plan.method.network},
                    )

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

    def pay_via_payin_id(self, *, payin_id: str) -> PaymentRecord:
        """Pay an existing payin directly by its `payin_id` — no button,
        no ACP checkout session, no mandate. The amount/token/recipient
        come from Moramba's own `payrequest/init` lookup (read-only, no
        side effect) — this rail never creates the payin itself, since
        the caller already has one from somewhere else (a dashboard,
        another system, a human). Still subject to the same local limit
        check and token allow-list as every other rail."""
        try:
            plan = payin_adapter.resolve_payin(self._settings.moramba_api_base_url, payin_id, self.wallet)
        except payin_adapter.Ap2Error as exc:
            return self.ledger.record(
                rail="payin", recipient=payin_id, token="unknown", amount=Decimal("0"),
                status=STATUS_REJECTED, reason=str(exc),
            )

        recipient = plan.init.to
        token = plan.init.token_name or "unknown"

        check = self.check_spend_limits(recipient=recipient, token=token, amount=plan.amount, rail="payin")
        if not check.allowed:
            return self.ledger.record(
                rail="payin", recipient=recipient, token=token, amount=plan.amount,
                status=STATUS_REJECTED, reason=check.reason,
                raw_request={"payin_id": payin_id},
            )

        result = payin_adapter.pay_payin(self._settings.moramba_api_base_url, payin_id, plan, self.wallet)
        record = self.ledger.record(
            rail="payin", recipient=recipient, token=token, amount=plan.amount,
            chain_id=plan.init.chain_id,
            status=STATUS_SETTLED if result.success else STATUS_FAILED,
            tx_hash=result.tx_hash, reason=result.error,
            raw_request={"payin_id": payin_id, "flow": result.flow},
        )
        self._sync_client.sync_pending(self.ledger)
        return record

    def history(
        self, *, since: str | None = None, recipient: str | None = None, rail: str | None = None, limit: int = 100
    ) -> list[PaymentRecord]:
        return self.ledger.history(since=since, recipient=recipient, rail=rail, limit=limit)

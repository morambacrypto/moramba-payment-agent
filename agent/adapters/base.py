from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PaymentResult:
    success: bool
    tx_hash: str | None = None
    signature: str | None = None
    raw_request: dict[str, Any] | None = None
    raw_response: dict[str, Any] | None = None
    error: str | None = None

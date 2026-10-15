"""Deterministic in-process mock payment service.

Implements BR01-BR06 (docs/07_Test_Automation_Design.md) with scripted
timeout/verification/callback outcomes. No HTTP, no network, no randomness,
no sleeps. The duplicate-ledger defect (BR06 violation) is controlled by
AITE_DUPLICATE_LEDGER_DEFECT (default true).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class MockPaymentError(ValueError):
    """Raised for invalid use of the mock service (not a business rejection)."""


@dataclass
class Payment:
    payment_id: str
    idempotency_key: str
    amount: str
    currency: str
    state: str  # "UNRESOLVED" or "SUCCESS"


@dataclass
class GatewayCharge:
    charge_id: str
    payment_id: str
    idempotency_key: str
    amount: str
    currency: str


@dataclass
class LedgerEntry:
    entry_id: str
    payment_id: str
    amount: str
    currency: str
    callback_id: str | None = None


@dataclass
class Event:
    sequence: int
    action: str
    payment_id: str | None = None
    key: str | None = None
    callback_id: str | None = None
    details: dict = field(default_factory=dict)


def _resolve_defect_flag(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    raw = os.environ.get("AITE_DUPLICATE_LEDGER_DEFECT", "true")
    return raw.strip().lower() in ("1", "true", "yes", "on")


class MockPaymentService:
    """Fresh, isolated, in-process mock of the payment gateway and ledger."""

    def __init__(self, duplicate_ledger_defect: bool | None = None) -> None:
        self.duplicate_ledger_defect = _resolve_defect_flag(duplicate_ledger_defect)
        self.payments: dict[str, Payment] = {}
        self.key_index: dict[str, dict] = {}
        self.gateway_charges: list[GatewayCharge] = []
        self.ledger: list[LedgerEntry] = []
        self.events: list[Event] = []
        self._callback_deliveries: dict[tuple[str, str], int] = {}
        self._sequence = 0

    # -- internal helpers -------------------------------------------------

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _log_event(self, action: str, **kwargs) -> Event:
        event = Event(sequence=self._next_sequence(), action=action, **kwargs)
        self.events.append(event)
        return event

    # -- logical operations (07 Section 2) ---------------------------------

    def submit_payment(
        self,
        payment_id: str,
        key: str,
        amount: str,
        currency: str,
        gateway_outcome: str = "SUCCESS",
    ) -> dict:
        if not key:
            raise MockPaymentError("idempotency key must be nonempty (BR01)")
        if key in self.key_index:
            raise MockPaymentError(
                "key already used; use retry_payment for a subsequent request"
            )
        if gateway_outcome not in ("SUCCESS", "TIMEOUT"):
            raise MockPaymentError(f"unsupported gateway_outcome: {gateway_outcome}")

        self.key_index[key] = {
            "payment_id": payment_id,
            "amount": amount,
            "currency": currency,
        }

        charge_id = f"charge-{len(self.gateway_charges) + 1}"
        state = "SUCCESS" if gateway_outcome == "SUCCESS" else "UNRESOLVED"
        self.payments[payment_id] = Payment(payment_id, key, amount, currency, state)

        self._log_event(
            "submit", payment_id=payment_id, key=key, details={"outcome": gateway_outcome}
        )
        self.gateway_charges.append(
            GatewayCharge(charge_id, payment_id, key, amount, currency)
        )
        self._log_event(
            "gateway_charge",
            payment_id=payment_id,
            key=key,
            details={"charge_id": charge_id},
        )

        if state == "SUCCESS":
            self._post_ledger_entry(payment_id, amount, currency, callback_id=None)

        return self.get_payment_snapshot(payment_id)

    def retry_payment(
        self,
        key: str,
        amount: str,
        currency: str,
        verification_outcome: str | None = None,
    ) -> dict:
        if key not in self.key_index:
            raise MockPaymentError(f"unknown idempotency key: {key}")

        original = self.key_index[key]
        payment_id = original["payment_id"]

        if amount != original["amount"] or currency != original["currency"]:
            self._log_event(
                "conflict_rejected",
                payment_id=payment_id,
                key=key,
                details={"attempted_amount": amount, "attempted_currency": currency},
            )
            return {
                "status": "CONFLICT_REJECTED",
                "payment_id": payment_id,
                **self.get_payment_snapshot(payment_id),
            }

        self._log_event("retry", payment_id=payment_id, key=key, details={"payload": "identical"})

        payment = self.payments[payment_id]
        if payment.state == "SUCCESS":
            self._log_event(
                "status_verification",
                payment_id=payment_id,
                key=key,
                details={"result": "SUCCESS", "already_resolved": True},
            )
            return self.get_payment_snapshot(payment_id)

        if verification_outcome not in ("SUCCESS", "UNRESOLVED"):
            raise MockPaymentError(
                "verification_outcome must be SUCCESS or UNRESOLVED when the "
                "original payment is still UNRESOLVED"
            )

        self._log_event(
            "status_verification",
            payment_id=payment_id,
            key=key,
            details={"result": verification_outcome},
        )

        if verification_outcome == "SUCCESS":
            self._post_ledger_entry(payment_id, payment.amount, payment.currency, callback_id=None)
            payment.state = "SUCCESS"

        return self.get_payment_snapshot(payment_id)

    def send_callback(self, payment_id: str, callback_id: str, outcome: str = "SUCCESS") -> dict:
        if payment_id not in self.payments:
            raise MockPaymentError(f"unknown payment_id: {payment_id}")
        if outcome != "SUCCESS":
            raise MockPaymentError("only SUCCESS callbacks are scripted in this suite")

        delivery_key = (payment_id, callback_id)
        delivery_number = self._callback_deliveries.get(delivery_key, 0) + 1
        self._callback_deliveries[delivery_key] = delivery_number

        self._log_event(
            "callback_delivered",
            payment_id=payment_id,
            callback_id=callback_id,
            details={"delivery_number": delivery_number, "outcome": outcome},
        )

        payment = self.payments[payment_id]

        if delivery_number == 1:
            payment.state = "SUCCESS"
            if self.count_ledger_entries(payment_id) == 0:
                self._post_ledger_entry(
                    payment_id, payment.amount, payment.currency, callback_id=callback_id
                )
        elif self.duplicate_ledger_defect:
            self._post_ledger_entry(
                payment_id, payment.amount, payment.currency, callback_id=callback_id
            )

        return self.get_payment_snapshot(payment_id)

    def _post_ledger_entry(
        self, payment_id: str, amount: str, currency: str, callback_id: str | None
    ) -> None:
        entry_id = f"ledger-{len(self.ledger) + 1}"
        self.ledger.append(LedgerEntry(entry_id, payment_id, amount, currency, callback_id))
        self._log_event(
            "ledger_posting",
            payment_id=payment_id,
            callback_id=callback_id,
            details={"entry_id": entry_id},
        )

    # -- read-only observation primitives (07 Section 3, BDD mapping) ------

    def get_payment_status(self, payment_id: str) -> str:
        return self.payments[payment_id].state

    def get_payment_snapshot(self, payment_id: str) -> dict:
        payment = self.payments[payment_id]
        return {
            "payment_id": payment.payment_id,
            "state": payment.state,
            "amount": payment.amount,
            "currency": payment.currency,
        }

    def count_gateway_charges(self, payment_id: str) -> int:
        return sum(1 for c in self.gateway_charges if c.payment_id == payment_id)

    def count_ledger_entries(self, payment_id: str) -> int:
        return sum(1 for e in self.ledger if e.payment_id == payment_id)

    def get_events(self, payment_id: str | None = None, action: str | None = None) -> list[Event]:
        events = self.events
        if payment_id is not None:
            events = [e for e in events if e.payment_id == payment_id]
        if action is not None:
            events = [e for e in events if e.action == action]
        return events

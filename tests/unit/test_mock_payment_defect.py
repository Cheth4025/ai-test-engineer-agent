"""Unit tests for MockPaymentService's duplicate-ledger defect toggle.

Not payment business-rule tests -- never part of the T01-T05 batch. Exercises
the AITE_DUPLICATE_LEDGER_DEFECT flag resolution directly (constructor
override, env default, env override).
"""

from __future__ import annotations

from ai_test_engineer.mock_payment import MockPaymentService

PAYMENT_ID = "P-001"
KEY = "K-001"
AMOUNT = "100.00"
CURRENCY = "USD"
CALLBACK_ID = "C-001"


def _establish_success_via_callback(service: MockPaymentService) -> None:
    service.submit_payment(PAYMENT_ID, KEY, AMOUNT, CURRENCY, gateway_outcome="TIMEOUT")
    service.send_callback(PAYMENT_ID, CALLBACK_ID, outcome="SUCCESS")


def test_defect_on_duplicate_callback_creates_second_ledger_entry():
    service = MockPaymentService(duplicate_ledger_defect=True)
    _establish_success_via_callback(service)
    assert service.count_ledger_entries(PAYMENT_ID) == 1

    service.send_callback(PAYMENT_ID, CALLBACK_ID, outcome="SUCCESS")

    assert service.count_ledger_entries(PAYMENT_ID) == 2
    assert service.count_gateway_charges(PAYMENT_ID) == 1
    assert service.get_payment_status(PAYMENT_ID) == "SUCCESS"


def test_defect_off_duplicate_callback_keeps_single_ledger_entry():
    service = MockPaymentService(duplicate_ledger_defect=False)
    _establish_success_via_callback(service)
    assert service.count_ledger_entries(PAYMENT_ID) == 1

    service.send_callback(PAYMENT_ID, CALLBACK_ID, outcome="SUCCESS")

    assert service.count_ledger_entries(PAYMENT_ID) == 1
    assert service.count_gateway_charges(PAYMENT_ID) == 1
    assert service.get_payment_status(PAYMENT_ID) == "SUCCESS"


def test_defect_defaults_to_true_when_env_unset(monkeypatch):
    monkeypatch.delenv("AITE_DUPLICATE_LEDGER_DEFECT", raising=False)

    service = MockPaymentService()

    assert service.duplicate_ledger_defect is True


def test_defect_env_var_can_disable_it(monkeypatch):
    monkeypatch.setenv("AITE_DUPLICATE_LEDGER_DEFECT", "false")

    service = MockPaymentService()

    assert service.duplicate_ledger_defect is False


def test_defect_env_var_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("AITE_DUPLICATE_LEDGER_DEFECT", "FALSE")

    service = MockPaymentService()

    assert service.duplicate_ledger_defect is False


def test_constructor_override_takes_precedence_over_env(monkeypatch):
    monkeypatch.setenv("AITE_DUPLICATE_LEDGER_DEFECT", "true")

    service = MockPaymentService(duplicate_ledger_defect=False)

    assert service.duplicate_ledger_defect is False

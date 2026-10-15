"""Exactly five predefined executable payment tests: T01-T05.

Sequences, assertions and defect-on expectations are preserved verbatim from
docs/AI_Test_Engineer_Agent_Requirements.md Section 4 and
docs/07_Test_Automation_Design.md Section 3. No xfail, no skip, no loosened
assertions. T04 must fail for real on a ledger-count assertion when the
duplicate-ledger defect is enabled (the default).
"""

from __future__ import annotations

# Fixed fixture values (immutable across the suite): P-001 / K-001 / 100.00 USD / C-001.
PAYMENT_ID = "P-001"
KEY = "K-001"
AMOUNT = "100.00"
CURRENCY = "USD"
CALLBACK_ID = "C-001"


def test_t01_initial_success(mock_service):
    # Given: P-001 with K-001 and 100.00 USD; gateway returns success.
    # When: submit the payment.
    mock_service.submit_payment(PAYMENT_ID, KEY, AMOUNT, CURRENCY, gateway_outcome="SUCCESS")

    # Then: exactly one gateway charge and one successful ledger posting;
    # payment is successful for 100.00 USD.
    assert mock_service.count_gateway_charges(PAYMENT_ID) == 1
    assert mock_service.count_ledger_entries(PAYMENT_ID) == 1
    assert mock_service.get_payment_status(PAYMENT_ID) == "SUCCESS"
    snapshot = mock_service.get_payment_snapshot(PAYMENT_ID)
    assert snapshot["amount"] == AMOUNT
    assert snapshot["currency"] == CURRENCY


def test_t02_timeout_identical_retry(mock_service):
    # Given: the original request with K-001 commits one gateway charge but
    # its response times out.
    mock_service.submit_payment(PAYMENT_ID, KEY, AMOUNT, CURRENCY, gateway_outcome="TIMEOUT")

    # Timeout was recorded as unresolved, not automatically failed.
    assert mock_service.get_payment_status(PAYMENT_ID) == "UNRESOLVED"
    assert mock_service.count_gateway_charges(PAYMENT_ID) == 1

    # When: retry with K-001 and the identical payload; status verification
    # subsequently returns original success.
    mock_service.retry_payment(KEY, AMOUNT, CURRENCY, verification_outcome="SUCCESS")

    # Then: retry returns the original successful payment; exactly one
    # gateway charge and one ledger posting exist.
    assert mock_service.get_payment_status(PAYMENT_ID) == "SUCCESS"
    assert mock_service.count_gateway_charges(PAYMENT_ID) == 1
    assert mock_service.count_ledger_entries(PAYMENT_ID) == 1

    # Status verification precedes any possible new charge: only one
    # gateway_charge event was ever logged, and it precedes verification.
    charge_events = mock_service.get_events(payment_id=PAYMENT_ID, action="gateway_charge")
    verification_events = mock_service.get_events(
        payment_id=PAYMENT_ID, action="status_verification"
    )
    assert len(charge_events) == 1
    assert len(verification_events) == 1
    assert charge_events[0].sequence < verification_events[0].sequence


def test_t03_conflicting_amount(mock_service):
    # Given: K-001 already identifies a successful 100.00 USD payment.
    mock_service.submit_payment(PAYMENT_ID, KEY, AMOUNT, CURRENCY, gateway_outcome="SUCCESS")

    # When: submit K-001 with amount 125.00 USD.
    result = mock_service.retry_payment(KEY, "125.00", CURRENCY)

    # Then: reject the conflicting request; the original amount, successful
    # payment, single gateway charge, and single ledger posting remain
    # unchanged.
    assert result["status"] == "CONFLICT_REJECTED"
    assert mock_service.get_payment_status(PAYMENT_ID) == "SUCCESS"
    snapshot = mock_service.get_payment_snapshot(PAYMENT_ID)
    assert snapshot["amount"] == AMOUNT
    assert snapshot["currency"] == CURRENCY
    assert mock_service.count_gateway_charges(PAYMENT_ID) == 1
    assert mock_service.count_ledger_entries(PAYMENT_ID) == 1


def test_t04_duplicate_callback(mock_service):
    # Given: callback C-001 for P-001 has established success with one
    # ledger posting and one gateway charge.
    mock_service.submit_payment(PAYMENT_ID, KEY, AMOUNT, CURRENCY, gateway_outcome="TIMEOUT")
    mock_service.send_callback(PAYMENT_ID, CALLBACK_ID, outcome="SUCCESS")
    assert mock_service.get_payment_status(PAYMENT_ID) == "SUCCESS"
    assert mock_service.count_gateway_charges(PAYMENT_ID) == 1
    assert mock_service.count_ledger_entries(PAYMENT_ID) == 1

    # When: deliver C-001 again for P-001.
    mock_service.send_callback(PAYMENT_ID, CALLBACK_ID, outcome="SUCCESS")

    # Then: payment amount, currency, successful state, and gateway charge
    # count remain unchanged.
    snapshot = mock_service.get_payment_snapshot(PAYMENT_ID)
    assert snapshot["state"] == "SUCCESS"
    assert snapshot["amount"] == AMOUNT
    assert snapshot["currency"] == CURRENCY
    assert mock_service.count_gateway_charges(PAYMENT_ID) == 1

    # Exactly one ledger posting remains.
    # Defect-on expectation: FAIL here -- expected 1, actual 2.
    assert mock_service.count_ledger_entries(PAYMENT_ID) == 1


def test_t05_delayed_success(mock_service):
    # Given: original request with K-001 commits one charge but times out;
    # first status verification remains unresolved.
    mock_service.submit_payment(PAYMENT_ID, KEY, AMOUNT, CURRENCY, gateway_outcome="TIMEOUT")

    # When: request an identical retry while unresolved.
    mock_service.retry_payment(KEY, AMOUNT, CURRENCY, verification_outcome="UNRESOLVED")

    # Timeout and unresolved verification do not mark failure or create
    # another charge.
    assert mock_service.get_payment_status(PAYMENT_ID) == "UNRESOLVED"
    assert mock_service.count_gateway_charges(PAYMENT_ID) == 1
    assert mock_service.count_ledger_entries(PAYMENT_ID) == 0

    # Deliver the scripted delayed success once; delayed success reconciles
    # P-001.
    mock_service.send_callback(PAYMENT_ID, CALLBACK_ID, outcome="SUCCESS")

    # Then resolve the retry against the original payment.
    final = mock_service.retry_payment(KEY, AMOUNT, CURRENCY)

    # Then: the retry resolves the same payment; exactly one gateway charge
    # and one ledger posting exist.
    assert final["payment_id"] == PAYMENT_ID
    assert final["state"] == "SUCCESS"
    assert mock_service.count_gateway_charges(PAYMENT_ID) == 1
    assert mock_service.count_ledger_entries(PAYMENT_ID) == 1

    # Status verification precedes any possible new charge: only the one
    # original gateway_charge event was ever logged.
    charge_events = mock_service.get_events(payment_id=PAYMENT_ID, action="gateway_charge")
    verification_events = mock_service.get_events(
        payment_id=PAYMENT_ID, action="status_verification"
    )
    assert len(charge_events) == 1
    assert len(verification_events) >= 1
    assert charge_events[0].sequence < verification_events[0].sequence

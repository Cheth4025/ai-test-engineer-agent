# Test Automation and Mock Service Design

**Project:** AI Test Engineer Agent  
**Use case:** Payment Retry & Duplicate Charge Prevention

## 1. Immutable business rules

The following BR01–BR06 are copied from the functional authority without altering the business behavior. The agent requirements are separate; neither model may redefine these rules or the executable assertions.

- **BR01 — Idempotency key:** Every payment request must include a unique idempotency key for its logical payment; retries reuse that key.

- **BR02 — Identical retry:** Reusing the same key with the same payload resolves the original payment and must not create another charge.

- **BR03 — Conflicting amount:** Reusing the same key with a different payment amount is rejected; the original charge and ledger remain unchanged.

- **BR04 — Uncertain timeout:** A timeout must not automatically mark the payment as failed. Retain its unresolved outcome until status verification or a callback establishes the result.

- **BR05 — Verify before retry:** Verify the original payment status before initiating another charge. If already successful, return the original result. If unresolved, do not create a new charge; retain the unresolved outcome until the scripted original success is available.

- **BR06 — Callback idempotency:** Re-delivery of the same successful callback, identified by callback ID and payment ID, must not create another financial ledger posting.

## 2. Mock model and evidence

**Recommended implementation:** A local in-process Python object, constructed fresh by each pytest fixture. No mock HTTP server, database, sleeps, actual gateway, or settlement simulation. Timeouts are deterministic scripted outcomes, not random timing.

| Record | Fields and behavior |
|---|---|
| Payment | payment_id=P-001, key=K-001, amount decimal string 100.00, currency USD, state UNRESOLVED or SUCCESS; initial absence means not yet created |
| Key index | K-001 → original payment ID and original payload; conflicting amount rejects before mutation; no production key-expiration policy |
| Gateway charge | charge_id, original payment/key, amount/currency; counter reflects committed charge, not number of submit/retry calls |
| Ledger entry | unique entry_id, payment_id, successful callback_id (if callback-originated), amount/currency, posting sequence |
| Callback | callback_id=C-001, payment_id=P-001, SUCCESS outcome, original amount/currency |
| Event log | Per-test sequence, action, payment/key/callback identity, before/after state, counters and rejection/status outcome |
| Defect configuration | duplicate_ledger_defect=true or false; default demo true; frozen per run and included in approval bundle |

UNRESOLVED/SUCCESS are technical enum recommendations for the source's uncertain/successful outcomes. Conflict is a rejected request outcome, not a destructive new state of the original payment. No decline/retry-exhaustion test or retry-count policy is added. Money uses Decimal-compatible strings; assertion values remain 100.00 USD except T03's conflicting input of 125.00 USD.

### Logical operations, not framework APIs

- Submit: validate nonempty key and payload; first accepted logical payment creates its original record and exactly one scripted gateway charge. For T01, return success and create one ledger posting. For scripted timeout, retain UNRESOLVED despite the gateway charge having committed.
- Retry identical payload: recognize original key/payload, log status verification before any possible new charge, and return original success if verified. If unresolved, retain original unresolved outcome; no new charge or ledger posting.
- Conflicting amount: reject same-key/different-amount request before touching original payment, charge or ledger; preserve original success.
- Verify original status: consume the test's deterministic status script; recorded outcome is UNRESOLVED or SUCCESS. Verified success reconciles original payment and creates its first ledger posting if absent; it never creates another posting for an already-recorded original success.
- Deliver delayed successful callback: consume C-001 once; reconcile P-001 and create the original posting if none. No wall-clock timer; the test explicitly delivers the queued event.
- Duplicate callback: with defect off, ignore repeat C-001's ledger effect and retain original state/data. With defect on, repeated callback appends one additional ledger entry carrying the same callback/payment identity; it never creates another gateway charge or changes amount/currency/state.

These choices implement only the five scripted cases. Generalized callback payload conflicts, concurrency, refunds, settlement, key lifetimes and real retry policies are outside scope. The conceptual idempotency catalog can require a missing-key observation under BR01 but no sixth executable test is introduced.

### Fixture and observation rules

Each executable test gets a fresh empty key index, payments, gateway list, ledger, callback-seen set and event counter. No shared module singleton state between tests. The fixed run defect setting applies to all five fixtures; only T04 delivers a repeated callback. Persist observations in fixture teardown even when an assertion fails, and persist critical before/after observations before the failure point. Observation recording does not become another test.

Events prove order by sequence, not timestamps. Status verification must precede any charge-creation branch of retry; tests assert no second charge. Evidence includes original charge IDs, retry identity/payload, status outcomes, callback deliveries, ledger entries, amounts/currency and final state. Do not infer an external double charge from duplicate ledger evidence.

## 3. Exactly five predefined executable tests

The source Given/When/Then and defect expectations below are preserved verbatim. Design elaborations specify deterministic sequencing/evidence only. S01–S05 are generated review scenarios with embedded display-only Gherkin, never executable inputs. Initial omission of S04 leaves T04 intact. No generated Python, Gherkin bindings, expanding parameterization, xfail or additional payment tests. Use plain local evidence files; no mock REST server or persistence service.

### T01 — Initial success (S01; BR01)

- **Given:** P-001 with K-001 and 100.00 USD; gateway returns success.

- **When:** Submit the payment.

- **Then:** Exactly one gateway charge and one successful ledger posting; payment is successful for 100.00 USD.

- **Defect-on expectation:** PASS.

**Fixed proposed pytest node ID:** tests/payment/test_payment.py::test_t01_initial_success

**Sequence and evidence:** Reset fixture; submit P-001/K-001/100.00 USD with SUCCESS script; capture the one charge and posting. Assert original payload/key, SUCCESS, charge count 1, ledger count 1, amount/currency. Save submit/charge/posting events and final records. Expected defect-on PASS because no duplicate delivery occurs.

### T02 — Timeout and identical retry (S02; BR01, BR02, BR04, BR05)

- **Given:** The original request with K-001 commits one gateway charge but its response times out; status verification subsequently returns original success.

- **When:** Retry with K-001 and the identical payload.

- **Then:** Timeout was recorded as unresolved; status verification precedes any possible new charge; retry returns the original successful payment; exactly one gateway charge and one ledger posting exist.

- **Defect-on expectation:** PASS.

**Fixed proposed pytest node ID:** tests/payment/test_payment.py::test_t02_timeout_identical_retry

**Sequence and evidence:** Reset; submit with charge-committed/response-TIMEOUT script. Capture UNRESOLVED and gateway count 1. Retry identical key/payload; record verification SUCCESS; reconcile original payment/posting and return original result. Assert timeout not recorded as failure, identical original identity/key/payload, verification before any new-charge branch, charge count 1 and ledger count 1. Save original timeout, verification, retry outcome and IDs. No duplicate callback delivery; expected PASS.

### T03 — Same key with conflicting amount (S03; BR01, BR03)

- **Given:** K-001 already identifies a successful 100.00 USD payment.

- **When:** Submit K-001 with amount 125.00 USD.

- **Then:** Reject the conflicting request; the original amount, successful payment, single gateway charge, and single ledger posting remain unchanged.

- **Defect-on expectation:** PASS.

**Fixed proposed pytest node ID:** tests/payment/test_payment.py::test_t03_conflicting_amount

**Sequence and evidence:** Reset; establish original successful 100.00 USD payment under K-001. Snapshot charge/ledger/payment; submit K-001/125.00 USD. Assert explicit conflict rejection and byte-equivalent original financial fields/charge/ledger (ignore audit-log additions). Save rejected input, reason, original/final snapshots. Expected PASS.

### T04 — Duplicate successful callback (S04; BR06)

- **Given:** Callback C-001 for P-001 has established success with one ledger posting and one gateway charge.

- **When:** Deliver C-001 again for P-001.

- **Then:** Exactly one ledger posting remains; payment amount, currency, successful state, and gateway charge count remain unchanged.

- **Defect-on expectation:** FAIL on ledger count, expected 1 and actual 2. Retain callback/payment identity and both ledger entries as evidence.

**Fixed proposed pytest node ID:** tests/payment/test_payment.py::test_t04_duplicate_callback

**Sequence and evidence:** Reset; establish P-001 success via first C-001, with one charge/posting. Deliver the same C-001 again once. Record both deliveries and ledger before checking count. Assert payment SUCCESS, amount/currency unchanged and gateway count 1; then assert ledger count 1. Defect mode gives actual 2, so T04 is FAIL, with two distinct entry IDs for the same payment/callback. Preserve the real pytest assertion failure; do not mark it expected-pass/xfail. With defect off, the same fixed assertions pass.

### T05 — Delayed success after timeout (S05; BR01, BR02, BR04, BR05)

- **Given:** Original request with K-001 commits one charge but times out; first status verification remains unresolved. Original success callback is queued.

- **When:** Request an identical retry, deliver the scripted delayed success once, then resolve the retry against the original payment.

- **Then:** Timeout and unresolved verification do not mark failure or create another charge; delayed success reconciles P-001; the retry resolves the same payment; exactly one gateway charge and one ledger posting exist.

- **Defect-on expectation:** PASS.

**Fixed proposed pytest node ID:** tests/payment/test_payment.py::test_t05_delayed_success

**Sequence and evidence:** Reset; submit with committed charge and TIMEOUT response; record UNRESOLVED. Identical retry verifies UNRESOLVED and creates no second charge. Explicitly deliver queued C-001 success once; reconcile original P-001 and its first posting; resolve the pending identical retry against that original result. Assert no false failure, original ID/key/payload, verification ordering, one charge, one ledger entry, successful original result and original 100.00 USD. Save timeout/verification/delayed-callback/retry events. Single callback delivery does not trigger defect; expected PASS.

## 4. Allowlisted pytest execution design

The controller launches the configured virtual-environment Python executable with a fixed argument vector, working directory pinned to the project root, and shell=False. Only trusted fixed manifest node IDs are included; no raw command input from UI/model. A separate human Execute click creates the execution request only after valid human Approve. It binds the current bundle, trusted manifest, all five IDs, defect setting and validated run directory. Under the single controller lock save execution_started=true before launch; never reset it in that run.

Future invocation shape, shown for design only and not executed here:

```text
<venv-python> -m pytest
  tests/payment/test_payment.py::test_t01_initial_success
  tests/payment/test_payment.py::test_t02_timeout_identical_retry
  tests/payment/test_payment.py::test_t03_conflicting_amount
  tests/payment/test_payment.py::test_t04_duplicate_callback
  tests/payment/test_payment.py::test_t05_delayed_success
  --junitxml=<validated-run-directory>/execution/junit.xml
  -q -p no:cacheprovider
```

This is a logical argument list, not a pasted multiline Windows shell command. Only interpreter and validated artifact location are configuration values; test nodes/options are allowlisted constants. Only the controller's separately clicked human Execute path may reserve this operation; a model tool call/request cannot invoke it. Do not use -x/maxfail because all five must run after T04 fails. No plugins are required beyond pytest; fixture code writes structured observations in execution/T01_evidence.json through execution/T05_evidence.json under the validated run directory. Child receives defect setting and evidence path through controller-generated environment values, without AWS credentials; disable plugin auto-loading to avoid unintended local plugins. The collection must contain exactly the five node IDs.

Pytest documents selection by node ID, module invocation and JUnit output. [Invocation](https://docs.pytest.org/en/stable/how-to/usage.html), [report output](https://docs.pytest.org/en/stable/how-to/output.html). Validate selected node mapping and result count rather than relying on console text alone. A local observation recorder supplements JUnit with structured expected/actual values; it cannot invent unexecuted assertions.

## 5. Results and failure semantics

Capture stdout, stderr, return code, observed duration, JUnit, fixture events/ledger and per-test records in results.json. A normal defect-on batch should return pytest failure status with four PASS and one FAIL, not a runner exception. No hardcoded expected-status substitution: actual evidence determines the report. Unexpected failures remain visible.

Setup/teardown/collection errors, missing/extra/skipped tests, interrupted child or missing required evidence make batch verification INCOMPLETE and workflow STOPPED. Preserve actual results obtained and mark remaining tests NOT_RUN. A consumed execution_started flag is not reset after timeout/launch failure; no automatic rerun. Kill the child on the proposed 60-second batch deadline and retain partial records. Python-only in-process fixtures create no grandchildren or live services.

T04 analysis maps expected ledger 1/actual 2, repeated C-001/P-001 and preserved single gateway charge to BR06. Missing supporting records are unavailable, never invented. Failure analysis does not repair service/test code. For defect-off review, use a fresh separately approved run; this is an optional reviewer baseline, not an extra test or automatic rerun.

## 6. Generation catalog and coverage content

Independent catalog mappings: S01/T01→BR01; S02/T02→BR01/BR02/BR04/BR05; S03/T03→BR01/BR03; S04/T04→BR06; S05/T05→BR01/BR02/BR04/BR05. The catalog is authored from the functional authority, not generated from the current scenario set.

For each generated scenario, validate nonempty correct setup, action sequence, exact structured oracle values, Gherkin agreement, matching test/rule IDs and synthetic payload. The observation vocabulary is payment_state, amount, currency, gateway_charge_count, ledger_success_count, same_payment_id, same_idempotency_key, same_payload, timeout_not_failed, status_verification_before_retry_charge, conflict_rejected, original_financial_records_unchanged and delayed_success_resolves_original. Numeric count operators use EQ; ordered actions/events use PRECEDES. Expected booleans are true for the listed behavioral invariants. S01/S02/S04/S05 expect SUCCESS at completion; T05 also observes UNRESOLVED before delayed success.

Structured action_codes supplement readable actions and Gherkin; they are semantic validation facts, never executable commands. Fixed required sequences: S01 SUBMIT_SUCCESS; S02 SUBMIT_COMMITTED_TIMEOUT → RETRY_IDENTICAL_VERIFY_SUCCESS; S03 SUBMIT_SUCCESS → SUBMIT_CONFLICTING_AMOUNT; S04 DELIVER_DUPLICATE_SUCCESS_CALLBACK after its stated original-success precondition; S05 SUBMIT_COMMITTED_TIMEOUT → RETRY_IDENTICAL_VERIFY_UNRESOLVED → DELIVER_SUCCESS_CALLBACK → RESOLVE_ORIGINAL_RETRY. No arbitrary action-code vocabulary or dynamic dispatch to pytest. The validator compares ordered codes and oracle values to this catalog, then validates generated Gherkin against a fixed Given/When/Then grammar built from those facts. Readable descriptions must not contradict these facts; unsupported/conflicting text is invalid or escalated, never counted based on its ID. Templates constrain grammar but the AI still generates scenario content; the controller does not fabricate missing S04.

S01 includes valid key and original charge/posting. S02 includes unchanged payload, uncertain timeout and verified original result with one charge/posting. S03 includes rejected conflicting amount and original record preservation. S04 includes repeated callback with correct one-posting oracle, original amount/currency/state and unchanged charge. S05 includes unresolved first verification, explicit single delayed callback and original-result resolution. A scenario with the right ID but a two-posting expected oracle is INVALID, not covered. Free prose and keywords alone cannot certify semantics; structured asserted facts/action ordering must agree with the fixed catalog, and inconsistent prose/Gherkin is rejected or escalated for review. Never treat a model self-assessed confidence as content validation. **Revision 2:** prose/Gherkin consistency is checked by the Decider grounding guardrail in [04](04_Decider_Decision_Design.md); its verdict can only flag a scenario for human review or (after human confirmation) mark it INVALID — it never makes content VALID.

The same catalog facts are used by the Test SME to label the grounding benchmark set (correct and mutated scenarios) described in [10](10_Hackathon_Submission_Plan.md) §3; the benchmark is not a payment test.

Required Scenario Coverage is valid required scenario count / 5: initial 4/5 (80%), final 5/5 (100%). Rule traceability has six rows but is not that metric's denominator. No code/exhaustive/defect-free coverage claim.


### BDD Scenario Mapping and Reusable Test Primitives

This is a lightweight three-layer organization of the existing five tests, not executable BDD or additional functionality.

**Layer 1 — Business scenarios:** AI-generated Given/When/Then scenarios S01–S05 map to BR01–BR06 and are reviewed/approved before execution. Initial S04 omission and the sole permitted remediation remain unchanged; T04 always stays in the executable manifest. Gherkin remains display-only review content and is never parsed into executable steps or Python.

**Layer 2 — Reusable test primitives:** Use a small set of predefined deterministic Python helpers or fixture methods around existing in-process mock operations. Names below are illustrative; reuse an equivalent service method rather than introducing another abstraction layer.

| Illustrative primitive | Responsibility |
|---|---|
| `create_payment()` | Submit the existing initial synthetic payment. |
| `simulate_timeout()` | Select the existing unresolved-response script; do not mark timeout as failure or assume no charge committed. |
| `retry_payment()` | Retry the original payment with the same idempotency key and payload, retaining status-verification behavior. |
| `send_callback()` | Deliver the existing successful callback or re-deliver the same callback to demonstrate duplication. |
| `get_payment_status()` | Read the current mock payment state without changing it. |
| `count_gateway_charges()` | Read the actual simulated gateway charge count. |
| `count_ledger_entries()` | Read the successful ledger posting count, distinct from gateway charges. |

Helpers use synthetic data and the test's isolated fixture, expose or preserve the reporting evidence already required above, and never call a real gateway. AI cannot generate or modify them dynamically. Test their behavior through the existing T01–T05; no additional executable helper tests are required. Keep business assertions in those predefined tests, not hidden inside helpers. Existing fixtures continue recording actual events/ledger observations before failure and during teardown.

**Layer 3 — Executable automation:** Exactly T01–T05 compose applicable shared operations: T01 initial payment/status/counts; T02 timeout/identical retry/status/counts; T03 initial payment/conflicting-amount request and unchanged original-record checks; T04 duplicate callback/status/counts; T05 timeout/identical retry/delayed callback/status/counts. Use the existing conflicting-request operation for T03 rather than forcing it through a same-payload helper. Preserve every existing input, assertion, fixture reset and outcome. Defect-on T04 must still fail a real ledger-count assertion; the other four expected passes are unchanged. Human Approve, separate Execute and one batch per run remain mandatory.

**Traceability example:** `BR06 → S04 → send_callback() / count_ledger_entries() → T04 → Failing assertion → Ledger evidence`.

Concise S04 review Gherkin:

```gherkin
Scenario: S04 duplicate successful callback preserves one ledger posting
  Given callback C-001 has made P-001 successful for 100.00 USD
    And one gateway charge and one successful ledger posting exist
  When callback C-001 is delivered again for P-001
  Then exactly one successful ledger posting remains
    And the gateway charge count remains one
    And the successful payment amount and currency remain unchanged
```

Illustrative body fragment of the existing T04 only; helper signatures are examples, not a new test or actual mock-service API. This documentation snippet is not executed. Its fixture already established the original C-001 success and the one-charge/one-posting precondition:

```python
# Existing isolated T04 fixture; duplicate-ledger defect is enabled.
send_callback(mock, payment_id="P-001", callback_id="C-001")
assert get_payment_status(mock, "P-001") == "SUCCESS"
assert count_gateway_charges(mock, "P-001") == 1
# Existing original-amount/currency assertions remain unchanged in T04.
ledger_count = count_ledger_entries(mock, "P-001")
# The fixture retains both deliveries and ledger entries as evidence.
assert ledger_count == 1  # Real failure: actual value is 2 with the defect.
```

**Explicit exclusions:** No pytest-bdd, Cucumber or new BDD framework; no dynamic Gherkin execution, AI-generated Python, step-definition registry or generic test-composition engine. No extra scenario, executable test, dependency, infrastructure or service. This organization fits the existing test-development milestone and October 20, 2026 deadline.

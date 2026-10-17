# Data Contracts and Workflow State

**Project:** AI Test Engineer Agent  
**Use case:** Payment Retry & Duplicate Charge Prevention  
**Revision 2 (Oct 13, 2026):** adds scenario grounding records, Decider confidence/latency/host fields, human escalation of low-confidence decisions, and the `AWAITING_DECISION_CONFIRMATION` stage label (still twelve states).

## 1. Simple local records

Use plain Python-compatible records and JSON files with explicit type/value checks; a lightweight schema library is optional. No database, artifact envelopes, manifest registry, general event sourcing, distributed workflow engine or separate component services. Every payload below requires run_id (UUID matching current run). References are relative filenames beneath that run directory; validate existence and path containment. Common run ID is not repeated in every field table.

Use UTC RFC3339 for recorded times; nonnegative milliseconds for durations; integer counts; decimal strings such as 100.00 for money. Source/version and fixed test definitions are immutable. Retain initial/final scenario and coverage files; do not overwrite initial evidence. Example run IDs/hashes/data below are illustrative except the actual authoritative source fingerprint. Examples are not proof of execution.

All fields in each table are required unless explicitly Optional/Conditional. Nested fields apply when array members exist; allowed empty arrays do not require fictional records. Unknown control/schema fields are rejected; only bounded evidence snapshots and stage outcomes use checkpoint-specific keys. Reject incorrect types before tool dispatch.

## 2. Requirement snapshot

| Field | Type | Required | Validation |
|---|---|---|---|
| requirement_id | `string` | Required | Fixed PAYMENT_RETRY_REQUIREMENT identifier |
| source_version | `string` | Required | Immutable sha256:source_hash |
| source_hash | `string` | Required | Actual unchanged authoritative file SHA-256 |
| text | `string` | Required | Exact bundled customer story and BR01–BR06; never edited during run |

Snapshot once in requirement.json. Extracted rule semantics are separate; human clarification never changes text/source/version.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "requirement_id": "PAYMENT_RETRY_REQUIREMENT",
  "source_version": "sha256:f846df6edb7e23ea70396a4a4f9e6f6b81a12101bb8c9eaccfa64db5cfe734da",
  "source_hash": "f846df6edb7e23ea70396a4a4f9e6f6b81a12101bb8c9eaccfa64db5cfe734da",
  "text": "As a customer, I want to retry a payment when the payment gateway times out, without being charged twice.\nBR01: Every payment request must include a unique idempotency key for its logical payment; retries reuse that key.\nBR02: Reusing the same key with the same payload resolves the original payment and must not create another charge.\nBR03: Reusing the same key with a different payment amount is rejected; the original charge and ledger remain unchanged.\nBR04: A timeout must not automatically mark the payment as failed. Retain its unresolved outcome until status verification or a callback establishes the result.\nBR05: Verify the original payment status before initiating another charge. If already successful, return the original result. If unresolved, do not create a new charge; retain the unresolved outcome until the scripted original success is available.\nBR06: Re-delivery of the same successful callback, identified by callback ID and payment ID, must not create another financial ledger posting."
}
```

## 3. Interpretation and clarification

| Field | Type | Required | Validation |
|---|---|---|---|
| rules | `array<object>` | Required | Exactly six unique rule_ids BR01–BR06 |
| rules[].rule_id | `string` | Required | BR01–BR06 |
| rules[].source_excerpt | `string` | Required | Exact substring of the bundled rule |
| rules[].meaning | `string` | Required | Source-consistent interpretation |
| rules[].expected_observations | `array<string>` | Required | Nonempty source-consistent observable outcomes for the interpreted rule |
| ambiguities | `array<object>` | Required | May be empty |
| ambiguities[].id | `string` | Required | Unique within run |
| ambiguities[].rule_ids | `array<string>` | Required | Affected existing BR IDs |
| ambiguities[].question | `string` | Required | Necessary meaning question |
| ambiguities[].critical | `boolean` | Required | Blocks correctness/coverage/execution if unresolved |
| ambiguities[].status | `string` | Required | OPEN, RESOLVED or CONFLICT |
| ambiguities[].human_response | `string` | Optional | Separate source-consistent explanation |

critical OPEN/CONFLICT blocks approval/execution. A reply is separately audited; this record can reflect resolved interpretation, not a new requirement. No arbitrary editing/regeneration loop.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "rules": [
    {
      "rule_id": "BR01",
      "source_excerpt": "Every payment request must include a unique idempotency key for its logical payment; retries reuse that key.",
      "meaning": "Every payment request must include a unique idempotency key for its logical payment; retries reuse that key.",
      "expected_observations": [
        "Each logical payment carries its key and retries reuse it."
      ]
    },
    {
      "rule_id": "BR02",
      "source_excerpt": "Reusing the same key with the same payload resolves the original payment and must not create another charge.",
      "meaning": "Reusing the same key with the same payload resolves the original payment and must not create another charge.",
      "expected_observations": [
        "Identical key/payload resolves the original result without another charge."
      ]
    },
    {
      "rule_id": "BR03",
      "source_excerpt": "Reusing the same key with a different payment amount is rejected; the original charge and ledger remain unchanged.",
      "meaning": "Reusing the same key with a different payment amount is rejected; the original charge and ledger remain unchanged.",
      "expected_observations": [
        "Conflicting amount is rejected and original records stay unchanged."
      ]
    },
    {
      "rule_id": "BR04",
      "source_excerpt": "A timeout must not automatically mark the payment as failed. Retain its unresolved outcome until status verification or a callback establishes the result.",
      "meaning": "A timeout must not automatically mark the payment as failed. Retain its unresolved outcome until status verification or a callback establishes the result.",
      "expected_observations": [
        "Timeout remains unresolved, not automatically failed."
      ]
    },
    {
      "rule_id": "BR05",
      "source_excerpt": "Verify the original payment status before initiating another charge. If already successful, return the original result. If unresolved, do not create a new charge; retain the unresolved outcome until the scripted original success is available.",
      "meaning": "Verify the original payment status before initiating another charge. If already successful, return the original result. If unresolved, do not create a new charge; retain the unresolved outcome until the scripted original success is available.",
      "expected_observations": [
        "Original status verification precedes any possible new charge."
      ]
    },
    {
      "rule_id": "BR06",
      "source_excerpt": "Re-delivery of the same successful callback, identified by callback ID and payment ID, must not create another financial ledger posting.",
      "meaning": "Re-delivery of the same successful callback, identified by callback ID and payment ID, must not create another financial ledger posting.",
      "expected_observations": [
        "Repeated successful callback leaves exactly one ledger posting."
      ]
    }
  ],
  "ambiguities": []
}
```

## 4. Scenario set with embedded Gherkin

| Field | Type | Required | Validation |
|---|---|---|---|
| version | `integer` | Required | 1 initial or 2 remediated; keep both files |
| scenarios | `array<object>` | Required | Initial four S01/S02/S03/S05; final five after S04 |
| scenarios[].scenario_id | `string` | Required | Unique required S01–S05 |
| scenarios[].title | `string` | Required | Readable nonempty title |
| scenarios[].rule_ids | `array<string>` | Required | Fixed mapping in 07 |
| scenarios[].test_id | `string` | Required | Corresponding T01–T05 |
| scenarios[].preconditions | `array<string>` | Required | Correct nonempty setup |
| scenarios[].actions | `array<string>` | Required | Readable ordered actions, never commands |
| scenarios[].action_codes | `array<string>` | Required | Fixed semantic catalog tokens in 07, never executable dispatch |
| scenarios[].expected_assertions | `array<object>` | Required | Correct fixed oracles |
| scenarios[].expected_assertions[].observation | `string` | Required | Allowlisted fact in 07 |
| scenarios[].expected_assertions[].operator | `string` | Required | EQ or PRECEDES |
| scenarios[].expected_assertions[].expected | `string / integer / boolean` | Required | Correct observable value |
| scenarios[].gherkin | `string` | Required | Display-only Given/When/Then agrees with semantic facts |

The example shows one member of a final set; full runtime acceptance requires all five. Initial/final sets are scenarios_initial.json/scenarios_final.json. Gherkin is embedded, eliminating separate files/circular references; it remains review-only. Other four scenarios must be unchanged by S04 addition.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "version": 2,
  "scenarios": [
    {
      "scenario_id": "S04",
      "title": "Duplicate callback keeps one ledger posting",
      "rule_ids": [
        "BR06"
      ],
      "test_id": "T04",
      "preconditions": [
        "P-001 is SUCCESS for 100.00 USD with one charge/posting from C-001"
      ],
      "actions": [
        "Deliver C-001 again for P-001"
      ],
      "action_codes": [
        "DELIVER_DUPLICATE_SUCCESS_CALLBACK"
      ],
      "expected_assertions": [
        {
          "observation": "ledger_success_count",
          "operator": "EQ",
          "expected": 1
        },
        {
          "observation": "gateway_charge_count",
          "operator": "EQ",
          "expected": 1
        },
        {
          "observation": "payment_state",
          "operator": "EQ",
          "expected": "SUCCESS"
        },
        {
          "observation": "amount",
          "operator": "EQ",
          "expected": "100.00"
        },
        {
          "observation": "currency",
          "operator": "EQ",
          "expected": "USD"
        }
      ],
      "gherkin": "Given P-001 is successful for 100.00 USD with one ledger posting and one charge\nWhen C-001 is delivered again for P-001\nThen there is still one successful ledger posting\nAnd one gateway charge\nAnd the original successful payment amount and currency remain unchanged"
    }
  ]
}
```

## 5. Coverage and gap

| Field | Type | Required | Validation |
|---|---|---|---|
| metric_name | `string` | Required | Required Scenario Coverage |
| scenario_set_version | `integer` | Required | 1 or 2 matches checked set |
| covered | `integer` | Required | Number of VALID required content rows, 0–5 |
| required | `integer` | Required | Exactly 5 |
| percentage | `number` | Required | covered / 5 × 100 |
| rows | `array<object>` | Required | Exactly five required rows including missing S04 |
| rows[].scenario_id | `string` | Required | Unique S01–S05 |
| rows[].test_id | `string` | Required | Fixed mapping; T04 always present |
| rows[].rule_ids | `array<string>` | Required | Fixed BR mappings |
| rows[].status | `string` | Required | VALID, MISSING or INVALID |
| rows[].findings | `array<string>` | Required | Actual missing/invalid content; empty when valid |
| rule_traceability | `object` | Required | Six BR keys mapped to valid supporting S IDs, not coverage denominator |

Content validator checks setup/actions/oracles/Gherkin/mapping, not just IDs. Required Scenario Coverage is computed by this deterministic validator only (F04).

**Grounding (Decider guardrail).** For each generated scenario, Decider answers one `noul` question: does the readable prose and Gherkin assert the same outcome as the structured oracle and linked rule? Save grounding_initial.json and grounding_final.json:

| Field | Type | Required | Validation |
|---|---|---|---|
| scenario_set_version | `integer` | Required | 1 or 2 |
| items | `array<object>` | Required | One per present scenario |
| items[].scenario_id | `string` | Required | S01–S05 |
| items[].p_yes | `number` | Required | Native Decider value 0–1 |
| items[].confidence | `number` | Required | max(p_yes, 1−p_yes) |
| items[].verdict | `string` | Required | GROUNDED, NOT_GROUNDED or NEEDS_REVIEW (confidence below threshold) |
| items[].human_resolution | `string / null` | Required | CONFIRMED_GROUNDED, CONFIRMED_NOT_GROUNDED or null |
| items[].native_response_ref | `string` | Required | Saved raw response |

A NOT_GROUNDED or unresolved NEEDS_REVIEW scenario blocks Approve until the human resolves it; a human-confirmed NOT_GROUNDED scenario is recorded as INVALID in the coverage row. Grounding never upgrades an INVALID or MISSING row to VALID.

Save coverage_initial.json and coverage_final.json; rows include gap explanation, so no separate gap schema/file. Computed 4/5 (80%)→5/5 (100%); no code/exhaustive/defect-free claim.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "metric_name": "Required Scenario Coverage",
  "scenario_set_version": 1,
  "covered": 4,
  "required": 5,
  "percentage": 80,
  "rows": [
    {
      "scenario_id": "S01",
      "test_id": "T01",
      "rule_ids": [
        "BR01"
      ],
      "status": "VALID",
      "findings": []
    },
    {
      "scenario_id": "S02",
      "test_id": "T02",
      "rule_ids": [
        "BR01",
        "BR02",
        "BR04",
        "BR05"
      ],
      "status": "VALID",
      "findings": []
    },
    {
      "scenario_id": "S03",
      "test_id": "T03",
      "rule_ids": [
        "BR01",
        "BR03"
      ],
      "status": "VALID",
      "findings": []
    },
    {
      "scenario_id": "S04",
      "test_id": "T04",
      "rule_ids": [
        "BR06"
      ],
      "status": "MISSING",
      "findings": [
        "Duplicate callback scenario content missing"
      ]
    },
    {
      "scenario_id": "S05",
      "test_id": "T05",
      "rule_ids": [
        "BR01",
        "BR02",
        "BR04",
        "BR05"
      ],
      "status": "VALID",
      "findings": []
    }
  ],
  "rule_traceability": {
    "BR01": [
      "S01",
      "S02",
      "S03",
      "S05"
    ],
    "BR02": [
      "S02",
      "S05"
    ],
    "BR03": [
      "S03"
    ],
    "BR04": [
      "S02",
      "S05"
    ],
    "BR05": [
      "S02",
      "S05"
    ],
    "BR06": []
  }
}
```

## 6. Decider request

| Field | Type | Required | Validation |
|---|---|---|---|
| checkpoint | `string` | Required | STRATEGY, COVERAGE, AMBIGUITY, FAILURE, COMPLETION or GROUNDING (sub-check of COVERAGE) |
| workflow_state | `string` | Required | Current high-level state |
| state_text | `string` | Required | Short controller-built summary of saved facts; no preselected answer |
| questions | `object` | Required | Named questions; each has type (`choice`, `noul`, `score`), instructions and, for `choice`/`score`, options from the fixed checkpoint vocabulary in 04 |
| evidence_references | `array<string>` | Required | Existing same-run relative filenames |
| threshold | `number` | Required | From config/decider.json; never model-supplied |

Application request; the adapter converts it to the native `/v1/systemone` body recorded during the spike (O01). Several questions on one state go in one call.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "checkpoint": "COVERAGE",
  "workflow_state": "PREPARING",
  "state_text": "Validator: 4/5 valid; S04 (BR06 duplicate callback) missing; remediation unused. Grounding: S01 0.97, S02 0.94, S03 0.96, S05 0.91.",
  "questions": {
    "coverage_action": {
      "type": "choice",
      "instructions": "Accept the scenario set or remediate the gap?",
      "options": ["ACCEPT_COVERAGE", "REMEDIATE_GAP"]
    }
  },
  "evidence_references": ["coverage_initial.json", "grounding_initial.json"],
  "threshold": 0.9
}
```

## 7. Decision record

| Field | Type | Required | Validation |
|---|---|---|---|
| checkpoint | `string` | Required | Matches actual request |
| workflow_state | `string` | Required | Current state at validation |
| selected_action | `string / null` | Required | Applied action; null only on parse-error rejection |
| decider_suggestion | `string / null` | Required | Decider's top answer (may differ from selected_action after human escalation) |
| confidence | `number / null` | Required | Native confidence (choice/score) or max(p, 1−p) (noul); null only on error |
| option_probabilities | `object` | Optional | Native per-option probabilities when returned |
| threshold | `number` | Required | Value used for routing |
| route | `string` | Required | AUTO or ESCALATED |
| decision_source | `string` | Required | DECIDER (AUTO) or HUMAN_ESCALATION |
| latency_ms | `integer` | Required | Native `latency_ms` if returned, else measured round trip |
| host_label | `string` | Required | e.g. local-cpu, ec2-g4dn-t4; from config |
| model_revision | `string` | Required | Pinned Decider model ID/revision |
| rationale | `string` | Required | Controller explanation from saved evidence |
| rationale_origin | `string` | Required | CONTROLLER_EVIDENCE |
| evidence_references | `array<string>` | Required | Existing referenced files |
| decision_status | `string` | Required | VALIDATED or REJECTED, assigned by controller |
| native_response_ref | `string` | Required | Saved raw response/error file |
| rejection_reason | `string` | Conditional | Required for REJECTED |
| per_question | `array<object>` | Required | One entry per question this checkpoint asked -- `{question, answer, suggestion, confidence, probabilities, route}` -- so a multi-question checkpoint's full audit survives, not just its primary verdict |

Confidence routes a decision; it never overrides a controller guard. A human pick after escalation is guarded the same way (e.g. ACCEPT_COVERAGE at 4/5 is still rejected). Unknown/malformed/stale selections stop the run; no hardcoded fallback.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "checkpoint": "COVERAGE",
  "workflow_state": "PREPARING",
  "selected_action": "REMEDIATE_GAP",
  "decider_suggestion": "REMEDIATE_GAP",
  "confidence": 0.96,
  "option_probabilities": {"ACCEPT_COVERAGE": 0.04, "REMEDIATE_GAP": 0.96},
  "threshold": 0.9,
  "route": "AUTO",
  "decision_source": "DECIDER",
  "latency_ms": 212,
  "host_label": "ec2-g4dn-t4",
  "model_revision": "<pinned-after-spike>",
  "rationale": "S04 is missing and remediation unused",
  "rationale_origin": "CONTROLLER_EVIDENCE",
  "evidence_references": ["coverage_initial.json"],
  "decision_status": "VALIDATED",
  "native_response_ref": "decider_calls/coverage_initial_response.json"
}
```

Illustrative values, not observed output.

## 8. Human approval

| Field | Type | Required | Validation |
|---|---|---|---|
| approval_id | `string` | Required | UUID assigned to human action |
| actor | `string` | Required | LOCAL_HUMAN, not authenticated identity claim |
| decision | `string` | Required | APPROVE or REJECT |
| bundle_hash | `string` | Required | SHA-256 of exact current review bundle |
| scenario_set_version | `integer` | Required | Current accepted final artifact version; 2 for the intended remediated run |
| test_ids | `array<string>` | Required | Exactly ordered T01–T05 reviewed by the human |
| test_manifest_hash | `string` | Required | SHA-256 of the fixed reviewed manifest/code fingerprints |
| recorded_at_utc | `string` | Required | UTC RFC3339 |
| valid | `boolean` | Required | Controller-owned; false on mismatch/rejection |

Approve only records authorization and enters APPROVED; it never launches pytest. Reject enters REJECTED and prevents execution. Bundle change invalidates authorization; source/test change stops.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "approval_id": "b2c423c4-361c-4ebc-8ee3-34c123456789",
  "actor": "LOCAL_HUMAN",
  "decision": "APPROVE",
  "bundle_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "recorded_at_utc": "2026-10-12T14:00:00Z",
  "valid": true,
  "scenario_set_version": 2,
  "test_ids": [
    "T01",
    "T02",
    "T03",
    "T04",
    "T05"
  ],
  "test_manifest_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

## 9. Execution request

| Field | Type | Required | Validation |
|---|---|---|---|
| execution_id | `string` | Required | Unique ID for the sole reserved batch |
| approval_ref | `string` | Required | Existing valid approval.json |
| bundle_hash | `string` | Required | Matches current approved bundle |
| test_ids | `array<string>` | Required | Exactly T01–T05 ordered |
| defect_enabled | `boolean` | Required | Matches approved run setting |
| evidence_directory | `string` | Required | Resolved fixed run subdirectory; no model/UI path |

Created only by separate human Execute after guards. Under lock set/save execution_started before child launch. Only trusted fixed pytest argument vector; no command_key platform or arbitrary arguments.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "execution_id": "c3d534d5-472d-4fcc-9ff4-45d123456789",
  "approval_ref": "approval.json",
  "bundle_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "test_ids": [
    "T01",
    "T02",
    "T03",
    "T04",
    "T05"
  ],
  "defect_enabled": true,
  "evidence_directory": "execution"
}
```

## 10. Batch and individual test results

| Field | Type | Required | Validation |
|---|---|---|---|
| execution_id | `string` | Required | Existing reserved ID |
| return_code | `integer / null` | Required | Actual child code; null if launch never completed |
| duration_ms | `integer` | Required | Observed nonnegative time |
| tests | `array<object>` | Required | Five fixed IDs; unperformed marked NOT_RUN |
| tests[].test_id | `string` | Required | Unique T01–T05 |
| tests[].scenario_id | `string` | Required | Matching S ID |
| tests[].rule_ids | `array<string>` | Required | Fixed mapping |
| tests[].status | `string` | Required | PASS, FAIL, ERROR, SKIPPED or NOT_RUN |
| tests[].duration_ms | `integer` | Required | Actual nonnegative per-test elapsed time; 0 for NOT_RUN |
| tests[].assertions | `array<object>` | Required | Only actual evaluated observations |
| tests[].assertions[].observation | `string` | Required | Named actual fact |
| tests[].assertions[].expected | `string / integer / boolean` | Required | Correct fixed oracle |
| tests[].assertions[].actual | `string / integer / boolean / null` | Required | Actual value; null if unavailable |
| tests[].assertions[].passed | `boolean` | Required | Real assertion outcome |
| tests[].evidence_references | `array<string>` | Required | Existing fixture/JUnit/stdout records |
| error | `string` | Optional | Runner/incomplete error when present |

Example assertions are a subset of recorded checks, not the complete runtime suite. Keep full actual checks from 07. T04 is real FAIL, no xfail/skip. Missing/extra/skipped/error results make verification INCOMPLETE; preserve partial evidence and no second batch.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "execution_id": "c3d534d5-472d-4fcc-9ff4-45d123456789",
  "return_code": 1,
  "duration_ms": 180,
  "tests": [
    {
      "test_id": "T01",
      "scenario_id": "S01",
      "rule_ids": [
        "BR01"
      ],
      "status": "PASS",
      "assertions": [
        {
          "observation": "ledger_success_count",
          "expected": 1,
          "actual": 1,
          "passed": true
        }
      ],
      "evidence_references": [
        "execution/T01_evidence.json",
        "execution/junit.xml"
      ],
      "duration_ms": 12
    },
    {
      "test_id": "T02",
      "scenario_id": "S02",
      "rule_ids": [
        "BR01",
        "BR02",
        "BR04",
        "BR05"
      ],
      "status": "PASS",
      "assertions": [
        {
          "observation": "ledger_success_count",
          "expected": 1,
          "actual": 1,
          "passed": true
        }
      ],
      "evidence_references": [
        "execution/T02_evidence.json",
        "execution/junit.xml"
      ],
      "duration_ms": 12
    },
    {
      "test_id": "T03",
      "scenario_id": "S03",
      "rule_ids": [
        "BR01",
        "BR03"
      ],
      "status": "PASS",
      "assertions": [
        {
          "observation": "ledger_success_count",
          "expected": 1,
          "actual": 1,
          "passed": true
        }
      ],
      "evidence_references": [
        "execution/T03_evidence.json",
        "execution/junit.xml"
      ],
      "duration_ms": 12
    },
    {
      "test_id": "T04",
      "scenario_id": "S04",
      "rule_ids": [
        "BR06"
      ],
      "status": "FAIL",
      "assertions": [
        {
          "observation": "ledger_success_count",
          "expected": 1,
          "actual": 2,
          "passed": false
        }
      ],
      "evidence_references": [
        "execution/T04_evidence.json",
        "execution/junit.xml"
      ],
      "duration_ms": 12
    },
    {
      "test_id": "T05",
      "scenario_id": "S05",
      "rule_ids": [
        "BR01",
        "BR02",
        "BR04",
        "BR05"
      ],
      "status": "PASS",
      "assertions": [
        {
          "observation": "ledger_success_count",
          "expected": 1,
          "actual": 1,
          "passed": true
        }
      ],
      "evidence_references": [
        "execution/T05_evidence.json",
        "execution/junit.xml"
      ],
      "duration_ms": 12
    }
  ]
}
```

## 11. Failure analysis

| Field | Type | Required | Validation |
|---|---|---|---|
| test_id | `string` | Required | Actual failed test, or batch for infrastructure |
| rule_ids | `array<string>` | Required | Supported BR violation only; empty for infrastructure |
| classification | `string` | Required | BUSINESS_RULE_VIOLATION, INFRASTRUCTURE_ERROR or INSUFFICIENT_EVIDENCE; selected at the FAILURE checkpoint by Decider (or the human after escalation); controller rejects BUSINESS_RULE_VIOLATION when no failed assertion exists |
| classification_decision_ref | `string` | Required | Decision record for the class choice |
| observations | `array<string>` | Required | Cited facts |
| expected_behavior | `string` | Required | Correct requirement oracle |
| actual_behavior | `string` | Required | Observed or explicitly unavailable |
| hypothesized_cause | `string / null` | Required | Labeled hypothesis, not invented source location |
| evidence_references | `array<string>` | Required | Existing assertion/fixture records |
| missing_evidence | `array<string>` | Required | May be empty |
| handling_action | `string / null` | Required | Actual failure selection; null before checkpoint |

Bedrock-assisted analysis uses captured facts; controller rejects invented outcomes/refs. Distinguish duplicate ledger from actual double charge. No repairs or reruns. Initial factual analysis precedes Decider failure selection; optional investigation deepens only once.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "test_id": "T04",
  "rule_ids": [
    "BR06"
  ],
  "classification": "BUSINESS_RULE_VIOLATION",
  "classification_decision_ref": "decisions.json#failure_class",
  "observations": [
    "C-001 delivered twice for P-001; two ledger entries; one gateway charge"
  ],
  "expected_behavior": "One ledger posting",
  "actual_behavior": "Two ledger postings",
  "hypothesized_cause": "Hypothesis: repeated callback was not suppressed at ledger write",
  "evidence_references": [
    "results.json",
    "execution/T04_evidence.json"
  ],
  "missing_evidence": [],
  "handling_action": "REPORT_FAILURE"
}
```

## 12. Central run state

| Field | Type | Required | Validation |
|---|---|---|---|
| workflow_state | `string` | Required | One of twelve states below |
| stage | `string` | Required | Readable current activity, not additional state machine; includes AWAITING_DECISION_CONFIRMATION while a low-confidence decision waits for the human |
| pending_decision_ref | `string / null` | Required | Decision awaiting human confirmation, else null |
| busy | `boolean` | Required | One local operation owns work |
| source_hash | `string` | Required | Immutable authority fingerprint |
| scenario_set_version | `integer` | Required | 0 before generation, 1 initial, 2 final |
| remediation_used | `boolean` | Required | Starts false; saved true before sole remediation |
| execution_started | `boolean` | Required | Starts false; saved true before sole child launch; never reset |
| investigation_used | `boolean` | Required | Starts false; true before one deeper read |
| approval_ref | `string / null` | Required | approval.json or null |
| bundle_hash | `string / null` | Required | Exact review bundle or null before review |
| defect_enabled | `boolean` | Required | Fixed reviewed config |
| resume_stage | `string / null` | Required | Pending stage only while clarification waits |
| stopping_reason | `string / null` | Required | Nonempty for terminal incomplete outcome |

Single controller owns run_state.json/current run, lock and worker. No distributed state, consumed-command registry or replay engine.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "workflow_state": "APPROVED",
  "stage": "AWAITING_EXECUTE",
  "busy": false,
  "source_hash": "f846df6edb7e23ea70396a4a4f9e6f6b81a12101bb8c9eaccfa64db5cfe734da",
  "scenario_set_version": 2,
  "remediation_used": true,
  "execution_started": false,
  "investigation_used": false,
  "approval_ref": "approval.json",
  "bundle_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "defect_enabled": true,
  "resume_stage": null,
  "pending_decision_ref": null,
  "stopping_reason": null
}
```

## 13. Basic audit event

| Field | Type | Required | Validation |
|---|---|---|---|
| sequence | `integer` | Required | Increasing per run |
| timestamp_utc | `string` | Required | UTC RFC3339 |
| event_type | `string` | Required | Run, stage, decision, approval, execution, error or report event |
| workflow_state | `string` | Required | Actual current state |
| message | `string` | Required | Sanitized observed fact |
| evidence_references | `array<string>` | Required | Existing same-run files, may be empty initially |

A simple chronological JSON list/basic log, not event sourcing. decisions.json holds full decision records. Never log secrets, hidden reasoning or fabricated model output.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "sequence": 12,
  "timestamp_utc": "2026-10-12T14:00:00Z",
  "event_type": "APPROVAL_RECORDED",
  "workflow_state": "APPROVED",
  "message": "Human approval recorded; tests not launched",
  "evidence_references": [
    "approval.json"
  ]
}
```

## 14. Report metadata

| Field | Type | Required | Validation |
|---|---|---|---|
| agent_workflow_status | `string` | Required | COMPLETED, REJECTED, BLOCKED or STOPPED |
| payment_verification_status | `string` | Required | PASSED, FAILED, INCOMPLETE or NOT_RUN |
| report_filename | `string` | Required | report.md only when saved |
| evidence_references | `array<string>` | Required | Real source/scenario/coverage/approval/results/analysis/decisions files |
| stopping_reason | `string / null` | Required | Required for incomplete terminal outcome |
| stage_outcomes | `object` | Required | Actual stages COMPLETED, NOT_RUN or ERROR |

COMPLETED agent with FAILED payment is expected defect-on. No results means NOT_RUN; incomplete runner/evidence means INCOMPLETE. Never infer passing tests from scenario coverage.

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "agent_workflow_status": "COMPLETED",
  "payment_verification_status": "FAILED",
  "report_filename": "report.md",
  "evidence_references": [
    "coverage_initial.json",
    "coverage_final.json",
    "approval.json",
    "results.json",
    "analysis.json",
    "decisions.json"
  ],
  "stopping_reason": null,
  "stage_outcomes": {
    "preparation": "COMPLETED",
    "approval": "COMPLETED",
    "execution": "COMPLETED",
    "analysis": "COMPLETED",
    "report": "COMPLETED"
  }
}
```

## 15. Twelve high-level states

One centralized manager owns these states; stage labels show substeps, not new state machines. Before a run the UI is idle with no run_id. PREPARING begins only after Start Analysis. Terminal states never resume; new work needs a new run/approval.

| State | Outgoing transition / guard |
|---|---|
| PREPARING | Intake, interpretation, real strategy choice, initial generation/check in sequence; REMEDIATING on valid gap choice; AWAITING_CLARIFICATION on necessary meaning question; no approval until accepted full content |
| AWAITING_CLARIFICATION | Return to pending preparation/remediation stage after source-consistent resolution; remain waiting without response; BLOCKED on unresolved critical conflict |
| REMEDIATING | Set remediation_used before S04 generation; preserve other four; final check/coverage choice; AWAITING_APPROVAL only at valid 5/5; persistent gap → BLOCKED |
| AWAITING_APPROVAL | Approve → APPROVED only; Reject → REJECTED; no response stays waiting |
| APPROVED | Separate Execute, matching bundle/current human approval and execution_started=false → EXECUTING; Reject before execution → REJECTED; stale reviewed bundle → AWAITING_APPROVAL; illegal source/test change → STOPPED |
| EXECUTING | Sole fixed pytest child completes with five full results → ANALYZING_RESULTS; launch/timeout/error/incomplete → STOPPED |
| ANALYZING_RESULTS | Bedrock-assisted factual findings and genuine failure choice if failures; bounded one read-only investigation when selected; then REPORTING. No failures: explicit note then REPORTING |
| REPORTING | Real completion selection and evidence guard; saved report → COMPLETED; invalid/missing prerequisite or write failure → STOPPED |
| COMPLETED | Terminal completed workflow; payment outcome remains its actual PASSED/FAILED |
| REJECTED | Terminal human rejection; no execution/rework |
| BLOCKED | Terminal unresolved critical ambiguity/coverage; no further remediation/execution |
| STOPPED | Terminal model/tool/error/invalid choice/interruption; no automatic replay |

A low-confidence Decider answer does not change the state: the stage becomes AWAITING_DECISION_CONFIRMATION, busy=false, and the run waits for the human's pick (no model polling). Any nonterminal technical/invalid-action error may enter STOPPED. A pre-execution critical ambiguity may enter AWAITING_CLARIFICATION with the pending stage stored; this does not permit another scenario generation after the sole remediation. Incomplete terminal reports are controller-rendered from available evidence without pretending FINALIZE_RESULTS succeeded.

## 16. Approval and single-use safeguards

The review bundle contains immutable source/interpretation, final scenario descriptions/Gherkin, accepted coverage, ordered fixed T01–T05 manifest, test/support/mock code fingerprints, and defect_enabled. Compute one SHA-256 over canonical UTF-8 JSON (sorted object keys, stable ordered arrays). Store bundle_hash in run state and approval. Before Execute, recompute/check content and expected code fingerprints. Local hashes protect accidental/stale changes, not a hostile user controlling the process.

Use one local lock around state/flag changes. Approve writes approval.json and APPROVED only; no batch is reserved. Execute requires a separate human action: check state/bundle/approval/no critical ambiguity/accepted coverage, set execution_started=true and state EXECUTING, save run_state.json, then launch exactly one fixed child. Only the human Execute handler can create this controller-owned request/reservation; the worker consumes that one reserved operation. Do not expose execution as an unrestricted model tool or accept a model-created execution request, even if state is APPROVED. If the save/launch fails, do not launch/retry again; flag stays consumed. Repeated click/rerender sees that flag and cannot dispatch again. Likewise save remediation_used/investigation_used before their one allowed action. No counters/command keys or retry framework are necessary.

One process-owned controller/current run plus a worker persists across browser reload; tabs/read-only refresh do not call models or execute tools. Session state alone is insufficient because reload resets its WebSocket connection. [Streamlit session state](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.session_state). On process restart, retain artifacts/read-only display but mark unfinished work STOPPED, never resume/replay. Current waiting/terminal facts may be inspected; a new active workflow needs a new run and approval.

## 17. Files and evidence

Use runs/<run_id>/ with requirement.json, interpretation.json, scenarios_initial.json, scenarios_final.json, grounding_initial.json, grounding_final.json, coverage_initial.json, coverage_final.json, decisions.json, decider_calls/, approval.json, execution_request.json, execution/, results.json, analysis.json, events.json, run_state.json, report.md and report_metadata.json. The fixed test manifest is trusted configuration, not a dynamic artifact registry. Files absent because stages were not run stay absent; metadata/controls show NOT RUN/unavailable.

Native call files include sanitized real request/response or exception, host label, model revision, native confidence/probabilities and latency. Basic sequential log events are enough; no log replay platform. Write each JSON with a simple temporary-file replace under the lock; stop on write failure before further side effects. No cross-file transaction/recovery protocol is required.

Fixture evidence includes ordered submit/retry/status/callback events, original payment/key/payload, amount/currency, gateway charge IDs/count, ledger entry IDs/count, final state and actual assertion observations. T04's two entries have distinct entry IDs but the same P-001/C-001 identity. Preserve expected 1/actual 2 before the assertion fails. All five results and stdout/stderr/JUnit are retained; missing/extra/skipped/error results are incomplete, not passing.

Downloads read existing report/JSON bytes only; never run inference, generate missing evidence or replay tests. The small embedded Gherkin and coverage findings remove separate reference cycles/gap objects while retaining F03/F04 review output and rule traceability.

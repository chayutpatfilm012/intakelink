# AI assistance log

Material AI assistance during the build, and what the author did with it.
Keep entries short and factual. Add one row per slice.

| Slice | What AI drafted | What I accepted, changed, or rejected | How I checked it |
| --- | --- | --- | --- |
| 1 | Record model, status and hold rules, audit log, access control, FR-05/FR-08/NFR-01 tests, README and repo files | Accepted the record model, status and hold rules, audit log, access control, and initial project structure. | Reviewed the implementation and ran test_review_actions.py (6 passed) and test_audit_and_access.py (5 passed). |
| 2 | `intakelink.intake`: intake checks (type, channel, size), write-once document store (exclusive create, read-only file, SHA-256 check on read), page-numbered text prep (`.txt` with form-feed pages; `.pdf` via optional pypdf); `tests/test_intake.py` (18 tests); `pdf` extra in `pyproject.toml`; README updates | Accepted intake validation, write-once document storage, SHA-256 integrity checking, and document text preparation. | Reviewed intake.py and ran test_intake.py (18 passed, 1 skipped because pypdf was not installed). |
| 3 | `intakelink.matching` (Evidence Matcher with the given weights, 1.0 cap, 0.6 threshold, 0.15 ambiguity margin, exact-after-normalization comparison); `intakelink.synthetic` (20 fictional matters, 64 fictional documents, hand-written expected outcomes, generator CLI); `data/synthetic/` generated files; `insurers` and `providers` fields added to `Matter`; `tests/test_matching.py` and `tests/test_synthetic_set.py` (114 tests); README updates. AI interpretation choices to review: AMBIGUOUS_MATCH only checked when the top score is ≥ 0.6; "name only" also covers a name with a non-matching DOB; LOW_CONFIDENCE is non-blocking but attaches no proposal; tier 1 = another client's name, tier 2 = another client's claim number or name + DOB | Defined the matcher weights, 0.60 threshold, 0.15 ambiguity margin, and 64-document dataset. Accepted the matching implementation. | Reviewed the matching rules and synthetic dataset; ran test_matching.py (41 passed) and test_synthetic_set.py (73 passed). |
| 4 | `intakelink.guard` (tier-1/tier-2 cross-matter detection, exact/probable/new-version duplicate checks, `run_checks` combining matcher + guard into `write_flags`); `intakelink.fields` (shared field names); `documentDate` added to the synthetic ground truth and data regenerated; `tests/test_guard_detection.py` (21 tests, detection only, no FR-11 review behavior). AI choices to review: duplicate flags are non-blocking; text-similarity threshold 0.8 (the set's probable copies score 0.84 and 0.87); flag details hide the other matter; FACT_CONFLICT and MATCHING_UNAVAILABLE not yet produced | Accepted the initial cross-matter and duplicate detection implementation, but later requested changes to duplicate handling and FACT_CONFLICT. | Reviewed the guard rules and ran test_guard_detection.py (30 passed, including later Slice 7 changes). |
| 5 | `intakelink.extraction`: offline `RuleBasedExtractor` (labels, letterhead, ID patterns, keyword document-type classifier), optional `LLMExtractor` (Claude `claude-opus-5-5`, JSON-schema structured output, server-side refusal fallback, re-validation of every fact against its page), `extract_into`; `llm` extra; `tests/test_extraction.py` (75 tests, LLM tested with a fake client only); README updates. Note: the rule-based rules were written against the synthetic format, so 100% on the set is expected | Accepted the rule-based extractor and optional LLM extraction implementation. | Reviewed extraction logic and ran test_extraction.py (75 passed, using a fake client for LLM tests). |
| 6 | `intakelink.filing` (mock case-management system: approved and unheld records only, once each; in-memory notifier); `intakelink.service` (pipeline wiring, re-check after correction, file on approval, hold and failure notices, matter-scoped queue and logged view refusals); `intakelink.demo` (64-document run plus scripted review session); `app/workbench.py` (Streamlit Workbench with simulated sign-in); synthetic `USERS`; `tests/test_service_and_filing.py` and `tests/test_workbench.py` (17 tests); README updates. AI choices to review: intake-queue records are visible to all staff; who gets notified; the demo script's reasons are invented | Accepted filing, service integration, demo, and Workbench implementation. Later requested restricted intake queue access. | Reviewed the workflow and ran test_service_and_filing.py (16 passed) and test_workbench.py (5 passed). |
| 7 | Report alignment (§7.5, FR-07): `EXACT_DUPLICATE` made blocking in `guard.py`; new blocking `FACT_CONFLICT` (target client's name with a different DOB on the same page, skipped when the DOB belongs to another client named on the document); synthetic A04 and L01 replaced by F01/F02 (claim-identified documents with a mistyped DOB), `fact_conflict` added to the manifest, data regenerated; demo steps for both rules; Workbench gets a fact-correction field; tests updated and added (+13). `review.py` unchanged | Changed EXACT_DUPLICATE to blocking and requested FACT_CONFLICT detection based on report §7.5. | Reviewed both guard rules against report §7.5 and ran test_guard_detection.py (30 passed). |
| 8 | (a) Intake-queue access: `access.can_handle_intake` (senior paralegals only) and two small changes in `review.py` (refuse non-intake staff on a record with no matter; let intake staff route to any matter with a reason; let intake staff decide an escalated intake record); `service.can_view`, Workbench routing list and intake notifications, demo step 5, `tests/test_intake_queue_access.py`, two existing tests moved to a senior paralegal. (b) Held-out set: `intakelink.heldout` (40 documents, 6 new templates) and `data/heldout/`. (c) `intakelink.evaluate` (extraction P/R, matching outcome, guard detection, threshold × margin sweep) and generated `docs/EVALUATION.md`; `match()` takes optional `threshold` / `margin` (defaults unchanged). `tests/test_heldout_and_evaluation.py`. The extractor was not changed after seeing held-out results | Selected restricted intake queue access, held-out evaluation, and threshold analysis. Kept the extractor unchanged after evaluation. | Reviewed access control and evaluation results; ran test_intake_queue_access.py (12 passed) and test_heldout_and_evaluation.py (8 passed). |

## Written by the author without AI

- FR-06 and FR-11 tests (see `docs/AUTHOR_TESTS.md`): I wrote FR-06 cases 1–4 in `tests/test_fr06_status_integrity.py` and FR-11 cases 6–9 in `tests/test_fr11_cross_matter_guard.py`. FR-11 case 5 was an AI-written worked example, and case 9b was also drafted by AI, so I do not count them as my own work.  One important thing I learned was that a test must reach the specific rule it intends to verify. In case 9, the active hold blocked the action before the override rule could be tested. I also learned that checking `all()` on an empty decision list can give a misleading result, so the test should verify the number of decisions first.


### AI help on the author tests (disclosed)

- FR-11 case 5 in `tests/test_fr11_cross_matter_guard.py` is a worked example
  drafted by AI (marked in the file).
- FR-06 case 1: the author wrote the test. On request, AI made two mechanical
  fixes. It replaced the `make_record` / `tier2_flag` fixture parameters (they
  are plain functions in `conftest.py`) with the file's existing imports, and it
  moved the precondition `assert` above the Act step. AI also ran a mutation
  check: it briefly made `write_flags` set review status, saw the test fail, and
  reverted the change. The author then wrote the audit-log assertion (`any(...)`
  over `hold_set` entries by `"system"`). On request, AI changed it to count those
  entries before and after the Act step and expect exactly one more. It also
  removed the finished TODO comment.
- FR-11 cases 6-9: written by the author. AI reviewed each draft and ran
  mutation checks in memory. The check on case 9 showed that the active hold
  refuses the override first, so the override rule itself was never tested.
  On request, AI added an assertion on the refusal message and a comment to
  case 9. AI also **wrote case 9b**
  (`test_override_rule_refuses_tier2_even_without_a_hold`, labeled "DRAFTED BY
  AI" in the file). It tests the override rule directly in a state the system
  does not produce. Case 9b is AI-written and is not part of the author's tests.
- FR-06 cases 2-4: written by the author. AI reviewed each draft and ran the
  tests and a mutation check in memory. On request, AI also fixed whitespace
  only in the file: it removed a stray `# ` line and set blank lines between
  tests.

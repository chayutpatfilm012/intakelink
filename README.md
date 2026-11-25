# IntakeLink

**Evidence-checked document matching for personal-injury matters.**
CEN 4010 Final Project prototype by Chayutpat Chuanchom.

IntakeLink helps a paralegal file each incoming document to the correct existing
client matter. The AI only reads the document; deterministic rules propose a
matter with the evidence behind every score, and a Cross-Matter Guard holds any
document carrying another client's identifiers for the responsible attorney.
A person approves every filing, and every action is logged.

**Try it online:** [https://intakelink-jbry7bvd7dauyhxn4admwd.streamlit.app/](https://intakelink-jbry7bvd7dauyhxn4admwd.streamlit.app/) (Workbench on Streamlit Community Cloud;
pick a simulated user in the sidebar, then load the synthetic test set)

> **Status:** prototype. All 6 slices are built (see [Progress](#progress)),
> including the author-written FR-06 and FR-11 tests.
> All data in this repository is synthetic. Do not load real client documents.

## Why

In a law office, filing a document to the wrong client's matter exposes that
client's health and financial data to staff on another case. IntakeLink treats
a misfile as a confidentiality risk, not just a filing error. The full problem
analysis, requirements, and design are in the Solution Design Report
([docs/IntakeLink_Solution_Design_Report.pdf](docs/IntakeLink_Solution_Design_Report.pdf)).

## Architecture

| ID | Component | Module | Status |
| --- | --- | --- | --- |
| C1–C3 | Intake, Document Store, Text Preparation | `intakelink.intake` | done |
| C4 | AI Extraction Service | `intakelink.extraction` | done |
| C5 | Matter Index | `intakelink.access.MatterIndex`; synthetic data in `intakelink.synthetic` | done |
| C6 | Evidence Matcher | `intakelink.matching` | done |
| C7 | Exception & Cross-Matter Guard, duplicate checks | `intakelink.guard` | done |
| C8 | Candidate Record Store, status and hold rules | `intakelink.models`, `intakelink.review` | done |
| C9 | Human Review Workbench | `app/workbench.py` (Streamlit) | done |
| C10 | Filing & Notification | `intakelink.filing` (mock case-management system, in-memory outbox) | done |
| — | Pipeline wiring | `intakelink.service` | done |
| X1 | Matter Access Control | `intakelink.access` | done |
| X2 | Audit Log | `intakelink.audit` | done |

Key rules already enforced in code (`src/intakelink/review.py`):

- Review status changes only through an attributed reviewer action (FR-06).
- The system may set hold status; it never sets review status.
- A tier-2 cross-matter hold can be released only by the matter's responsible attorney (FR-11).
- An ambiguous match blocks approval until the reviewer picks one matter with a reason (FR-07).
- Any correction re-runs checks before approval.
- Every refused request is written to the audit log (FR-08, NFR-01).
- A record with no matter yet (low confidence or unreadable) may hold any
  client's data. Only intake staff (senior paralegals) can open or act on it
  (NFR-01). They can route it to any matter with a written reason, and after
  that only the matter's team has access.

Evidence Matcher rules (`src/intakelink/matching.py`; initial values, not validated):

| Evidence | Weight |
| --- | --- |
| Matter number | 1.0 |
| Claim number | 0.6 |
| Full name + date of birth | 0.5 |
| Name only | 0.2 |
| Incident date | 0.2 |
| Insurer or provider | 0.1 |

Each rule counts once per matter, and the score is capped at 1.0. A top score
below 0.6 is no confident match: the record gets `LOW_CONFIDENCE` and no
proposal, so a person must choose the matter. If the top two scores are within
0.15, the record gets `AMBIGUOUS_MATCH`, which blocks approval. Values must match
exactly after normalization (case, punctuation, date format, "Last, First");
there is no fuzzy matching. The AI never sees the Matter Index.

Cross-Matter Guard and duplicate checks (`src/intakelink/guard.py`):

- **Tier 2** (`CROSS_MATTER_RISK`, held for the responsible attorney): another
  client's matter number, claim number, or name plus date of birth.
- **Tier 1** (`CROSS_MATTER_NAME`, blocks until overridden with a reason): another
  client's name alone.
- The same client's other matter is not treated as foreign. A name shared by two
  different clients is not foreign unless the other client's DOB also appears.
- Flag details give the page and kind of identifier, never the other matter or
  client.
- **Fact conflict** (`FACT_CONFLICT`, blocks approval; FR-07): a name on the
  document matches the target matter's client (the selected matter, else the top
  proposal), but a date of birth on the same page differs from that matter's
  record. It is not raised when that name and DOB belong to another client's
  matter, because tier 2 or the matcher covers that case. The detail gives only
  the field and the page, for example "date of birth on page 1 differs from the
  matter record", never the record's value. The reviewer can correct the fact or
  the matter, and the checks re-run automatically. The reviewer can also reject
  or escalate with a reason.
- **Exact duplicate** (`EXACT_DUPLICATE`, blocks approval; report §7.5): the same
  bytes (SHA-256) as an earlier document. The reviewer rejects it with a reason,
  or escalates it. Approval stays refused while the flag is open.
- **Possible duplicate or new version** (warnings that don't block):
  `POSSIBLE_NEW_VERSION` (same type and identifiers, similar text, new revision
  marker) and `POSSIBLE_DUPLICATE` (same type, identifiers and document date,
  similar text).

## Prerequisites

- Python 3.10 or newer
- Git

## Setup

```bash
git clone https://github.com/chayutpatfilm012/intakelink.git
cd intakelink
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,ui]"
```

The core package has no third-party dependencies. `ui` adds Streamlit for the
Workbench; `dev` adds pytest; `pdf` adds pypdf for reading PDF text layers.
Without `pdf`, plain-text documents (pages separated by a form feed) still work,
and a PDF fails text preparation visibly instead of being skipped. `llm` adds the
Anthropic SDK for the optional LLM extraction mode.

### Extraction modes

The default extractor is rule-based and works offline. It reads labeled lines,
letterhead organizations, and claim and matter number patterns, and it is tuned
to the synthetic documents. The optional LLM mode (`LLMExtractor`) calls Claude
(`claude-opus-5-5`, structured JSON output, server-side refusal fallback). It
needs `pip install -e ".[llm]"` and an `ANTHROPIC_API_KEY`. **The LLM mode sends
document text to an outside service, so use it with synthetic data only.**

In both modes, the extractor gets only the page-numbered text. It never sees the
Matter Index and never picks a matter. LLM output is checked again: a fact with
an unknown field, a page that doesn't exist, or a value that isn't on the stated
page is dropped and counted. A failed or refused request moves the record to
`EXTRACTION_FAILED`.

## Run tests

```bash
pytest -q
```

| Test file | Proves |
| --- | --- |
| `tests/test_review_actions.py` | FR-05 four reviewer actions, FR-07 ambiguous match, re-check after correction, stale-version refusal |
| `tests/test_audit_and_access.py` | FR-08 attributed audit trail, NFR-09 append-only log, NFR-01 off-matter access denied and logged |
| `tests/test_intake.py` | Write-once store (no overwrite, read-only, integrity check), page numbering, corrupted files kept and marked `EXTRACTION_FAILED`, refused files logged |
| `tests/test_matching.py` | Each matcher weight, cap, once-per-rule, normalization (no fuzzy match), 0.6 threshold, 0.15 ambiguity margin, low-confidence and ambiguous records blocked until a person chooses |
| `tests/test_guard_detection.py` | Guard detection only: tier-1/tier-2 identifiers, same-client and shared-name cases, flag details reveal nothing about the other matter, FACT_CONFLICT raised and not raised (and cleared by correcting the DOB), exact (blocking) and probable/new-version (warning) duplicates, and the right flags on every document of the synthetic set in arrival order |
| `tests/test_extraction.py` | Extractor imports nothing that reads the Matter Index; rule-based output equals ground truth on all 62 readable documents; end-to-end on raw files (intake → extraction → checks) gives the expected outcome for every document; LLM output validation and failure handling with a fake client (no network) |
| `tests/test_service_and_filing.py` | Only approved records are filed, and only once; pipeline matches ground truth; hold notice goes only to the responsible attorney and reveals nothing about the other matter; approval files and notifies the matter team; correction re-runs checks; exact duplicate blocks approval (also after escalation) until rejected; fact conflict cleared by correcting the fact; off-matter view refused and logged; demo runs end to end |
| `tests/test_workbench.py` | Workbench smoke tests (Streamlit AppTest, skipped if Streamlit is absent): queue limited to the user's matters, refused action shown and changes nothing, fact correction clears a fact conflict, approval through the page files the record |
| `tests/test_synthetic_set.py` | Test-set composition (20 matters, 64 documents), committed files match the generator, every ground-truth fact is on its stated page, matcher outcome for each of the 62 readable documents |
| `tests/test_intake_queue_access.py` | Intake queue limited to senior paralegals (view and act refused and logged for others), routing needs a reason, a routed record moves to the matter team, unreadable and escalated intake records can still be decided |
| `tests/test_heldout_and_evaluation.py` | Held-out set composition and drift, ground truth on its page, `docs/EVALUATION.md` up to date, no wrong confident proposal at system settings on either set |
| `tests/test_fr06_*.py`, `tests/test_fr11_*.py` | Written by the author; see `docs/AUTHOR_TESTS.md` |

## Run the prototype

**End-to-end demo (terminal):**

```bash
python -m intakelink.demo                     # uses a new temp folder
python -m intakelink.demo --workdir data/local/demo
```

The demo receives all 64 synthetic documents, checks every outcome against
`manifest.json`, and then runs a scripted review session through the real rules:

- a clean approval
- a tier-1 override
- a tier-2 hold released by the responsible attorney
- an ambiguous match resolved
- a low-confidence document assigned to a matter
- an exact duplicate refused for approval, then rejected
- a conflicting date of birth corrected, then approved
- an off-matter view refused

It prints the filings, notifications and audit-log totals. It exits with code 1
if anything differs from what is expected.

**Workbench (browser):**

```bash
streamlit run app/workbench.py
```

A hosted copy runs at [https://intakelink-jbry7bvd7dauyhxn4admwd.streamlit.app/](https://intakelink-jbry7bvd7dauyhxn4admwd.streamlit.app/). The app may sleep when
unused; the first visit wakes it, which takes about a minute. Each visitor gets
their own in-memory copy of the data.

Pick a synthetic user in the sidebar (simulated sign-in), load the test set, and
work through the queue. Each user sees only records on their own matters. Senior
paralegals (intake staff) also see the intake queue of records that have no
matter yet, and can route them to a matter. Every action goes through
`review_action()`, and an approved record is filed to the mock case-management
system. State lives in memory for the browser session.

## Data

All matters, people, and documents are fictional and generated for testing.
The test set (Solution Design Report §8.3) is in `data/synthetic/`:

- `matters.json`: 20 fictional matters, including two unrelated clients with the
  same name and one client with two matters
- `documents/doc-01.txt` … `doc-64.*`: 30 standard, 10 ambiguous or conflicting
  (5 expected `AMBIGUOUS_MATCH`, 3 `LOW_CONFIDENCE`, 2 confident matches with a
  conflicting DOB that raise `FACT_CONFLICT`), 10 cross-matter (5 tier-1, 5 tier-2), 6 duplicate pairs (2 exact, 2 probable,
  2 newer-version), and 2 corrupted files
- `manifest.json`: ground truth for each file: category, correct matter, expected
  matcher outcome, cross-matter details, duplicate pair, and every identifier with
  its page number

Expected outcomes are written by hand in `src/intakelink/synthetic.py`, not computed
by the matcher. To regenerate the files: `python -m intakelink.synthetic data/synthetic`.
A test fails if the committed files and the generator drift apart.

`data/heldout/` is a separate **held-out set** of 40 fictional documents in six
formats the extractor was not written for:
- labels it doesn't know, and values on the line under the label
- prose, and several fields on one line
- 2-digit years, dotted dates, ordinals
- OCR character swaps

It has 30 standard documents, 6 cross-matter (3 tier-1, 3 tier-2) and 4 with a
conflicting DOB. Its ground truth is the printed text and its page. It has no
expected system outcomes, only the truth. The extractor must not be tuned to it.
To regenerate it: `python -m intakelink.heldout data/heldout`.

Never commit real client data, API keys, or credentials; `.env` is git-ignored.

## Evaluation

```bash
python -m intakelink.evaluate --out docs/EVALUATION.md
```

This measures the offline pipeline on both sets: extraction precision and recall
per field, the matching outcome, guard detection, and a sensitivity grid over the
threshold and the ambiguity margin. The full results are in
[`docs/EVALUATION.md`](docs/EVALUATION.md). A test fails if that file is out of
date. Headline results at the system settings (threshold 0.6, margin 0.15):

| | dev (62 readable) | held-out (40) |
| --- | --- | --- |
| Correct proposal | 54 | 25 |
| **Wrong proposal** | **0** | **0** |
| Sent to a person | 8 | 15 |
| Cross-matter document stopped (flagged or sent to a person) | 10/10 | 5/6 |
| Conflicting-DOB document stopped | 2/2 | 1/4 |

On the held-out set the rule-based extractor misses most names, dates of birth
and incident dates (recall 28%, 12% and 0%). The system fails safe: those
documents go to a person instead of to a wrong matter. The misses still mean
that one held-out cross-matter document and three conflicting-DOB documents got
a clean proposal, and only the reviewer could catch them. Because weights are
multiples of 0.1, a margin of 0.15 behaves exactly like 0.1. In the sweep, a
lower threshold or a zero margin produces wrong proposals on the dev set.

## Progress

Development follows Solution Design Report §8.1: deterministic controls first, AI last.

- [x] 1. Record model, status rules, audit log, access control
- [x] 2. Intake, document store, text preparation
- [x] 3. Matter index data and Evidence Matcher
- [x] 4. Cross-Matter Guard and duplicate checks
- [x] 5. AI Extraction Service (rule-based offline mode; optional LLM mode)
- [x] 6. Filing, notification, Workbench, end-to-end demo

## Known limitations

- Synthetic data only; accuracy on real office documents is untested.
- Matching weights and thresholds are initial design values (report §7.5).
- The synthetic documents are plain text with labeled fields, so they are easier
  to read than real faxes and scans. The rule-based extractor scores 100% on them
  because its rules were written for this format. On the held-out formats its
  recall drops sharply (see [Evaluation](#evaluation)). Both sets are generated
  by the author's code, so neither one stands in for real office documents.
- The LLM mode is tested only with a fake client. It has not been run against
  the live API in this repository.
- No real case-management system; filing goes to an in-memory mock, and
  notifications go to an in-memory outbox (no email).
- Sign-in on the Workbench is simulated (choose a synthetic user). There is no
  authentication.
- Records live in memory; only the original files and the audit log are written
  to disk. Restarting the Workbench starts over.
- The guard does not yet produce `MATCHING_UNAVAILABLE`. `FACT_CONFLICT` checks
  only the date of birth on the same page as the client's name.

## AI assistance

AI tools helped draft code and documentation. What was accepted, changed, or
rejected is recorded in [`docs/AI_ASSISTANCE.md`](docs/AI_ASSISTANCE.md).

## License

MIT; see [LICENSE](LICENSE).

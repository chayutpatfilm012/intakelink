# Contributing

IntakeLink is a course prototype, but changes follow the same rules as a real project.

## Ground rules

- **Synthetic data only.** Never add real client documents, names, or identifiers.
- **No secrets.** API keys go in a local `.env` file, which is git-ignored.
- **Human authority stays in the rules.** A change must not let the system set
  review status, or let anyone but the responsible attorney release a tier-2 hold.

## Workflow

1. Create a branch: `git checkout -b feature/short-name`.
2. Make the change and add or update tests.
3. Run `pytest -q`; all tests must pass.
4. Commit with a message that says what and why, e.g.
   `Add tier-2 hold release rule (FR-11)`.
5. Open a pull request that names the requirement IDs it affects.

## AI-assisted changes

Record material AI assistance in `docs/AI_ASSISTANCE.md`: what was generated,
what you changed or rejected, and how you checked it.

# Implement: PR workflow

`/implement` is expected to end with an open PR, not just local commits — this repo's PRs all target `develop` (see recent merged PRs for the pattern).

`script/check` **must** pass locally before any push that opens or updates a PR — docs-only diffs included. Once it is green and `/code-review` is clean:

1. If working directly on `develop`, create a feature branch first (short, descriptive name; doesn't need to match the issue title verbatim).
2. Commit, then push the branch with `-u`.
3. Open a PR against `develop` with `gh pr create`, referencing the issue (`Closes #<n>`) in the body.

No separate confirmation needed for the commit/push/PR steps themselves — that's covered by the exception in `AGENTS.md`. Still surface the PR URL back to the user afterward.

## PR description style

Write PR bodies in [ASD-STE100 Simplified Technical English](https://en.wikipedia.org/wiki/Simplified_Technical_English): short sentences, one idea each, active voice, simple present tense, no idioms, no synonyms for the same thing.

Say **why** the change is right. Do not restate what the diff does — the diff shows that. In particular:

- Lead with the user-visible problem or the wrong behaviour the change fixes.
- Explain why the chosen direction is correct, including the alternatives it rules out.
- Link to the issue with `Closes #<n>` when one exists.
- Mention a trade-off, follow-up, or known limitation only if the reviewer needs it to merge.

Do **not** include:

- A file-by-file or function-by-function walk-through of the implementation.
- CI-automated steps the repo already runs (`script/check`, `script/lint`, `ruff`, `ty`, the pre-commit hooks, the test suite count). The reviewer sees these pass on the PR.
- Tool banners or generator footers.

One or two short paragraphs is usually enough. Add headings only when the body has two or more distinct sections that need them.

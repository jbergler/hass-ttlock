# Implement: PR workflow

`/implement` is expected to end with an open PR, not just local commits — this repo's PRs all target `develop` (see recent merged PRs for the pattern).

Once `script/check` passes and `/code-review` is clean:

1. If working directly on `develop`, create a feature branch first (short, descriptive name; doesn't need to match the issue title verbatim).
2. Commit, then push the branch with `-u`.
3. Open a PR against `develop` with `gh pr create`, referencing the issue (`Closes #<n>`) in the body.

No separate confirmation needed for the commit/push/PR steps themselves — that's covered by the exception in `CLAUDE.md`. Still surface the PR URL back to the user afterward.

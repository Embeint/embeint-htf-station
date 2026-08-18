# Contributing

## Commit messages

Every commit must use this format:

```text
<type>(<optional-scope>): <short description>

<longer description explaining what changed and why.>

Signed-off-by: <author name> <author@example.com>
```

Allowed types are `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`,
`refactor`, `revert`, `style`, and `test`.

Use an imperative, concise subject line. The body is required and should give
reviewers the context not apparent from the diff. Add the trailer with
`git commit -s`; GitHub pull-request checks enforce this rule for every commit.

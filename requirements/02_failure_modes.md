# Find Failures

Examples of the kinds of failures the benchmark should expose. Each distinct failure must be **specific, reproducible, fair, gradable, and tied to a real work consequence.**

## Failure examples from the brief

- Full refund issued when only partial refund is allowed.
- Tool called before required eligibility check.
- Wrong customer, order, ticket, or employee updated.
- Policy ignored in favor of a friendly answer.
- Missing information not requested.
- Approval skipped.
- Final message says success while database state is wrong.
- Tempting distractor tool used.

## Quality bar for a recorded failure

A useful failure is:

- **Specific** — exact tool call, message, or DB delta cited.
- **Reproducible** — same prompt + seed produces the same failure category.
- **Fair** — the policy and tool docs gave the agent enough to do the right thing.
- **Gradable** — the rubric or checker can decide pass/fail without judgement calls.
- **Tied to a real work consequence** — e.g. money lost, data leaked, compliance violated.

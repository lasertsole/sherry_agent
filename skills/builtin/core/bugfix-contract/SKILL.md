---
name: bugfix-contract
description: "Synthesize a bug's behavior contract before patching: a behavior matrix from the issue, every visible assertion on the same behavior, conflicts resolved in the issue's favor, and a minimal patch that satisfies both. Use for bug fixes judged by tests you cannot fully see."
when_to_use: "Fixing a reported bug whose acceptance is a hidden or partially visible test suite (e.g. SWE-bench-style tasks), or when an existing test asserts the exact behavior the issue calls wrong."
when_not_to_use: "Green-field features with no prior behavior; pure refactors; tasks where the user supplies the acceptance test explicitly."
---

# Bugfix Contract

A discipline for bug-fix tasks whose acceptance criteria are **not fully visible**: derive the
behavior contract first, then patch. The failure this prevents is patching the *symptom* while
missing the intended semantics — or worse, aligning the fix with an old test that encodes the bug.

## 1. Behavior matrix

From the issue, extract a matrix of **input → expected rendering/behavior**. Put the issue's own
example(s) in it verbatim. Add the neighboring cases the same code path handles (the fix will
touch them): rule out each row explicitly or keep it as a suspected-affected row.

## 2. Collect the visible contract

Find **every existing assertion** on the same behavior: the repo's tests, doc examples, doctests,
string fixtures. Record what each expects. Two outcomes matter:

- an existing assertion that matches the issue's expectation → it is your regression anchor;
- an existing assertion on the *same behavior* that **contradicts** the issue (it asserts exactly
  what the issue calls wrong) → it is a conflict signal, not a constraint (see step 3).

## 3. Conflict rule: the issue wins

When an issue reports rendering/behavior as inaccurate and an existing test asserts that exact
behavior, the acceptance suite for such a fix usually **flips that test**. So:

- treat the conflicting old assertion as *expected to change*; do not bend the fix to satisfy it;
- derive the fix from the issue's semantics (what the correct behavior must be) and the code's
  own invariants — not from the old expectation;
- **record the trade-off explicitly** in your final reply ("test X asserted the old behavior; the
  fix intentionally changes it"), so a reviewer can see the decision instead of discovering it.

## 4. Minimal-patch discipline

- change the least mechanism that makes the matrix correct — prefer fixing the root cause over
  adding a special case for the issue's exact input;
- the patch must keep the *non-conflicting* existing tests green (run them);
- a patch that only moves the symptom (fix one input, break a sibling input) is not done:
  re-check the whole matrix.

## 5. Reach for the upstream fix (when the environment allows)

Before inventing the contract, check whether it already exists upstream: extended git history
(`git log --all`, tags beyond the checkout), the project's issue/PR tracker, or a web search.
Probe availability with one bounded attempt (a short `curl -m 5` or a search call); if the
environment is offline, say so and fall back to the matrix — do not silently skip this step.

## 6. Pre-submit self-check

Run the matrix, row by row, in the actual repo (the rows that are runnable). List the rows you
could **not** run and why. State, in one line per row, expected vs actual. "The issue's example
now renders correctly" is necessary but not sufficient — the neighboring rows are the test.

## Honest boundary

This raises the odds; it does not guarantee the hidden contract. When the intended boundary
between two defensible behaviors is genuinely underdetermined by everything visible, the honest
move is to state both readings in your reply and pick the one most consistent with the issue and
the code's invariants — not to guess silently.

"""Task 8 package — tests for server/service/auto_turn.py adjustments.

New queueing semantics: user input arriving during an auto turn is QUEUED,
the auto turn runs to completion (no takeover cancellation, no PENDING
requeue from the auto_turn side).
"""

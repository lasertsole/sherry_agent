"""Meeting mode (spike) — discussion between experts produces a shared plan.

Three roles per the plan: the HOST opens/summarizes/closes, the SELECTOR (a
structured-output LLM) decides who speaks next from the business dependencies,
and PARTICIPANTS (registered experts) give their domain opinion. The loop lives
in :mod:`agent.meeting.flow`; participants and the selector are injectable so
tests drive the whole meeting without a model.
"""

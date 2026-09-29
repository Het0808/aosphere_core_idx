"""Guided interview: turning an under-specified question into a definitive one.

A Marketing Restrictions question is only answerable once five things are known —
jurisdiction, marketing activity, product/service category, (in the EEA, for active
marketing) who is marketing, and investor type. Asked without them, the corpus answers
"it depends", because it genuinely does: the memo carries a different route for each
combination. This package collects those five, then hands the answer path a scenario it
can be definitive about.

Three layers, deliberately separated:

  * `vocabulary` — the controlled values and their definitions. Data, no logic.
  * `engine`     — the deterministic interview: which question is next, which options are
                   ruled out, what a changed answer invalidates, and what the confirmed
                   scenario looks like. PURE — no I/O, no model, fully testable.
  * `router`     — the two model calls: classify the intent and extract what the user
                   already said; triage a mid-interview turn. The model MAPS WORDING; it
                   never decides the interview's shape.

The split is the point. A model that both classifies and drives the flow can talk itself
into skipping a question, and the cost of a wrongly-skipped question is a confident answer
to the wrong scenario.
"""

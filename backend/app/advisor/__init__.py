"""Conversational advisor (ARCHITECTURE §5) — the LLM never decides facts.

Groq is used for exactly three things: slot extraction into a strict schema,
phrasing a question the planner already chose, and explaining a recommendation
from a supplied facts object. Eligibility, products, quantities and prices come
from the deterministic engines in :mod:`app.domain`. Every LLM failure, timeout or
unverifiable output falls back to templates / the guided form.
"""

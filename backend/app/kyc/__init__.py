"""Smart KYC pipeline (ARCHITECTURE §8).

upload guard → quality gates → OCR → label-anchored parse → deterministic
validators → registry + cross-document checks → explainable verdict → review queue.

Everything here is deterministic Python; no LLM ever sees a KYC document.
"""

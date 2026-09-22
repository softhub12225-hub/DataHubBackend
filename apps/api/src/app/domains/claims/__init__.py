"""Evidence-backed candidate field claims (Step 5C.2).

normalized evidence -> deterministic field extractors -> append-only candidate claims.

**Not** `field_claim` -> canonical fact. A candidate says only *"this extractor found
this value in this exact evidence"*: not verified, not canonical, not publishable, not
conflict-free. Deterministic rules only -- no LLM, no network, no cross-page inference.
"""

from app.domains.claims.model import Candidate, Confidence, FieldKind, extractors_for

__all__ = ["Candidate", "Confidence", "FieldKind", "extractors_for"]

"""The language-model extractor's verification guard.

These tests contain no network call and need no API key. That is the point: the part
of this extractor that has to be correct is not the model, it is the rule that decides
whether to believe it. A fabricated figure and a correctly read one are
indistinguishable until the quote is checked against the document.

So the model is a stub returning whatever a test wants, and every case here asks the
same question: given this answer, does a claim survive that should not?
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import OpenAISettings
from app.domains.claims.llm import (
    EXTRACTOR_VERSION,
    LlmExtractionError,
    extract,
)
from app.domains.claims.model import Confidence, FieldKind
from app.domains.extraction.document import Block, BlockKind, NormalizedDocument


def _block(text: str) -> Block:
    return Block(kind=BlockKind.PARAGRAPH, text=text)


def _document(*texts: str, title: str | None = None) -> NormalizedDocument:
    """A real NormalizedDocument, not a stand-in.

    The extractor reads `blocks[].text`, so a stub with those two attributes would
    pass every test here and still be wrong the day the real type renames one. The
    cost of using the genuine type is four constructor arguments.
    """
    return NormalizedDocument(
        schema="test",
        extractor_name="test",
        extractor_version="0",
        media_type="text/html",
        title=title,
        blocks=[_block(text) for text in texts],
    )


class _StubClient:
    """An OpenAI client that answers with whatever the test supplies."""

    def __init__(self, payload: Any, *, raise_on_call: Exception | None = None) -> None:
        self._payload = payload
        self._raise = raise_on_call
        self.calls: list[dict[str, Any]] = []
        # The real client exposes `client.chat.completions.create`; this stands in
        # for all three levels.
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> Any:
        if self._raise is not None:
            raise self._raise
        self.calls.append(kwargs)
        content = self._payload if isinstance(self._payload, str) else json.dumps(self._payload)

        class _Message:
            def __init__(self, text: str) -> None:
                self.content = text

        class _Choice:
            def __init__(self, text: str) -> None:
                self.message = _Message(text)

        class _Response:
            def __init__(self, text: str) -> None:
                self.choices = [_Choice(text)]

        return _Response(content)


PAGE = _document(
    "Entry requirements for international applicants",
    "Applicants must hold an overall IELTS band of 7.0 with no component below 6.5.",
    "Fees are published per programme on the Programs and Courses website.",
    title="English language requirements",
)


def _settings() -> OpenAISettings:
    return OpenAISettings(api_key=SecretStr("test-key-not-used"), model="stub-model")


def _claim(**overrides: Any) -> dict[str, Any]:
    base = {
        "field_kind": FieldKind.LANGUAGE_OVERALL_SCORE.value,
        "value_raw_text": "an overall IELTS band of 7.0",
        "evidence_text": "Applicants must hold an overall IELTS band of 7.0.",
        "block_index": 1,
        "confidence": "HIGH",
        "confidence_reason": "stated directly",
        "not_stated": False,
    }
    base.update(overrides)
    return base


def test_a_quote_present_in_the_document_is_kept() -> None:
    client = _StubClient({"claims": [_claim()]})
    outcome = extract(PAGE, _settings(), client=client)

    assert len(outcome.candidates) == 1
    assert outcome.rejected_unquoted == 0
    candidate = outcome.candidates[0]
    assert candidate.value_raw_text == "an overall IELTS band of 7.0"
    assert candidate.locator.block_index == 1
    assert candidate.confidence is Confidence.HIGH


def test_an_invented_figure_is_discarded() -> None:
    """The case this module exists for.

    6.5 overall is an entirely plausible IELTS requirement and appears nowhere in this
    page. A system that merely lowered its confidence would still have put it in front
    of a reviewer as a candidate reading of the document.
    """
    client = _StubClient({"claims": [_claim(value_raw_text="an overall IELTS band of 6.5")]})
    outcome = extract(PAGE, _settings(), client=client)

    assert outcome.candidates == []
    assert outcome.rejected_unquoted == 1


def test_a_quote_assembled_from_two_separate_blocks_is_discarded() -> None:
    """Every word is present in the document; the sentence is not.

    This is the subtle fabrication -- a fluent recombination of real fragments. It
    fails because the check is a substring test against one block, not a bag of words.
    """
    client = _StubClient(
        {"claims": [_claim(value_raw_text="an overall IELTS band of 7.0 published per programme")]}
    )
    outcome = extract(PAGE, _settings(), client=client)

    assert outcome.candidates == []
    assert outcome.rejected_unquoted == 1


def test_a_correct_quote_with_the_wrong_block_index_is_kept_and_corrected() -> None:
    """Miscounting blocks is not fabricating.

    The locator is rewritten to where the wording actually is, so it still resolves for
    a reviewer, and the model's own claim about the index is preserved in `context`
    rather than silently dropped.
    """
    client = _StubClient({"claims": [_claim(block_index=0)]})
    outcome = extract(PAGE, _settings(), client=client)

    assert len(outcome.candidates) == 1
    assert outcome.candidates[0].locator.block_index == 1
    assert outcome.candidates[0].context["cited_block"] == 0


def test_whitespace_differences_do_not_count_as_fabrication() -> None:
    client = _StubClient({"claims": [_claim(value_raw_text="an overall   IELTS\n band of 7.0")]})
    outcome = extract(PAGE, _settings(), client=client)

    assert len(outcome.candidates) == 1
    assert outcome.rejected_unquoted == 0


def test_an_abstention_is_recorded_rather_than_dropped() -> None:
    """ "The page does not state this" is a finding this product publishes."""
    client = _StubClient(
        {
            "claims": [
                _claim(
                    field_kind=FieldKind.TUITION.value,
                    value_raw_text="",
                    evidence_text="Fees are published per programme.",
                    not_stated=True,
                )
            ]
        }
    )
    outcome = extract(PAGE, _settings(), client=client)

    assert outcome.abstentions == 1
    assert len(outcome.candidates) == 1
    candidate = outcome.candidates[0]
    assert candidate.unresolved_reason == "NOT_STATED_ON_PAGE"
    assert candidate.value is None
    assert candidate.value_raw_text == ""


def test_an_abstention_needs_no_quote_and_is_never_rejected() -> None:
    client = _StubClient({"claims": [_claim(value_raw_text="", not_stated=True), _claim()]})
    outcome = extract(PAGE, _settings(), client=client)

    assert outcome.rejected_unquoted == 0
    assert len(outcome.candidates) == 2


def test_an_unknown_field_kind_is_discarded_rather_than_guessed() -> None:
    client = _StubClient({"claims": [_claim(field_kind="SCHOLARSHIP_AMOUNT")]})
    outcome = extract(PAGE, _settings(), client=client)

    assert outcome.candidates == []
    assert outcome.rejected_unquoted == 1


def test_an_empty_quote_on_a_non_abstention_is_discarded() -> None:
    """A value with nothing behind it is exactly what must not reach a reviewer."""
    client = _StubClient({"claims": [_claim(value_raw_text="   ")]})
    outcome = extract(PAGE, _settings(), client=client)

    assert outcome.candidates == []
    assert outcome.rejected_unquoted == 1


def test_malformed_json_is_an_error_not_an_empty_result() -> None:
    """Silence and failure must not look the same.

    Returning no candidates here would read downstream as "this page states nothing",
    which is a publishable finding. A broken model response is not that.
    """
    client = _StubClient("not json at all")
    with pytest.raises(LlmExtractionError):
        extract(PAGE, _settings(), client=client)


def test_a_client_failure_is_an_error_not_an_empty_result() -> None:
    client = _StubClient({}, raise_on_call=RuntimeError("connection reset"))
    with pytest.raises(LlmExtractionError):
        extract(PAGE, _settings(), client=client)


def test_the_model_is_never_given_a_url_or_an_institution_name() -> None:
    """It answers from the page or not at all.

    Naming the university invites recall in place of reading, and recall is the one
    input that cannot be traced to a stored snapshot.
    """
    client = _StubClient({"claims": []})
    doc = _document("Tuition is published per programme.", title="Fees")
    extract(doc, _settings(), client=client)

    sent = json.dumps(client.calls[0]["messages"])
    assert "http" not in sent
    assert "anu.edu.au" not in sent


def test_extraction_is_requested_at_temperature_zero() -> None:
    client = _StubClient({"claims": []})
    extract(PAGE, _settings(), client=client)
    assert client.calls[0]["temperature"] == 0.0


def test_the_version_is_pinned_so_a_prompt_change_is_visible() -> None:
    """`extractor_version` is part of every candidate's fingerprint.

    If it did not move when the prompt moved, a re-run would overwrite candidates
    produced by different instructions and the two could never be compared.
    """
    assert EXTRACTOR_VERSION == "openai-1"

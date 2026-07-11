"""LLM proposal claims: the model suggests, the deterministic engine decides.

Propose never writes; a proposal survives only if the identical JSON would
be accepted as a direct PATCH; malformed model output is a 502 (our
dependency failed), while a well-formed proposal that doesn't resolve is a
422 with the standard candidates machinery (the instruction couldn't be
safely mapped onto this document)."""

import json

import pytest
from fastapi.testclient import TestClient

from app.llm import LLMError, MockLLMClient
from app.main import create_app
from tests.conftest import assert_error_envelope

GOVERNING_LAW_DOC = {
    "title": "MSA",
    "text": (
        "This Agreement shall be governed by the laws of the State of New York. "
        "The Company shall notify the Company in writing."
    ),
}


class StubLLM:
    """A provider that returns a fixed payload or fails, for exercising the
    parse/error paths without depending on mock heuristics."""

    def __init__(self, output: str | None = None, fail: bool = False):
        self.output = output
        self.fail = fail

    def propose(self, document_text: str, instruction: str) -> str:
        if self.fail:
            raise LLMError("provider exploded")
        assert self.output is not None
        return self.output


def client_with(llm) -> TestClient:
    return TestClient(create_app(llm_client=llm), raise_server_exceptions=False)


def propose(client, doc_id, instruction):
    return client.post(
        f"/documents/{doc_id}/changes/propose", json={"instruction": instruction}
    )


# --- the happy path: propose -> review -> apply ---------------------------------


def test_mock_proposal_round_trips_through_patch_with_provenance(client):
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    response = propose(
        client, doc["id"], "change the governing law from New York to Delaware"
    )
    assert response.status_code == 200
    proposal = response.json()
    assert proposal["base_version"] == 1
    assert proposal["changes"] == [
        {
            "operation": "replace",
            "target": {"text": "New York", "occurrence": None},
            "range": None,
            "replacement": "Delaware",
        }
    ]

    # The caller reviews, then applies: same schema, pinned to the version
    # the proposal was validated against, carrying the proposal id back.
    applied = client.patch(
        f"/documents/{doc['id']}",
        json={
            "changes": proposal["changes"],
            "expected_version": proposal["base_version"],
            "proposal_id": proposal["proposal_id"],
        },
    )
    assert applied.status_code == 200
    assert "laws of the State of Delaware" in applied.json()["text"]

    revisions = client.get(f"/documents/{doc['id']}/revisions").json()
    assert revisions[-1]["source"] == "llm_proposal_applied"
    assert revisions[-1]["proposal_id"] == proposal["proposal_id"]
    # And ordinary revisions are unaffected: creation is plain "api".
    assert revisions[0]["source"] == "api"


def test_propose_never_writes(client):
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    propose(client, doc["id"], "change the governing law from New York to Delaware")
    after = client.get(f"/documents/{doc['id']}").json()
    assert after["version"] == 1
    assert after["text"] == GOVERNING_LAW_DOC["text"]
    assert len(client.get(f"/documents/{doc['id']}/revisions").json()) == 1


# --- proposals that don't survive validation ------------------------------------


def test_proposal_targeting_nonexistent_text_is_422(client):
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    response = propose(client, doc["id"], "replace Guarantor with Trustee")
    body = assert_error_envelope(response, 422)
    assert "Guarantor" in body["error"]


def test_ambiguous_proposal_surfaces_candidates(client):
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    response = propose(client, doc["id"], "replace Company with Client")
    body = assert_error_envelope(response, 422)
    assert body["occurrences_found"] == 2
    assert [c["occurrence"] for c in body["candidates"]] == [1, 2]


def test_uninterpretable_instruction_is_502_not_a_guess(client):
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    response = propose(client, doc["id"], "make this contract better somehow")
    assert_error_envelope(response, 502)


# --- provider failure taxonomy ----------------------------------------------------


def test_malformed_model_output_is_502_envelope():
    client = client_with(StubLLM(output="Sure! Here are the changes you asked for..."))
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    response = propose(client, doc["id"], "anything")
    body = assert_error_envelope(response, 502)
    assert "malformed" in body["error"]


def test_schema_invalid_model_output_is_502_envelope():
    client = client_with(
        StubLLM(output=json.dumps({"changes": [{"operation": "obliterate"}]}))
    )
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    assert_error_envelope(propose(client, doc["id"], "anything"), 502)


def test_provider_failure_is_502_and_core_service_unaffected():
    client = client_with(StubLLM(fail=True))
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    assert_error_envelope(propose(client, doc["id"], "anything"), 502)
    # LLM being down does not degrade the deterministic core.
    assert client.get(f"/documents/{doc['id']}").status_code == 200


def test_code_fenced_model_output_is_tolerated():
    fenced = "```json\n" + json.dumps(
        {
            "changes": [
                {
                    "operation": "replace",
                    "target": {"text": "New York"},
                    "replacement": "Delaware",
                }
            ]
        }
    ) + "\n```"
    client = client_with(StubLLM(output=fenced))
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    assert propose(client, doc["id"], "anything").status_code == 200


# --- endpoint plumbing ------------------------------------------------------------


def test_propose_on_missing_document_is_404(client):
    assert_error_envelope(propose(client, "nope", "replace a with b"), 404)


def test_empty_instruction_is_422(client):
    doc = client.post("/documents", json=GOVERNING_LAW_DOC).json()
    assert_error_envelope(propose(client, doc["id"], ""), 422)


# --- mock determinism ---------------------------------------------------------------


@pytest.mark.parametrize(
    "instruction",
    [
        'replace "New York" with "Delaware"',
        "replace New York with Delaware",
        "change the governing law from New York to Delaware",
    ],
)
def test_mock_is_deterministic_across_phrasings(instruction):
    mock = MockLLMClient()
    first = mock.propose("irrelevant", instruction)
    assert first == mock.propose("irrelevant", instruction)
    parsed = json.loads(first)
    assert parsed["changes"][0]["target"]["text"] == "New York"
    assert parsed["changes"][0]["replacement"] == "Delaware"

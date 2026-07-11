"""Search claims: case-insensitive scan, snippet windows, pagination."""

from tests.conftest import assert_error_envelope

MSA_TEXT = (
    "This Agreement shall be governed by the laws of New York. "
    "Any material adverse effect must be reported. "
    "A Material Adverse Effect excuses performance."
)
NDA_TEXT = "The receiving party shall keep all material strictly confidential."


def make_docs(client):
    msa = client.post("/documents", json={"title": "MSA", "text": MSA_TEXT}).json()
    nda = client.post("/documents", json={"title": "NDA", "text": NDA_TEXT}).json()
    return msa, nda


def test_match_reports_offset_and_context_window(client):
    msa, _ = make_docs(client)
    body = client.get("/documents/search", params={"q": "New York", "context": 10}).json()
    assert body["total"] == 1
    result = body["results"][0]
    assert result["document_id"] == msa["id"]
    assert MSA_TEXT[result["offset"] : result["offset"] + len("New York")] == "New York"
    assert result["snippet"] == "e laws of New York. Any mate"


def test_search_is_case_insensitive_and_phrase_capable(client):
    make_docs(client)
    body = client.get("/documents/search", params={"q": "material adverse effect"}).json()
    assert body["total"] == 2  # 'material adverse' and 'Material Adverse'


def test_multi_document_search_spans_all_documents(client):
    msa, nda = make_docs(client)
    body = client.get("/documents/search", params={"q": "material"}).json()
    assert {r["document_id"] for r in body["results"]} == {msa["id"], nda["id"]}
    assert body["total"] == 3


def test_single_document_search_scopes_to_that_document(client):
    msa, _ = make_docs(client)
    body = client.get(f"/documents/{msa['id']}/search", params={"q": "material"}).json()
    assert body["total"] == 2
    assert all(r["document_id"] == msa["id"] for r in body["results"])


def test_single_document_search_missing_document_is_404(client):
    assert_error_envelope(client.get("/documents/nope/search", params={"q": "x"}), 404)


def test_pagination_windows_the_match_list(client):
    make_docs(client)
    page1 = client.get(
        "/documents/search", params={"q": "material", "limit": 2, "offset": 0}
    ).json()
    page2 = client.get(
        "/documents/search", params={"q": "material", "limit": 2, "offset": 2}
    ).json()
    assert page1["total"] == page2["total"] == 3
    assert len(page1["results"]) == 2
    assert len(page2["results"]) == 1
    offsets = {(r["document_id"], r["offset"]) for r in page1["results"] + page2["results"]}
    assert len(offsets) == 3  # no overlap between pages


def test_no_match_returns_empty_result_not_error(client):
    make_docs(client)
    body = client.get("/documents/search", params={"q": "force majeure"}).json()
    assert body["total"] == 0
    assert body["results"] == []


def test_snippet_clamps_at_document_boundaries(client):
    client.post("/documents", json={"title": "tiny", "text": "short text"})
    body = client.get("/documents/search", params={"q": "short", "context": 500}).json()
    assert body["results"][0]["snippet"] == "short text"


def test_empty_query_is_422_envelope(client):
    make_docs(client)
    assert_error_envelope(client.get("/documents/search", params={"q": ""}), 422)


def test_regex_metacharacters_in_query_are_literal(client):
    client.post("/documents", json={"title": "priced", "text": "Fee: $1,000 (net 30)."})
    body = client.get("/documents/search", params={"q": "$1,000 (net"}).json()
    assert body["total"] == 1

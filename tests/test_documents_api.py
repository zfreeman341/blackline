from tests.conftest import assert_error_envelope

SAMPLE = {"title": "Master Services Agreement", "text": "This Agreement is made..."}


def test_create_document_returns_full_text_and_version_1(client):
    response = client.post("/documents", json=SAMPLE)
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == SAMPLE["title"]
    assert body["text"] == SAMPLE["text"]
    assert body["version"] == 1
    assert body["id"]


def test_get_document_roundtrips_created_text(client):
    doc_id = client.post("/documents", json=SAMPLE).json()["id"]
    body = client.get(f"/documents/{doc_id}").json()
    assert body["text"] == SAMPLE["text"]
    assert body["version"] == 1


def test_get_missing_document_returns_404_envelope(client):
    response = client.get("/documents/does-not-exist")
    body = assert_error_envelope(response, 404)
    assert "does-not-exist" in body["error"]


def test_list_documents_returns_summaries_without_text(client):
    client.post("/documents", json=SAMPLE)
    client.post("/documents", json={"title": "NDA", "text": "Confidential..."})
    body = client.get("/documents").json()
    assert len(body) == 2
    assert {d["title"] for d in body} == {"Master Services Agreement", "NDA"}
    assert all("text" not in d for d in body)


def test_invalid_create_payload_returns_422_envelope(client):
    response = client.post("/documents", json={"title": "no text field"})
    assert_error_envelope(response, 422)


def test_empty_title_rejected_with_envelope(client):
    response = client.post("/documents", json={"title": "", "text": "body"})
    assert_error_envelope(response, 422)

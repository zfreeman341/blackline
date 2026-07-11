"""Change engine claims: resolve -> validate -> apply, atomically, against a
single base text. Mostly exercised through the API so every error path also
proves the envelope."""

import pytest

from tests.conftest import assert_error_envelope

# "Company" occurs 3 times; "Contractor" once.
CONTRACT = (
    "The Company shall indemnify the Company against all claims. "
    "The Contractor may notify the Company in writing."
)


@pytest.fixture()
def doc(client):
    return client.post(
        "/documents", json={"title": "MSA", "text": CONTRACT}
    ).json()


def patch(client, doc_id, changes, **top_level):
    return client.patch(f"/documents/{doc_id}", json={"changes": changes, **top_level})


def replace_target(text, replacement, occurrence=None):
    target = {"text": text} if occurrence is None else {"text": text, "occurrence": occurrence}
    return {"operation": "replace", "target": target, "replacement": replacement}


# --- resolve: text targets ----------------------------------------------------


def test_unique_text_target_replaces_and_bumps_version(client, doc):
    response = patch(client, doc["id"], [replace_target("Contractor", "Vendor")])
    assert response.status_code == 200
    body = response.json()
    assert "The Vendor may notify" in body["text"]
    assert body["version"] == 2
    assert body["base_version"] == 1


def test_occurrence_targets_the_nth_match(client, doc):
    response = patch(client, doc["id"], [replace_target("Company", "Client", occurrence=2)])
    text = response.json()["text"]
    assert text.startswith("The Company shall indemnify the Client against")
    assert text.count("Company") == 2


def test_target_not_found_is_422_naming_the_target(client, doc):
    response = patch(client, doc["id"], [replace_target("Guarantor", "X")])
    body = assert_error_envelope(response, 422)
    assert "Guarantor" in body["error"]


def test_occurrence_beyond_match_count_is_422(client, doc):
    response = patch(client, doc["id"], [replace_target("Company", "X", occurrence=4)])
    body = assert_error_envelope(response, 422)
    assert body["occurrences_found"] == 3


def test_ambiguous_target_returns_candidate_list(client, doc):
    response = patch(client, doc["id"], [replace_target("Company", "Client")])
    body = assert_error_envelope(response, 422)
    assert body["occurrences_found"] == 3
    candidates = body["candidates"]
    assert [c["occurrence"] for c in candidates] == [1, 2, 3]
    for candidate in candidates:
        start, end = candidate["range"]["start"], candidate["range"]["end"]
        assert CONTRACT[start:end] == "Company"
        assert "Company" in candidate["context"]
        assert len(candidate["context"]) <= len("Company") + 80


# --- resolve: ranges ------------------------------------------------------------


def test_range_target_replaces_exact_span(client, doc):
    start = CONTRACT.index("Contractor")
    changes = [
        {
            "operation": "replace",
            "range": {"start": start, "end": start + len("Contractor")},
            "replacement": "Vendor",
        }
    ]
    assert "The Vendor may notify" in patch(client, doc["id"], changes).json()["text"]


def test_range_accepts_text_as_replacement_alias(client, doc):
    changes = [{"operation": "replace", "range": {"start": 0, "end": 3}, "text": "A"}]
    assert patch(client, doc["id"], changes).json()["text"].startswith("A Company")


def test_conflicting_replacement_and_text_keys_rejected(client, doc):
    changes = [
        {
            "operation": "replace",
            "range": {"start": 0, "end": 3},
            "replacement": "A",
            "text": "B",
        }
    ]
    assert_error_envelope(patch(client, doc["id"], changes), 422)


def test_range_past_document_end_is_422(client, doc):
    changes = [
        {
            "operation": "replace",
            "range": {"start": 0, "end": len(CONTRACT) + 1},
            "replacement": "X",
        }
    ]
    body = assert_error_envelope(patch(client, doc["id"], changes), 422)
    assert "out of bounds" in body["error"]


def test_empty_range_is_insertion_at_position(client, doc):
    position = CONTRACT.index(" shall")
    changes = [
        {
            "operation": "replace",
            "range": {"start": position, "end": position},
            "replacement": ", Inc.",
        }
    ]
    text = patch(client, doc["id"], changes).json()["text"]
    assert text.startswith("The Company, Inc. shall")


def test_replacement_may_be_empty_string(client, doc):
    response = patch(client, doc["id"], [replace_target(" in writing", "")])
    assert response.json()["text"].endswith("notify the Company.")


# --- schema shape ----------------------------------------------------------------


def test_change_with_both_target_and_range_is_422(client, doc):
    changes = [
        {
            "operation": "replace",
            "target": {"text": "Company"},
            "range": {"start": 0, "end": 3},
            "replacement": "X",
        }
    ]
    assert_error_envelope(patch(client, doc["id"], changes), 422)


def test_change_with_neither_target_nor_range_is_422(client, doc):
    assert_error_envelope(
        patch(client, doc["id"], [{"operation": "replace", "replacement": "X"}]), 422
    )


def test_empty_change_list_is_422(client, doc):
    assert_error_envelope(patch(client, doc["id"], []), 422)


def test_unknown_operation_is_422(client, doc):
    assert_error_envelope(
        patch(
            client,
            doc["id"],
            [{"operation": "shuffle", "target": {"text": "Company"}, "replacement": "X"}],
        ),
        422,
    )


def test_patch_on_missing_document_is_404(client):
    response = patch(client, "no-such-doc", [replace_target("x", "y")])
    assert_error_envelope(response, 404)


# --- validate: the set --------------------------------------------------------


def test_overlapping_changes_are_422_naming_the_pair(client, doc):
    changes = [
        replace_target("Contractor", "Vendor"),
        {
            "operation": "replace",
            "range": {
                "start": CONTRACT.index("Contractor") + 2,
                "end": CONTRACT.index("Contractor") + 12,
            },
            "replacement": "X",
        },
    ]
    body = assert_error_envelope(patch(client, doc["id"], changes), 422)
    assert body["conflicting_changes"] == [0, 1]
    assert "changes 0 and 1" in body["error"]


def test_two_insertions_at_same_position_are_a_conflict(client, doc):
    insertion = {
        "operation": "replace",
        "range": {"start": 4, "end": 4},
        "replacement": "X",
    }
    assert_error_envelope(patch(client, doc["id"], [insertion, dict(insertion)]), 422)


# --- apply: atomic, against the base -------------------------------------------


def test_all_changes_resolve_against_base_not_each_other(client, doc):
    # The first change lengthens the text before the second change's range.
    # Under against-base semantics the range still means what it meant in
    # the document the caller was looking at; under sequential semantics it
    # would land shifted. This is the core semantic guarantee.
    contractor_start = CONTRACT.index("Contractor")
    changes = [
        replace_target("The Company shall", "The Company shall promptly", occurrence=None),
        {
            "operation": "replace",
            "range": {"start": contractor_start, "end": contractor_start + len("Contractor")},
            "replacement": "Vendor",
        },
    ]
    text = patch(client, doc["id"], changes).json()["text"]
    assert "The Company shall promptly indemnify" in text
    assert "The Vendor may notify" in text


def test_multi_change_request_produces_exactly_one_revision(client, doc):
    changes = [
        replace_target("Contractor", "Vendor"),
        replace_target("Company", "Client", occurrence=1),
        replace_target(" in writing", ""),
    ]
    body = patch(client, doc["id"], changes).json()
    assert body["version"] == 2
    revisions = client.get(f"/documents/{doc['id']}/revisions").json()
    assert [r["version"] for r in revisions] == [1, 2]


def test_one_bad_change_leaves_document_completely_untouched(client, doc):
    changes = [
        replace_target("Contractor", "Vendor"),
        replace_target("Guarantor", "X"),  # resolves to nothing -> whole set dies
        replace_target(" in writing", ""),
    ]
    assert_error_envelope(patch(client, doc["id"], changes), 422)
    after = client.get(f"/documents/{doc['id']}").json()
    assert after["text"] == CONTRACT
    assert after["version"] == 1
    assert len(client.get(f"/documents/{doc['id']}/revisions").json()) == 1


# --- versioning & revisions ------------------------------------------------------


def test_stale_expected_version_is_409_with_current_version(client, doc):
    patch(client, doc["id"], [replace_target("Contractor", "Vendor")])
    response = patch(
        client, doc["id"], [replace_target("Vendor", "Supplier")], expected_version=1
    )
    body = assert_error_envelope(response, 409)
    assert body["current_version"] == 2


def test_matching_expected_version_applies(client, doc):
    response = patch(
        client, doc["id"], [replace_target("Contractor", "Vendor")], expected_version=1
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2


def test_patch_without_expected_version_records_resolved_base(client, doc):
    patch(client, doc["id"], [replace_target("Contractor", "Vendor")])
    patch(client, doc["id"], [replace_target("Vendor", "Supplier")])
    revisions = client.get(f"/documents/{doc['id']}/revisions").json()
    assert [(r["version"], r["base_version"]) for r in revisions] == [
        (1, None),
        (2, 1),
        (3, 2),
    ]


def test_revision_history_records_the_applied_change_set(client, doc):
    patch(client, doc["id"], [replace_target("Contractor", "Vendor")])
    revisions = client.get(f"/documents/{doc['id']}/revisions").json()
    assert revisions[0]["change_summary"] is None  # creation
    summary = revisions[1]["change_summary"]
    assert summary == [
        {
            "operation": "replace",
            "target": {"text": "Contractor"},
            "replacement": "Vendor",
        }
    ]
    assert revisions[1]["source"] == "api"


def test_patch_response_contains_full_updated_text(client, doc):
    body = patch(client, doc["id"], [replace_target("Contractor", "Vendor")]).json()
    assert body["text"] == CONTRACT.replace("Contractor", "Vendor")

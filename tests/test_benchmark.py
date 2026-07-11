"""Benchmark tier: the ~10MB synthetic document (their spec's stated scale).

Excluded from the default run (pyproject addopts); run with:

    uv run pytest -m benchmark -s

Each test asserts a generous hard bound (so a slow CI box doesn't flake)
and prints the actual measurement; the README's performance table quotes
these numbers. The document is deterministic: seeded RNG over legal
boilerplate clauses, numbered sections, one unique sentinel clause planted
~90% deep so single-target scans pay a realistic worst case."""

import random
import time

import pytest
from fastapi.testclient import TestClient

from app.changes import ChangeError, apply_change_set
from app.main import create_app
from app.models import Change

pytestmark = pytest.mark.benchmark

TARGET_BYTES = 10 * 1024 * 1024
SENTINEL = "the XYZZY Quantum Meruit Fund"

CLAUSES = [
    "The {party} shall indemnify and hold harmless the other party from any claim arising under this Section within {days} days of written notice.",
    "No amendment to this Agreement shall be effective unless executed in writing by authorized representatives of the {party}.",
    "A material adverse effect on the business, assets, or condition of the {party} shall excuse performance for the duration of such effect.",
    "The {party} shall maintain commercially reasonable safeguards over all Confidential Information for a period of {days} months.",
    "Failure of the {party} to enforce any provision hereof shall not constitute a waiver of subsequent enforcement of that provision.",
    "All notices required hereunder shall be delivered to the {party} at its registered address and deemed received {days} business days after dispatch.",
    "This Section survives termination of this Agreement for {days} months, notwithstanding anything herein to the contrary.",
]
PARTIES = ["Company", "Contractor", "Receiving Party", "Disclosing Party"]


def generate_contract(target_bytes: int = TARGET_BYTES) -> str:
    rng = random.Random(1337)
    parts: list[str] = []
    size = 0
    section = 0
    while size < target_bytes:
        section += 1
        block = f"Section {section}. " + rng.choice(CLAUSES).format(
            party=rng.choice(PARTIES), days=rng.randint(10, 90)
        ) + "\n"
        parts.append(block)
        size += len(block)
    text = "".join(parts)
    # One unique clause, deep in the document: worst-ish case for a scan.
    planted_at = int(len(text) * 0.9)
    line_break = text.index("\n", planted_at) + 1
    return (
        text[:line_break]
        + f"Section {section + 1}. Residual amounts shall accrue to {SENTINEL}.\n"
        + text[line_break:]
    )


@pytest.fixture(scope="module")
def big_text() -> str:
    start = time.perf_counter()
    text = generate_contract()
    print(
        f"\n[benchmark] generated {len(text) / 1024 / 1024:.1f} MB "
        f"in {(time.perf_counter() - start) * 1000:.0f} ms"
    )
    return text


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def timed(label: str, bound_seconds: float, fn):
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print(f"[benchmark] {label}: {elapsed * 1000:.1f} ms (bound {bound_seconds}s)")
    assert elapsed < bound_seconds, f"{label} took {elapsed:.2f}s (bound {bound_seconds}s)"
    return result


def make_change(payload: dict) -> Change:
    return Change.model_validate(payload)


def test_single_text_target_replace_on_10mb(big_text):
    change = make_change(
        {
            "operation": "replace",
            "target": {"text": SENTINEL},
            "replacement": "the Residual Escrow Fund",
        }
    )
    new_text = timed(
        "engine: unique text-target replace over 10MB", 1.0,
        lambda: apply_change_set(big_text, [change]),
    )
    assert "Residual Escrow Fund" in new_text
    assert abs(len(new_text) - len(big_text)) == len(SENTINEL) - len(
        "the Residual Escrow Fund"
    )


def test_200_scattered_range_edits_in_one_request(big_text):
    # Near-linearity claim: one pass over the base builds the output, so
    # 200 edits cost roughly one traversal, not 200 rebuilds of 10MB.
    changes = [
        make_change(
            {
                "operation": "replace",
                "range": {"start": offset, "end": offset + 10},
                "replacement": "[REDACTED]",
            }
        )
        for offset in range(1000, len(big_text) - 1000, len(big_text) // 200)
    ]
    new_text = timed(
        f"engine: {len(changes)} scattered range edits over 10MB", 1.0,
        lambda: apply_change_set(big_text, changes),
    )
    assert new_text.count("[REDACTED]") >= len(changes)


def test_ambiguous_target_error_is_bounded_on_10mb(big_text):
    # Thousands of matches must produce a fast, capped candidate list;
    # the refuse-to-guess behavior can't cost unbounded work or payload.
    change = make_change(
        {"operation": "replace", "target": {"text": "the Company"}, "replacement": "X"}
    )
    def attempt():
        with pytest.raises(ChangeError) as excinfo:
            apply_change_set(big_text, [change])
        return excinfo.value
    error = timed("engine: ambiguous-target candidate build over 10MB", 1.0, attempt)
    assert len(error.extra["candidates"]) == 20
    assert error.extra["occurrences_found"] > 1000


def test_patch_end_to_end_on_10mb_document(big_text, client):
    doc = client.post("/documents", json={"title": "big", "text": big_text}).json()
    response = timed(
        "api: PATCH round trip incl. 10MB response serialization", 5.0,
        lambda: client.patch(
            f"/documents/{doc['id']}",
            json={
                "changes": [
                    {
                        "operation": "replace",
                        "target": {"text": SENTINEL},
                        "replacement": "the Residual Escrow Fund",
                    }
                ]
            },
        ),
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2


def test_search_phrase_across_10mb_document(big_text, client):
    doc = client.post("/documents", json={"title": "big-search", "text": big_text}).json()
    response = timed(
        "api: phrase search over 10MB", 2.0,
        lambda: client.get(
            f"/documents/{doc['id']}/search",
            params={"q": "material adverse effect", "limit": 20},
        ),
    )
    body = response.json()
    assert body["total"] > 100
    assert len(body["results"]) == 20

# Blackline

A FastAPI service for managing plain-text documents with structured, auditable changes (redlining) and search. The design follows from one sentence:

> **Every change is a proposal against a specific document version; applying it is a separate, atomic, recorded act.**

A human's PATCH request and an LLM's suggestion are the same object. Same schema, same validation, same deterministic engine, same kind of auditable revision. The versioning model, the concurrency answer, the LLM safety answer, and the audit trail all fall out of that sentence.

The worked example throughout is an in-house legal team redlining vendor contracts: people who won't tolerate silent guesses or a tool that edits a contract without a record. They're one client of a horizontal core. The engine, versioning, and search are client-agnostic; per-client policy lives at named seams (matching semantics, ambiguity policy, LLM provider and tenancy, retention).

## Quickstart

```bash
uv sync                         # or: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
uv run pytest                   # 59 tests, under a second
uv run pytest -m benchmark -s   # ~10MB tier, prints timings
./samples.sh                    # full product story; starts its own server
```

Everything runs offline with zero API keys (`MOCK_LLM=true` is the default). Real LLM proposals:

```bash
MOCK_LLM=false ANTHROPIC_API_KEY=sk-... uv run uvicorn app.main:app
```

Documents are plain text with a title. Format ingestion (docx/PDF) is out of scope on purpose; see the cut list.

## API

`samples.sh` runs all of this live. The shapes:

```
POST  /documents                    {"title": "...", "text": "..."}   -> 201, version 1
GET   /documents                    -> summaries (no text; documents can be 10MB)
GET   /documents/{id}               -> full text + version
GET   /documents/{id}/revisions     -> the audit trail
PATCH /documents/{id}               -> apply a change set, get full updated text + new version
POST  /documents/{id}/changes/propose   -> natural language in, validated change JSON out
GET   /documents/search?q=&limit=&offset=&context=
GET   /documents/{id}/search?q=...
```

A PATCH body:

```json
{
  "expected_version": 3,
  "changes": [
    { "operation": "replace",
      "target": { "text": "Company", "occurrence": 2 },
      "replacement": "Contractor" },
    { "operation": "replace",
      "range": { "start": 100, "end": 108 },
      "text": "new text" }
  ]
}
```

Each change targets by exact text (with optional 1-indexed `occurrence`) or by half-open character range, never both. `replacement` and `text` are accepted as aliases; both present with different values is a 422. `expected_version` is optional: present and stale means 409 with the current version in the body. An optional `proposal_id` marks the revision as an applied LLM proposal.

Every 4xx/5xx body is `{"error": string, "code": number}`, where `code` mirrors the HTTP status. Structured detail rides in typed fields next to the envelope:

| Case | Response |
|---|---|
| document doesn't exist | 404 |
| stale `expected_version` | 409, current version included |
| target text not found | 422, names the target |
| ambiguous target, no occurrence given | 422 with a candidate list: each occurrence's number, range, and surrounding context (capped at 20, total reported) |
| two changes overlap | 422 naming the conflicting pair |
| LLM provider down or output malformed | 502; the core service is unaffected |

The ambiguous-target response is a product behavior, not error handling. The API refuses to guess about someone's contract, and it returns exactly what's needed to disambiguate: pick an occurrence or use a candidate's range directly.

Search is case-insensitive while edit matching is exact. That asymmetry is deliberate: searching is a question, editing is an act. Results carry document id, character offset, and a snippet (`context` sets the window size). Both search endpoints paginate; the prompt's sketch only paginated multi-doc search, but an uncapped common-word query on a 10MB document isn't a response anyone wants.

## Four decisions

**(a) Versioned revisions, not in-place mutation.** Redlining is version history. A mutable text blob can't answer what changed, when, from what, on whose instruction, and those are the first questions a lawyer asks. Revisions are append-only and each records the `base_version` its change set was resolved against. `expected_version` could have been required, and arguably should be for range writes, but the sample payloads in the prompt omit it, so concurrency control is opt-in per request. The revision log records the resolved base either way; auditability doesn't depend on the caller opting in.

**(b) All changes resolve against one base text, atomically.** I considered sequential application (each change sees the previous change's output) and rejected it: it's order-dependent, it's quadratic on large documents, and the caller can't reason about offsets that move underneath them. Against-base makes a change set a pure function of `(base_version, changes)`: resolve every target to a concrete range, reject overlaps as contradictions, build the output in one pass. One request, one revision. A set with one bad change leaves the document byte-identical.

**(c) The model suggests; the deterministic engine writes.** Propose sends the document and instruction to the LLM and returns validated change JSON. It never writes. The output must parse into the same `Change` schema PATCH accepts and survive the same resolve/validate pipeline, so a hallucinated target dies as a 422 and an ambiguous instruction comes back with candidates, never silently repaired. A human applies the proposal through the normal PATCH. What they review is a minimal structured diff (exact target, exact replacement), short enough that actually reading it is trivial. Passing the `proposal_id` back stamps the revision `source: "llm_proposal_applied"`; there is no client-supplied source field, because callers labeling their own revisions would make the audit trail negotiable. In production, proposals would carry signed identifiers the apply path verifies; here provenance is client-asserted.

**(d) REST where the resource model fits, action-based where it doesn't.** Reads are GETs. The single write path is PATCH on the document, because a change set is a partial modification of that resource. `/changes/propose` is an action-style POST because proposing is a computation that returns a draft. Proposals aren't stored, so forcing them into a resource shape would imply server state that deliberately doesn't exist.

## Performance

The engine sorts resolved ranges and builds output in a single pass: near-linear in document size, not (edits x size). Search is a compiled, escaped, case-insensitive scan per document. Measured on the deterministic ~10MB benchmark document (Apple Silicon):

| Operation (10MB document) | Time |
|---|---|
| Engine: unique text-target replace | ~2.5 ms |
| Engine: 200 scattered range edits, one request | ~1.2 ms |
| Engine: ambiguous target, capped candidates from 1000+ matches | ~4.6 ms |
| API: PATCH end to end, incl. 10MB response serialization | ~10 ms |
| API: phrase search ("material adverse effect") | ~49 ms |

**Inverted index (outlined, not built).** The design is term to postings with positions. The catch for this domain: a naive token index answers "contains material AND adverse AND effect" but not the phrase "material adverse effect", and legal search is phrase-heavy. Phrase support needs positional intersection, which Postgres FTS provides for free. So the graduation path is: linear scan (fine at this corpus size, per the numbers above), then Postgres FTS once documents live in Postgres anyway, then a dedicated engine (OpenSearch) only when corpus or ranking demands it. Building the in-memory positional index is on the next-steps list.

**Streaming.** 10MB documents are comfortable in memory here. Streaming belongs at the upload/download boundary in production, not in the change engine, which needs the full base text to resolve targets. See INFRA.md.

## Cut against the timebox

- **docx/PDF ingestion**: a parsing problem, orthogonal to the change/version/search core.
- **Fuzzy or case-insensitive edit matching**: per-client policy; exact is the safe default for contracts.
- **Inverted index**: outlined above; the scan honestly wins at this scale.
- **Auth / multi-tenancy**: INFRA.md territory.
- **Persistence**: in-memory behind the repository interface, which is the Postgres seam.
- **Proposal verification**: signed proposal ids are a one-line design in INFRA.md.
- **Frontend**: samples.sh tells the story; a page was the first thing cut.

## What I'd build next

Each drops into an existing socket:

- `insert` / `delete` operations: one enum member plus one resolver each; both reduce to the engine's existing edit primitive.
- `revert`: a new revision whose text is an old revision's. History stays append-only.
- Diff between revisions.
- The positional inverted index, behind the same search interface, benchmarked against the scan.
- Search ranking and highlighting; match offsets already exist on the result model.

# Blackline — document redlining & search service

A FastAPI service for managing plain-text documents with structured, auditable changes ("redlining") and search. The design reduces to one sentence:

> **Every change is a proposal against a specific document version; applying it is a separate, atomic, recorded act.**

A human's PATCH request and an LLM's suggestion are the same object, flowing through the same validation pipeline, applied by the same deterministic engine, producing the same kind of auditable revision. Everything below — the versioning model, the concurrency answer, the LLM-safety answer, the audit trail — is that sentence, applied.

## Who this is for

The worked example throughout is an in-house legal team redlining vendor contracts: people who will not tolerate silent guesses, unauditable changes, or a tool that mutates a contract without a record. They are *one client* served by a deliberately horizontal core — the change engine, versioning, and search are client-agnostic, and the things that would vary per client are isolated behind named seams: matching semantics (exact-only here), ambiguity policy (refuse-and-return-candidates here), LLM provider and tenancy (mock/Anthropic here), retention (in-memory here). Each seam is called out below where it appears.

## Quickstart

```bash
git clone <repo> && cd blackline
uv sync                      # or: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
uv run pytest                # 58 tests, < 1s
uv run pytest -m benchmark -s   # the ~10MB tier, with timings
./samples.sh                 # the full product story, self-hosting, zero keys
```

No network or API keys are required for anything above: `MOCK_LLM=true` is the default. Real LLM proposals are one env var away:

```bash
MOCK_LLM=false ANTHROPIC_API_KEY=sk-... uv run uvicorn app.main:app
```

Documents are plain text with a title. File-format ingestion (docx/PDF) is out of scope by design — see "Cut against the timebox".

## API tour

Mirrors `samples.sh`; run that for live output.

**Create / read**

```
POST /documents                     {"title": "...", "text": "..."}        -> 201, full doc, version 1
GET  /documents                     -> summaries (no text — documents can be 10MB)
GET  /documents/{id}                -> full text + version
GET  /documents/{id}/revisions      -> the audit trail (see below)
```

**Change a document**

```
PATCH /documents/{id}
{
  "expected_version": 3,            // optional optimistic concurrency; stale -> 409
  "proposal_id": "…",               // optional provenance (see LLM section)
  "changes": [
    { "operation": "replace",
      "target": { "text": "Company", "occurrence": 2 },   // find text (exact, case-sensitive)
      "replacement": "Contractor" },
    { "operation": "replace",
      "range": { "start": 100, "end": 108 },               // or explicit half-open offsets
      "text": "new text" }                                 // `text` and `replacement` are aliases
  ]
}
-> 200 with the full updated text + version metadata
```

Exactly one of `target` / `range` per change. Failure modes, all with the `{"error": string, "code": number}` envelope (`code` mirrors the HTTP status; structured detail rides in typed fields alongside it):

- target not found → 422 naming the target
- target ambiguous → **422 with a candidate list** — each occurrence's number, character range, and ±40 chars of context. This is a product behavior, not error handling: *the API refuses to guess about someone's contract* and returns exactly what's needed to disambiguate (pick an `occurrence`, or use a candidate's `range` directly). Capped at 20 candidates with the total count reported.
- two changes overlap → 422 naming the conflicting pair (the request contradicts itself)
- stale `expected_version` → 409 with the current version
- LLM provider failure/malformed output → 502 (the deterministic core is unaffected)

**Search**

```
GET /documents/search?q=termination&limit=20&offset=0&context=60
GET /documents/{id}/search?q=...
-> matches with document id, character offset, and a context snippet
```

Search is case-insensitive while edit-matching is exact and case-sensitive — a deliberate asymmetry: searching is a question, editing is an act on someone's contract. (Their sketch paginates only the multi-doc endpoint; single-doc search got the same `limit`/`offset` so a common-word query on a 10MB document stays bounded.)

**LLM proposals**

```
POST /documents/{id}/changes/propose   {"instruction": "change the governing law from New York to Delaware"}
-> { "proposal_id": "…", "base_version": 3, "changes": [ …validated change JSON… ] }
```

This endpoint **never writes**. The model's output must parse into the *same* `Change` schema PATCH accepts (one shared Pydantic model — one source of truth) and must survive the *same* resolve/validate pipeline. A hallucinated target dies as a 422; an ambiguous one comes back with the candidates machinery. The caller reviews and applies via PATCH — ideally with `expected_version: base_version` (so the proposal still means what it meant) and `proposal_id`, which stamps the resulting revision's `source` as `"llm_proposal_applied"`. `source` is derived, never client-supplied: callers labelling their own revisions would make the audit trail's meaning negotiable. Provenance here is client-asserted (the id isn't verified) — in production, proposals would carry signed identifiers the apply path verifies.

**The audit trail**

Revisions are append-only — nothing is ever mutated in place. Each records the version it created, the `base_version` its change set was resolved against, the change set as submitted, a timestamp, and its provenance (`"api"` or `"llm_proposal_applied"` + proposal id). The final frame of `samples.sh` shows human edits and an applied LLM proposal side by side in one history.

## Four decisions, with the debate

**(a) Versioned revisions instead of in-place mutation.** Redlining *is* version history — a mutable text blob can't answer "what changed, when, from what, by whose instruction," and those are the first four questions a lawyer asks. Revisions also make range-targeting safe (a range is only meaningful relative to a version) and give optimistic concurrency for free. `expected_version` could have been required — arguably should be, for range-targeted writes — but the client's sample payloads omit it, and following their sketched interface was the higher-priority constraint. So concurrency control is opt-in per request, while the revision log records the resolved `base_version` regardless: auditability does not depend on the caller opting in.

**(b) All changes resolve against a single base text, atomically.** The alternative — sequential application, each change seeing the previous change's output — was considered and rejected: it's order-dependent (the same set means different things shuffled), quadratic on large documents (every step rebuilds the text), and impossible for a caller to reason about (change 3's offsets depend on what changes 1–2 did). Against-base semantics make a change set a pure function of `(base_version, changes)`: resolve every target to a concrete range, reject contradictions (overlaps), then build the output in one pass. One request → one atomic transformation → one revision; a set with one bad change leaves the document byte-identical.

**(c) Propose ≠ apply for the LLM.** The model suggests; the deterministic engine writes. The propose endpoint returns validated change JSON and never touches the document — a human reviews and submits it through the same PATCH path as any manual edit. This is the safety architecture real legal AI needs: hallucinated targets die in validation, ambiguous instructions surface candidates instead of guesses, and the audit trail records exactly which revisions originated from a model. Provider failure degrades *only* the propose endpoint (502) — the core service doesn't depend on the LLM being up, which is itself a product decision.

**(d) REST where the resource model fits, action-based where it doesn't.** Reads are plain GETs on resources. The single write path is PATCH on the document resource — a change set *is* a partial modification of the document, so the resource verb fits. But `/changes/propose` is deliberately an action-style POST: proposing is a computation that returns a draft, not CRUD on any stored resource (proposals aren't persisted). Bending it into a resource shape (`POST /proposals` + `GET /proposals/{id}`?) would imply server-side proposal state that deliberately doesn't exist.

## Performance

Approach: the engine sorts resolved ranges and builds output in a single pass over the base text — near-linear in document size, and crucially *not* O(edits × size). Search is a linear scan per document via a compiled, escaped, case-insensitive pattern. Measured on the ~10MB deterministic legal-boilerplate document (`uv run pytest -m benchmark -s`, Apple Silicon):

| Operation (10MB document) | Time |
|---|---|
| Engine: unique text-target replace | ~2.5 ms |
| Engine: 200 scattered range edits, one request | ~1.2 ms |
| Engine: ambiguous target → capped candidate list (>1000 matches) | ~4.6 ms |
| API: PATCH end-to-end incl. 10MB response serialization | ~10 ms |
| API: phrase search ("material adverse effect") | ~49 ms |

**The inverted index (outlined, deliberately not built).** Design: tokenize each revision's text into terms with positions; maintain `term -> [(doc_id, [positions])]`; answer queries by intersecting posting lists. The catch for this domain: a naive token index answers "which documents contain *material* and *adverse* and *effect*" but not the phrase "material adverse effect" — and legal search is overwhelmingly phrase- and term-of-art-shaped. Phrase support needs positional intersection (adjacent-position joins), which is exactly what Postgres full-text search with `phraseto_tsquery` gives you for free — so the graduation path is: linear scan (here, ~50ms at 10MB — honestly fine at this corpus size) → Postgres FTS when documents live in Postgres anyway (INFRA.md) → a dedicated engine (OpenSearch) only when corpus size or ranking demands it. Implementing the in-memory positional index is on the live-coding menu below.

**Streaming.** 10MB documents are comfortably handled in memory here (the numbers above say so). Streaming enters in production at the upload/download boundary — multipart upload to object storage, streamed responses — not in the change engine, which needs the full base text to resolve targets against. See INFRA.md.

## Cut against the timebox

- **File-format ingestion (docx/PDF)** — a document-*parsing* problem, orthogonal to the change/version/search core being evaluated.
- **Fuzzy or case-insensitive edit matching** — a per-client policy seam; exact-only is the safest default for contracts, and the seam is one strategy behind the target resolver.
- **Inverted index** — outlined above instead; their spec offered outline-or-implement, and the honest engineering answer is that a linear scan wins at this corpus size.
- **Auth / multi-tenancy** — INFRA.md territory; nothing in the core assumes a principal.
- **Persistence** — in-memory behind `DocumentRepository`; the interface is the Postgres seam, and route handlers never see storage details.
- **Proposal verification** — `proposal_id` is client-asserted; signed proposal identifiers are a one-line design in INFRA.md.
- **Frontend** — the API story is told by `samples.sh`; a static page was the first thing cut, per plan.

## What I'd build next

Each of these drops into an existing socket:

- **`insert` / `delete` operations** — one enum member + one resolver each; both reduce to the engine's existing `ResolvedEdit` primitive (insert: empty span at an anchor; delete: empty replacement).
- **`revert`** — `POST /documents/{id}/revert` creating a *new* revision whose text is an old revision's (append-only history preserved; no rewriting).
- **Diff between revisions** — `GET /documents/{id}/diff?from=2&to=5`, rendering the change summaries or a computed text diff.
- **The positional inverted index** — as outlined, behind the same search interface, with the benchmark tier as the referee.
- **Search ranking & highlighting** — term proximity + match offsets already exist on the result model.

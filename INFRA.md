# Part II — Production infrastructure

> Skeleton. Every box in the eventual architecture diagram corresponds to a
> seam visible in the Part I codebase; the mapping table is the spine of the
> document.

## Seam mapping: Part I code → production counterpart

| Seam in this repo | Where | Production counterpart |
|---|---|---|
| `DocumentRepository` interface | `app/repository.py` | Postgres (documents + revisions tables, transactional version bump) |
| Append-only `Revision` log | `app/repository.py` / `app/models.py` | Append-only audit store: partitioned revisions table, WORM/retention policy per client |
| Optimistic concurrency (`expected_version` → 409) | `app/routes.py` | `UPDATE … WHERE version = ?` compare-and-swap; no app-level locks |
| Linear-scan search | `app/search.py` | Postgres FTS (`phraseto_tsquery` for phrase queries) → OpenSearch when corpus/ranking demands |
| `LLMClient` protocol (mock / Anthropic) | `app/llm.py` | Provider abstraction; per-client model choice, in-tenant endpoints where required |
| Client-asserted `proposal_id` | `app/routes.py` | Signed proposal identifiers, verified on the apply path |
| Error envelope handlers | `app/errors.py` | Gateway-level error contract, consistent across services |
| `MOCK_LLM` / env config | `app/llm.py` | Secrets manager + per-environment config |
| In-process state | `app/main.py` | Stateless app tier; all state in the data layer → horizontal scaling |

## Deployment models for legal clients

<!-- TODO(zack): the centerpiece section -->

### Multi-tenant SaaS

### Single-tenant VPC

### The LLM call path when documents cannot leave the tenant

## Data lifecycle & retention

<!-- TODO(zack): revision retention, legal hold, deletion vs. the append-only trail -->

## Observability & audit

<!-- TODO(zack): the revision log as the product-level audit trail vs. operational logging -->

## Scaling story

<!-- TODO(zack): stateless app tier, read paths, when search graduates (mirror README performance section) -->

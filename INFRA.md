# Part II — Production infrastructure

> One page, trade-offs over prose. Every production component below maps to a
> seam visible in the Part I codebase (table at bottom).

## Architecture & Infra

<!-- TODO(zack): compute (stateless app tier), storage (Postgres: documents +
     append-only revisions), caching (what's worth caching and what must never
     be stale), object storage at the upload/download boundary -->

## Security & Compliance

<!-- TODO(zack): auth (SSO/OAuth2 at the gateway), encryption in transit/at
     rest, the revision log as the product-level audit trail, GDPR/SOC2
     posture, retention & legal hold vs. append-only history -->

### Deployment models for legal clients

<!-- TODO(zack): multi-tenant SaaS vs single-tenant VPC; the LLM call path
     when documents cannot leave the tenant (in-tenant model endpoints);
     signed proposal identifiers on the apply path -->

## CI/CD & Deployment

<!-- TODO(zack): pipeline (lint/test/benchmark gate), rollout strategy
     (blue-green or canary; stateless app tier makes this cheap), migrations
     on an append-only schema -->

## Scalability & Resilience

<!-- TODO(zack): horizontal scaling (all state in the data layer), version
     bump as compare-and-swap UPDATE (replaces the in-memory lock), search
     graduation path: linear scan -> Postgres FTS -> OpenSearch, failover,
     LLM provider degradation is already isolated to the propose endpoint -->

## Monitoring & Observability

<!-- TODO(zack): metrics (change-set rejection rates BY REASON — ambiguity
     rate is a product signal, not just an error rate), tracing across the
     propose->apply loop, alerts, structured logs; operational logging vs.
     the audit trail -->

## Operations & Cost

<!-- TODO(zack): cost centers (LLM tokens dominate; per-tenant budgets),
     right-sizing, multi-region only when a client's data residency demands
     it, cost of append-only storage and when to archive cold revisions -->

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

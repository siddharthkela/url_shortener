# Run summary: Bug fix: generated short code collides with a custom alias

**Requirement:** Bug report: creating a short URL sometimes fails with 409 Conflict even though no custom alias was requested.
**Branch:** `orchestrator-demo/bugfix-alias-collision`
**Run ID:** bugfix-alias-collision

## Decision lineage

1. **requirements** — Normalized: POST /api/v1/urls without a customAlias must never fail because its generated code is already held by an alias.
   - *Rationale:* The report names the symptom (409 with no alias). The acceptance criteria pin down the invariant to restore without widening scope.
2. **codebase_analysis** — Root cause: UrlService assigns Base62(id) to generated rows without checking whether a custom alias already holds that code; aliases and generated codes share one unique short_code column.
   - *Rationale:* Read UrlService.java and CreateUrlRequest.java to confirm both halves of the defect: the unchecked generated path and the overlapping alias pattern.
3. **design** — Fix: In the generated path, if Base62(id) is already taken, delete that row and insert a fresh one to take the next id; repeat until the code is free.
   - *Rationale:* Smallest change that restores the invariant, stays inside the existing transaction, and leaves the public API and URL formats untouched.
4. **test** — Wrote/modified: ['src/test/java/com/urlshortener/integration/ShortCodeCollisionRegressionTest.java', 'src/test/java/com/urlshortener/service/UrlServiceTest.java']
   - *Rationale:* Integration test reproduces the defect against real H2; the unit test pins down the skip-and-retry behavior with mocks.
5. **docs** — Wrote: ['README.md']
   - *Rationale:* 
6. **reproduce** — Bug reproduced: new tests fail on unfixed code with DataIntegrityViolation
   - *Rationale:* A fix is only meaningful against a reproduction. The test is now known to detect the defect, so it turning green after the fix is real evidence.
7. **implementation** — Wrote/modified: ['src/main/java/com/urlshortener/service/UrlService.java']
   - *Rationale:* 
8. **approval** — Approved node 'release_readiness'
   - *Rationale:* autonomy=assisted, requires_approval=True
9. **release** — Release status: ready
   - *Rationale:* 

## Reliability metrics

| Metric | Value |
|---|---|
| Success rate | 100% (11/11 nodes) |
| Retries | 0 |
| Rollbacks | 0 |
| MTTR | n/a |
| Total latency | 32.69s |

## Risks & trade-offs

- Skipping a colliding id discards that identity value, leaving a gap in the id sequence. Ids are internal and never exposed except through their Base62 code, so a gap has no user-visible effect.
- Each collision costs one extra insert and delete inside the same transaction. Collisions need an alias equal to a future Base62(id), so they are rare, and the loop always terminates because every alias can block at most one id.

## Assumptions

- The existing public contract stays: aliases may be any 3-16 character [a-zA-Z0-9_-] string, including ones that look like generated codes. Rejecting such aliases would also fix the bug, but would break clients that already rely on them, so it was rejected as a behavior change.
- The reported 409 comes from generated creates only. Alias creates already check existsByShortCode and return a clear 409 'alias taken', which is correct behavior.

## Limitations

- The reverse direction (a client requesting an alias equal to an already-generated code) was already handled correctly before this fix and is unchanged.

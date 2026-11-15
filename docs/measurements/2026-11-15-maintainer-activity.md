# Maintainer activity measurement (#1202)

Measured the candidate helper in the deployed 4.1.0 container runtime, Python
3.15.2 free-threaded and SQLite 3.53.1, on 2026-11-15. The database was opened
with `mode=ro` and `PRAGMA query_only=1`. An explicit read transaction provided
one consistent snapshot for counts, computation, plans and memory measurement.
It lasted 3.57 seconds. No database copy, schema change or production cache write
was made. The helper source was loaded into the measurement process in memory;
`cache.set` was intercepted to encode and measure payloads without publishing.

The measured helper's SHA-256 was
`3920a450e0018be097f70ba315e128165d5726c3dd2ea4c20f37d21647fcb425`.

## Corpus and bounds

Both queries used the same half-open UTC interval:
`[2026-11-08T22:37:33.813713Z, 2026-11-15T22:37:33.813713Z)`.

| Quantity | Rows |
| --- | ---: |
| Articles in the interval | 47,288 |
| Distinct raw author headers in the interval | 3,783 |
| Reviewed-by, Acked-by and Tested-by trailer rows in the interval | 13,239 |
| Grouped review-trailer addresses | 1,285 |
| Current distinct lowercased M addresses | 2,072 |
| One-hop mailmap pairs | 894 |
| Eligible positive-message ranking | 677 |

The existing date-index statistics recorded 17,927,590 articles (`17927590 1`).
That is a planner statistic, not a fresh full-table count.

## Actual SQL and plans

Captured from the running helper, with bound timestamps and the three roles:

```sql
SELECT author FROM articles INDEXED BY ix_articles_date
WHERE date >= ? AND date < ?;

SELECT t.address_normalized, count(*)
FROM articles AS a INDEXED BY ix_articles_date
CROSS JOIN article_trailers AS t ON t.article_id = a.id
WHERE a.date >= ? AND a.date < ?
  AND t.role IN (?, ?, ?)
GROUP BY t.address_normalized;
```

```text
SEARCH articles USING INDEX ix_articles_date (date>? AND date<?)

SEARCH a USING COVERING INDEX ix_articles_date (date>? AND date<?)
SEARCH t USING INDEX ix_article_trailers_article_id (article_id=?)
USE TEMP B-TREE FOR GROUP BY
```

Neither query scans the archive or joins list membership. Replaying and fully
consuming the captured SQL on the same snapshot took 23.8 ms for authors and
15.2 ms for grouped trailers. These are warm-page-cache SQL timings, measured
after the first aggregate computation, not disk-cold predictions.

## Compute and added warming cost

The first forced computation took **0.794 seconds**, including a fresh current
maintainer directory, bulk mailmap read, streamed header parsing, both queries,
ranking, generation recheck and intercepted cache encoding. The ranking encoded
to 70,100 bytes with the measurement's ordinary JSON spacing; the refreshed
directory encoded to 141,051 bytes. Production compact JSON is smaller.

A separate repeat measured **1,225,277 bytes of peak traced Python allocations**
during computation. Process maximum resident memory after the first computation
and over the measurement was **140,708 KiB**, including the interpreter and
imported application. This is not incremental aggregate memory.

The existing `handle_warm_global` ran with only the new target selected and the
normal slow-tier refresh window. Its separate read-only snapshot used the same
time bounds. Added handler wall time was **0.765 seconds**, with the target
reported as 764 ms and no errors. This isolates the new target's compute and
encoding cost; it is neither the duration of the full slow cycle nor a measure
of writer-queue/cache-publication latency. No production warm RPC was sent.

These measurements support adding the target to slow global warming before the
per-maintainer fan-out. They do not require a schema/index change or author
materialization. The web path only reads the version and cache and remains cold
until slow warming populates the current generation.

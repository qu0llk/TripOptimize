# How route optimization works

## Flow (end-to-end)

```
User request
  → orchestrator.submit()          creates DB task (PENDING), puts task_id into asyncio.Queue
  → _worker_loop()                 single background coroutine, blocks on queue.get()
  → LegPlanner.plan()              expands city list into RouteLeg(city_a, city_b, date) objects
  → _gather_tickets()              checks DB cache; cache misses → ScraperManager
  → ScraperManager.collect_iter()  runs Aviasales + RZD scrapers in parallel, yields per-leg
  → _assemble_itinerary()          calls optimizer
  → find_optimal_route()  (TSP)    tries every permutation of intermediate cities
  → optimize_itinerary()  (DP)     picks cheapest/fastest ticket sequence for one city order
  → result saved to DB → COMPLETED
```

---

## Queue

`asyncio.Queue` of `task_id` UUIDs. One worker coroutine runs forever in `_worker_loop`. `submit()` enqueues; worker dequeues and processes sequentially. No Redis/Celery — designed for ≤2 concurrent users. Progress (`_progress` dict) is updated in-memory as each leg completes so SSE can stream it live.

---

## Scrapers ↔ Optimizer interconnection

**Scrapers** produce `TicketDTO` objects (price, departure/arrival datetimes, duration, source, baggage). `ScraperManager` runs both scrapers in parallel for each leg; results stream out via `collect_iter`.

`_gather_tickets` groups all collected tickets by `(departure_city, arrival_city)` pair into `tickets_by_pair: dict[tuple[str,str], list[TicketDTO]]`.

**Optimizer** receives that dict and city ordering. It never calls scrapers — it only reads from `tickets_by_pair`.

---

## LegPlanner: what gets scraped

Two strategies:

- **SequentialLegPlanner** — one pair per hop, one date per surplus day. Linear number of legs. Used by default.
- **AllPairsLegPlanner** — all directed pairs needed for TSP (origin↔each intermediate, intermediate↔intermediate). More legs = more proxy traffic, but lets optimizer try every city order.

Each `RouteLeg(city_a, city_b, date)` becomes one scraper request (skipped if DB cache is fresh).

---

## Optimizer: DP + TSP

### TSP shell — `find_optimal_route`

Tries every permutation of intermediate cities. For each order calls `optimize_itinerary`. Picks the permutation with lowest total cost. Falls back to user's original order if no complete path found. Complexity: O(N! × T²), practical up to N≤7.

### DP core — `optimize_itinerary`

State: `(accumulated_cost, surplus_days_left, last_arrival_dt, path)`.

For each leg, a ticket is valid if:
```
prev_arrival + min_stay  ≤  ticket.departure  ≤  prev_arrival + min_stay + surplus_days + 12h
```

Each valid ticket extends the state; `_prune` drops dominated states (same last ticket, worse cost or less surplus). After all legs, the state with minimum cost wins.

Metric is either `price` (money) or `duration_minutes` (time).

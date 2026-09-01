# Scenario I — Dealership Engagement Comparison

Range **30 Days** · generated 2026-09-01 15:05 UTC

## Top 3 events

| Event | Ejner Hessel | Approved Automotive |
|---|---:|---:|
| Forms submitted | 130 (65.0%) | 130 (65.0%) |
| Carousel clicked | 40 (20.0%) | 40 (20.0%) |
| CTAs clicked | 30 (15.0%) | 30 (15.0%) |
| **Total clicks** | **200** | **200** |

## Answer

- **More clicks:** neither — both record 200.
- **More submitted forms:** neither — both record 130.

## Notes

- Both dealerships return identical engagement figures, which matches the example in the brief exactly. This card appears to serve fixed data on this environment rather than per-dealership values; the comparison is reported as a tie rather than inventing a winner.

## How this was produced

The operator signed in with a saved session, opened each dealership from the
sidebar tree by its id, switched to the Analytics tab, applied the date range,
and read the User Engagement card from the DOM. Values are read from the
chart's legend markup rather than from the rendered canvas.

Sources: https://seezar-dashboard.seez.dev/dealership/2/analytics/527 · https://seezar-dashboard.seez.dev/dealership/11929/analytics/454
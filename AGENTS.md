# Repository rules

## Web and combat performance

Performance is part of the game contract. A feature is not complete if it makes a button
delay other players, repeats deterministic combat work, or sends data the open screen
does not render.

When changing `pets_web.py`, combat, dungeon actions, achievements, farming, economy
settlement, or any code using `_farm_settlement_lock`:

1. Never run disk I/O, full-store JSON parsing, combat simulation, image processing, or
   lock-waiting synchronous code on the aiohttp event-loop thread. Run the complete
   blocking unit through `asyncio.to_thread`; do not offload only one small part while
   leaving setup or response assembly on the event loop.
2. Read the pet store once when constructing immutable inputs for one fight. Build both
   fighters from that snapshot with the pure `*_for(record)` helpers. Do not call public
   helpers that each reload the whole store for every stat, item, scroll, or shield.
3. Run one simulation for one live fight. Reuse its result and immutable fighter
   snapshots for settlement, audit, playback, logging, and the HTTP response. Never
   simulate again merely to render or record the fight.
4. Derive each fighter's combat values at most once per response-building stage. Share
   that result between the fighter header, maximum-HP bars, and other playback fields.
5. Every mutation response must identify the current view when building state. Itemless
   views (`arena`, `dungeon`, `farm`, `quests`, `more`) must return `bag: null`; never send
   the inventory catalogue, item descriptions, or art URLs to a screen that does not draw
   them.
6. Do not add an automatic follow-up HTTP request when the action response already
   contains the fresh state. If another request is necessary, document which information
   is missing and why adding it to the existing response is worse.
7. Never perform a currency grant, `_load`, or `_save` once per item/achievement inside a
   loop. Load once, mutate the already-loaded store, and save once. Keep
   `_farm_settlement_lock` sections as short as possible and never hold that lock across
   network calls, sleeps, simulations, or response rendering.
8. Do not add wall-clock sleeps to production request paths or replay setup. Visual replay
   delays belong in the client animation and must respect the existing skip preference.
9. Preserve `Server-Timing` on state, action, and arena-fight responses so production
   reports can separate server time from connection and client-animation time.

## Required regression checks

Any pull request that touches the paths above must keep or extend tests proving all
applicable properties:

- an intentionally stalled combat calculation does not delay an unrelated web request;
- one live fight calls `pets_combat.simulate` exactly once;
- playback derives each side once and reuses it for HP;
- itemless action responses contain `bag: null`;
- bulk repairs and payouts have bounded store read/write counts, asserted as numbers;
- the Mini App JavaScript remains syntactically valid.

Run at minimum:

```powershell
python -m unittest tests.test_pets_web tests.test_pets_combat tests.test_pets_gatekeeper tests.test_pets_phoenix -q
```

For a focused web change, run the affected test by its full `unittest` name first, then
the full command above before handoff. Avoid fragile absolute latency limits for normal
local work: test concurrency with a deliberately blocked worker and events, so a slow CI
machine cannot create false failures.

Before finishing, inspect the response shape and the number of store reads/writes as well
as correctness. A test that only checks the final winner or HTTP 200 does not protect
performance.

Test audit — 2026-09-09

The clearest excess is source-code policing: tests that require a particular variable, import, CSS class, phrase, or number of source occurrences. Most of the security and federation scenarios describe distinct trust boundaries. Reducing those to a single happy-path test would lose useful protection.

This is a **static triage of the complete inventory, with focused review of flagged cases**. It is not a completed manual, production-code trace of every test. No tests or application code were changed, and the application suites were not run. Runtime cost and flakiness have not been measured. The conservative “retain provisionally” entries are explicitly distinguished from specific recommendations; they are not assertions that those tests are all necessary or sufficient.

The [per-test ledger](inventory.csv) contains a location, A–E disposition, reason, review depth, consolidation destination where applicable, and separate CI action. The [file index](files.md) makes the inventory easier to navigate.

| Scope | Entries |
| --- | ---: |
| Backend | 2,316 |
| Frontend | 645 |
| Authored LiveKit patch tests | 5 |
| Mobile | 504 |
| Desktop, including archived Slint | 143 |
| Python SDK | 383 |
| Deployment | 64 |
| **Total across 373 files** | **4,060** |

Counts represent named test declarations and shell scenarios, not expanded parameterized executions. Python declarations were extracted with AST; frontend declarations with TypeScript AST; Dart/Rust declarations were located statically. Shell parser assertions are separate entries; the updater script has five scenario entries. Third-party dependency/build trees are excluded. The five tests added by the checked-in LiveKit patch are included. Verification programs and runner checks are assessed separately below.

| Disposition | Specific recommendations | Interpretation |
| --- | ---: | --- |
| A — Loosen | 27 | Remove incidental constraints while preserving the actual contract. |
| B — Strengthen | 34 | Assert the promised outcome, sometimes by replacing source inspection with execution. |
| C — Remove | 19 | Tautologies, source/layout sentinels, or redundant registry bookkeeping. |
| D — Combine | 28 | Entries to absorb into named case tables or an existing shared contract check. |
| E — Other | 39 | Replace, rename, or retain archived/artifact checks for a specific reason. |
| E — Retain provisionally | 3,913 | No specific change established by the static triage. |

These are **147 targeted entries**, not 147 independent bugs. A combination includes its destination test in the count. Separate execution-wiring findings also apply to otherwise provisionally retained tests.

The first changes worth making

1. **Delete the three fixture/callback tests that do not exercise useful application behavior.** [Web uploads](../../frontend/src/lib/media/uploads.test.ts#L49) asserts that its own fixture ID is a string, then that its own object has the status it was assigned. [Mobile voice Apps](../../mobile/test/voice_app_launcher_test.dart#L7) constructs a widget, reads back its callback, and calls that local callback; it never renders or taps Apps. Type checking covers these shapes. If launcher wiring needs coverage, test an actual tap.

2. **Delete eight tests that inspect the federation verifier's implementation.** In [test_verify_federation_helpers.py](../../backend/tests/test_verify_federation_helpers.py), the entries at lines 112, 149, 187, 212, 256, 284, 309 and 324 freeze default wait values, local names, AST call counts, source positions, or the chosen emoji. They can pass without successful federation and fail after a harmless refactor. Keep the real federation verification and the helper tests that actually exercise bounded retries, deadline handling and failure reporting. Do not delete the whole file.

3. **Replace security-sensitive source searches with a few real behavior scenarios.** The web permission surfaces, gateway permission coherence, encrypted rich-media privacy and VAD enforcement suites search source rather than exercise the route/runtime. A required substring can remain in dead code while a protection breaks. Keep the distinct cases: denied permissions, revoked access, late completion after revocation, hidden-page PTT release, and blocked external media. Drive the event or interaction and observe state, requests or media effects. This is stronger coverage with fewer implementation constraints; merely weakening their regexes is not sufficient.

4. **Make oldest-first cache eviction testable.** [Desktop cache](../../desktop/crates/kaede-cache/src/lib.rs#L555) accepts either `/cache/old` or `/cache/new`. Both writes can receive the same second-resolution timestamp, so the test never proves its oldest-first claim. Seed distinct stored access times, require the older path, and retain the cross-account check. Do not introduce a real sleep to force an ordering.

5. **Strengthen the clipboard assertion.** [Authenticated media](../../frontend/src/lib/media/authenticated.test.ts#L13) checks only that `clipboard.write` was called once. Assert the MIME type and actual copied bytes. The neighboring “retries an expired signed path” test only returns a path unchanged: rename it to the behavior it exercises instead of presenting it as renewal coverage.

6. **Replace migration source assertions with migration effects.** Reading a migration and finding a trigger condition, `drop_column`, or SQL fragment cannot prove that the SQL executes or preserves rows. Keep focused ORM foreign-key/constraint checks. Test upgrade/backfill and rejected downgrade using a disposable PostgreSQL database with rows that exercise the boundary. Existing migration verification is the place to absorb this work, not a second testing framework. The full table-name inventory in [test_models.py](../../backend/tests/test_models.py#L11) is a deletion candidate; the focused integrity checks are not.

7. **Centralize migration graph checking.** Four historical migration tests assert literal revision/head constants. Some names claim to establish a single head without inspecting the graph. Use one Alembic graph check for head count and reachability. Historical tests should not need unrelated edits whenever a new migration becomes current. Also freeze historical emoji migration vectors instead of requiring a frozen migration to keep matching future runtime behavior.

8. **Keep useful mobile widget coverage; loosen the cosmetic inventory.** [Settings visual tests](../../mobile/test/discord_settings_visual_test.dart#L443) genuinely render and navigate, unlike the web source searches. Retain those actions and render-error checks. Drop incidental uppercase headings and exact summary copy. Their unusually tall canvas establishes structure, not small-phone layout. Preserve the separate keyboard/short-viewport tests. In [appearance tests](../../mobile/test/appearance_test.dart#L204), “square” should not require exactly `40 × 40` pixels.

9. **Rename and improve the mobile database suite.** [local_database_performance_test.dart](../../mobile/test/local_database_performance_test.dart) contains functional checks, not benchmarks. Keep the crypto worker round trip. The other two tests record database calls; they do not show that rows were replaced or removed. A small SQLite-backed scenario with an existing row and a different account/kind can prove both effects and isolation.

10. **Narrow four generic exception expectations.** The ledger identifies backend response rendering, DM history validation, LiveKit webhook validation and profile-proof validation. Use their concrete application exceptions; preserve status/error semantics. These are small improvements, not a reason to duplicate the surrounding suites. For example, the production wrappers raise `FederationNetworkError` for invalid DM history/profile proof and `LiveKitError` for an invalid webhook signature.

What to combine, and what to keep separate

The landing-variant, context-menu geometry and reaction-presentation cases can become small named tables. Two mobile swipe tests share the same move-sequence procedure and can share a table. Preserve every input and diagnostic label. Do not turn different state-machine scenarios into a long sequential test that stops at the first failure.

The scattered endpoint registration lists can feed the existing application smoke test. This combines method/path bookkeeping; it does not replace authorization tests for the endpoints.

Three Lua tests inspect `cjson`/`string.sub`/`redis.call` spelling. Combine their intended contracts into actual Redis/Dragonfly cases for dispatch, local presence transitions and remote presence. Assert that empty arrays and payload values survive generation updates. Keep a named case for each script; do not require every script to have the same implementation.

Do **not** automatically combine these:

- Backend enforcement and client visibility. They run different code and catch different failures.
- Browser, Dart, Rust and Python cryptographic vectors. They verify separate implementations against a shared byte contract.
- Unit validation and PostgreSQL rollback/locking scenarios. Mocks cannot establish database atomicity or lock ordering.
- Permission grants and subsequent revocations. Successful initial access does not prove cleanup when access is lost.
- Retry, exact replay, stale generation and equal-generation conflicting data. Those are distinct state transitions.
- Real gesture cases for cancellation, deliberate reversal, nested recognizers and disabled rows. Their different event sequences matter.

Exact cryptographic bytes, canonical identities, wire tags, version fences, omitted-versus-null fields and no-secret/no-side-effect assertions should remain strict. User-facing prose, CSS classes, local variable names and tunable UI dimensions usually should not.

Checks and execution wiring

| Check / runner | Action | Recommendation |
| --- | --- | --- |
| Main backend pytest job | E — Keep | Useful unit/contract suite. Its ordinary database URL does not activate the dedicated PostgreSQL tests. |
| SDK pytest suite | E — Wire | Absent from the main workflow and Makefile test targets. Run it in an appropriate SDK environment rather than assuming backend pytest discovers it. Preserve the explicit optional native/LiveKit integration cases. |
| Tauri embedded tests | E — Wire | `make desktop-test` excludes `kaede-tauri`; native CI runs `cargo check`. Neither check nor clippy executes the embedded tests. Run the existing Tauri tests explicitly where supported. |
| Desktop toolchain checks | E — Fix | Makefile desktop targets pin `+1.92.0`, while the workspace requires Rust 1.97 and CI installs 1.97.1. Align the existing commands before treating these gates as useful evidence. |
| `test:livekit-crypto` | E — Wire | The five authored patch tests are outside the normal frontend glob. `check:e2ee-wasm` exercises a different artifact. Run the existing dedicated cryptor command in CI. |
| PostgreSQL test files | E — Wire | Dedicated URLs activate bulk moderation, interaction atomicity, owner authority, reaction migration and tracker tests. Compose's `migration-check` invokes the first four; main CI's independently written migration job does not invoke that target/list. Tracker is missing from that Compose list too. Use a disposable migrated database and run the existing tests explicitly. |
| Migration command sequences | D — Consolidate | CI and Compose maintain different sequences. Establish one shared sequence so the guarded/data-bearing upgrade/downgrade checks and database tests do not silently diverge. Keep real data-preservation checks. |
| `make check` followed by `make test` | D — Remove duplicate execution | Both Compose `backend-check` and `frontend-check` already execute their unit suites. The `*-test` services run them again. Decide which target owns execution; retain lint/type/build coverage. This is a local target duplication, not evidence that main CI runs both commands. |
| Generated-protocol drift | D — One owner | `test_generated_protocol_files_are_current` compares web, Rust, Dart **and Python** outputs. CI regenerates and checks three output directories again. Keep one authoritative drift gate and explicit published-value contracts; do not replace the four-output check with the narrower directory list. |
| `verify_schema.py` | E — Keep | Actually executes PostgreSQL constraints, cascades and partition routing. This is substantially better evidence than migration substring tests. |
| `verify_migration_guards.py` | E — Keep | Its seeded data and guarded downgrade scenarios protect retained state; ensure they are wired consistently as above. |
| `verify_identity.py` | E — Keep provisionally | Real identity/service integration is distinct from mocked unit behavior. No runtime-cost case for removal established. |
| `verify_chat.py` | E — Keep provisionally | Retain the real service flow. Replace fixed sleeps only where an observable completion condition exists; avoid widening them indiscriminately. |
| `verify_media.py` | E — Keep | Storage, scanner and media-access integration is not reproduced by URL/parser unit tests. Keep safety rejection and pre-scan access checks. |
| `verify_voice.py` | E — Keep | Redis/LiveKit integration checks real grants and service behavior; pure policy helpers cannot replace them. |
| `verify_release.py` | B — Improve fanout evidence | `verify_shared_fanout` counts total messages across subscribers. Track expected message indices per subscriber so a duplicate on one subscriber cannot compensate for a missing delivery elsewhere. Retain a generous bounded smoke deadline; do not infer a performance benchmark from it. |
| Federation HTTP + TLS runs | E — Keep provisionally | TLS has a separate transport/proxy boundary. A smaller TLS smoke may eventually suffice, but deleting the full second run needs scenario overlap and timing evidence not established here. |
| `verify-csp.mjs` | B — Narrow positive checks | Keep the built-artifact check. Some allowed-origin assertions search the whole policy; require the origin in its intended directive (`connect-src`, etc.), as the script already does for some `frame-src` checks. A source appearing in an unrelated directive should not pass. |
| `verify-e2ee-wasm.mjs` | E — Keep | Executes the checked-in WASM artifact and its JS bindings. Native Rust source tests cannot prove those shipped artifacts agree. |
| Deploy environment / Compose validators | A — Loosen source extras | Keep unsafe-config rejection and resolved topology checks. Replace exact interpolation counts and shell prompt/source matches with resolved/emitted values. |
| Nginx example tests + `nginx -t` | A — Keep semantic requirements | Syntax validation does not prove WebSocket forwarding or admission limits exist. Keep semantic directive checks; avoid whitespace and incidental numeric/zone-name coupling. |
| Setup input script | E — Keep | Small positive and negative parser cases, including overflow, are useful. Python deployment tests already invoke it. |
| Auto-update script tests | E — Wire | The Makefile exposes `auto-update-check`, but main CI does not invoke it. Existing scenarios protect credential redaction, enable/disable state and dirty/wrong-branch rejection. They do not establish a successful real update/rollback flow. |
| Archived Slint tests | E — Archive together | Explicitly excluded from the workspace. Keep with the archived implementation; do not add them to active CI or delete only their tests. |
| AV1 interoperability tools under `docs/av1-e2ee` | E — Keep as dedicated validation | Real browser/native transport evidence is different from parser/worker unit tests. The existing README identifies environment/device limitations; this audit did not rerun it. |
| Lint, formatting, type checks, build and dependency audits | E — Keep | They establish different properties. No measured cost/overlap evidence here justifies deleting them. Avoid repeating the same build when configuration and artifact needs are truly identical. |
| CI cache-budget script | E — Keep outside test counts | This manages disposable runner caches, not application correctness. Do not treat its budget values as test coverage. |

Suggested implementation order: remove tautologies and verifier/layout sentinels; fix missing execution and duplicate runners; replace the security/migration source checks with behavior at the existing boundaries; then apply optional table consolidation and wording cleanup. Avoid deleting a security guard's only test before its behavioral replacement exists.

Validation of this report: inventory locations, unique file/line keys, totals, and the listed override targets were checked against the working tree. No claims of passing suites, measured speedups or demonstrated flakiness are made.

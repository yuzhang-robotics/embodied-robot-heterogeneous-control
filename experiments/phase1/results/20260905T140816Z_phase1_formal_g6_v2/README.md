# Phase 1 G6 v2 Failed Formal Attempt

This report preserves and independently reconstructs the first G6 v2 formal attempt. The attempt stopped on a system-under-test Gate failure and is not confirmatory evidence.

## Source and integrity

| Field | Value |
| --- | --- |
| Collection | `20260905T140816Z_phase1_formal_g6_v2` |
| Session attempt | `session-01-attempt-01` |
| Runner commit | `1e5e1c75f2dc3717ebcb07ea7f145e5a59e23a6f` |
| Protocol | `phase1-g6-fixed-input-sync-async-v2` |
| Protocol SHA-256 | `5aa995a563234429ae7fca513e89bd64e2f75130e6d0502591dfb427134fab0a` |
| Collection archive SHA-256 | `0306a0c9e5e2746b9da37c15db3189c51cc131771d515dfe97d420b1f829a892` |
| llama-server log archive SHA-256 | `67352addf8dcb67c57eeaa19cd5b5e90afd6e819bddeab42ed3d669e2af6ab40` |
| Verified manifest artifacts | 42 |
| Resource samples | 3558 |

All manifest-declared artifact sizes and hashes, the frozen protocol, preflight, ledger prefix, event traces and resource records were revalidated. Raw inputs, model text, private paths and raw service logs are not included in this report.

## Attempt outcome

| Field | Observation |
| --- | --- |
| Manifest status | `aborted` |
| Failure class | `system_under_test` |
| Completed entries | 17 |
| Run records inspected | 18 |
| Gates | 179 passed, 1 failed |
| Failed entry | ordinal 18, VLM `formal_async` |
| Failed Gate | `translation_route_verified` |
| Observed translation route | `argos` |

## Failure diagnosis

Moondream completed in 69732.743 ms. The Qwen rewrite then remained at its 30 s client boundary for 30029.203 ms, failed, and the adapter completed through the Argos fallback. The bound llama-server record shows the corresponding task was cancelled 30073.946 ms after launch and the slot returned to idle. The VLM child process exited normally and its IPC protocol completed.

In the recorded implementation, the Moondream unload request followed the Qwen rewrite and fallback. The attempt therefore contains a model-residency-order confound. It does not by itself prove that residency caused the timeout. The isolated correction moves the unload request between Moondream inference and Qwen rewriting while retaining the 30 s Qwen timeout for the next descriptive diagnostic.

## Safety and resource checks

The session maximum Tj was 55.093 C; the failed-run maximum was 54.812 C, well below the 85 C stop threshold. No thermal stop, sampler failure, child-process lifecycle failure or model-service crash was observed.

## Decision

G6 v2 is closed after this system-under-test failure. The attempt will not be rerun, replaced or entered into confirmatory timing analysis, and no Phase 1 formal claim is permitted. After the residency-order correction is reviewed, a separate descriptive Jetson diagnostic will determine whether a future protocol version can retain the 30 s timeout.

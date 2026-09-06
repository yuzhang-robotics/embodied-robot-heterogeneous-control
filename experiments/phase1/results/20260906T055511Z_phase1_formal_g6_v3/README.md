# Phase 1 G6 v3 Failed Formal Attempt

This report preserves and independently reconstructs the first G6 v3 formal attempt. The attempt stopped on a system-under-test Gate failure and is not confirmatory performance evidence.

## Source and integrity

| Field | Value |
| --- | --- |
| Collection | `20260906T055511Z_phase1_formal_g6_v3` |
| Session attempt | `session-01-attempt-01` |
| Runner commit | `3dca66ba6752650cad081293fc5e6601ab84d270` |
| Protocol | `phase1-g6-fixed-input-sync-async-v3` |
| Protocol SHA-256 | `070ec2d571c957a413567a2d2bd92d3dddd2e9fb07a7b1ef8c0c0c89bcdcfc4b` |
| Collection archive SHA-256 | `601a097e5691264a663e88c07b9ea07e6c5b9bf7c3db4cbf6594ab3a14d41c69` |
| llama-server log archive SHA-256 | `a18b253e477a18b5e09bd8fa1e928112e8f4d51f9a951779a00fbd009b308239` |
| Verified manifest artifacts | 26 |
| Resource samples | 1724 |

All manifest-declared artifact sizes and hashes, the frozen protocol, preflight, ledger prefix, event traces, resource records and llama-server request sequence were revalidated. Raw inputs, model text, private paths and raw service logs are not included.

## Attempt outcome

| Field | Observation |
| --- | --- |
| Manifest status | `aborted` |
| Failure class | `system_under_test` |
| Completed entries | 9 |
| Run records inspected | 10 |
| Gates | 97 passed, 2 failed |
| Failed entry | ordinal 10, VLM `formal_sync` |
| Failed Gates | `residency_contract_verified`, `translation_route_verified` |
| Translation route | `argos` |

## Failure diagnosis

The corrected-order contract is bound to the runner commit: Moondream completed in 55698.124 ms, its unload request returned in 311.674 ms, and Qwen then remained at the 30 s client boundary for 30031.008 ms. The adapter completed through the Argos fallback in 53802.869 ms.

The bound llama-server request was not cancelled. It completed and released its slot after 30117.120 ms, 117.120 ms beyond the configured timeout, and the server returned to idle. The VLM child process also exited normally with process protocol `0.2.0` complete.

| Qwen request | Warm-up | Failed measured | Difference |
| --- | ---: | ---: | ---: |
| Prompt tokens | 161 | 171 | +10 |
| Generated tokens | 32 | 37 | +5 |
| Server total (ms) | 25826.320 | 30117.120 | +4290.800 |

The longer prompt and generation counts are associated with the boundary crossing, but two observations do not establish why their counts or evaluation rates differed. In particular, unload completion is not observable from the current Ollama interface, so neither prompt length nor residency is assigned as a causal explanation.

## Safety and resource checks

The session and failed-run maximum Tj were both 55.812 C, below the 85 C stop threshold. No thermal stop, sampler failure, child-process lifecycle failure or model-service crash was observed.

## Decision

G6 v3 is closed after this system-under-test failure. The attempt will not be rerun, replaced or entered into confirmatory timing analysis. The preregistered G6 success criterion is not met, so the Phase 1 application slice is not authorized. This is a negative Phase 1 result, not evidence for a synchronous/asynchronous performance comparison.

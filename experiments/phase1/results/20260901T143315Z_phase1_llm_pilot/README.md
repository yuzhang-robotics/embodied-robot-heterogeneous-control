# Phase 1 Fixed-input LLM Pilot

This report records one motion-disabled correctness pilot on the Jetson Orin Nano. It validates the local llama.cpp HTTP boundary, response identity handling and old-generation result rejection.

## Evidence boundary

- Workload: one fixed Phase 0 prompt with empty history per condition.
- Conditions: one `llm_async` run followed by one `llm_stale` run.
- Backend: one pre-existing loopback llama-server retained across both runs.
- Physical motion and UART access: disabled.
- Permitted interpretation: integration and lifecycle correctness evidence.
- Not permitted: cancellation-latency, backend-cancellation, asynchronous superiority, hard-real-time, performance or heterogeneous-inference claims.

## Provenance

| Field | Value |
| --- | --- |
| Session | `20260901T143315Z_phase1_llm_pilot` |
| Source commit | `6e83ede085cdb4b025172e5c41da930c4989eff3` |
| Source branch | `main` |
| Transfer archive SHA-256 | `889debda235c475ad70362980c6a85e90b9a4c782937f2bb5b0c128cecb0797e` |
| Independently valid runs | yes |
| LLM G5 component | satisfied |

Machine-readable derived data: [`analysis.json`](analysis.json).

## Frozen identities

| Field | Value |
| --- | --- |
| Input SHA-256 | `15ee277f4140cb3c2bca3d4762e6462e098787e5b5843245760d9f40da2ea7f2` |
| Input size | 124 bytes |
| Model SHA-256 | `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e` |
| Model size | 1117320736 bytes |
| Served model | `qwen2.5-1.5b-instruct-q4_k_m.gguf` |
| llama.cpp source | `b9246-2-g585080d31` |
| Model alias | `qwen` |
| Temperature | 0.400 |
| Maximum completion tokens | 80 |
| System prompt SHA-256 | `5e4cd3892f6603935b7c33f0c77c4b47936cdeab9dfc0db67f86c85e35b10081` |
| Empty-history SHA-256 | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` |

Prompt, history and response text, raw HTTP data and private filesystem paths are not serialized.

## Correctness results

| Condition | Execution | Disposition | Accepted | Stale consumed | Output length | Prompt/completion/total tokens | Gates |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| `llm_async` | `ok` | consumed=1 | 1 | 0 | 43 | 103/26/129 | pass |
| `llm_stale` | `cancel_observed` | rejected_state=1 | 0 | 0 | 40 | 103/26/129 | pass |

The nominal response identity was consumed exactly once. The stale response completed at the blocking HTTP boundary but was rejected before consumption because its state generation was obsolete.

## Cancellation and residency boundary

| Condition | Requested | Worker observed | Client wait stopped | Backend stop confirmed | Unload requested |
| --- | --- | --- | --- | --- | --- |
| `llm_async` | no | no | no | n/a | no |
| `llm_stale` | yes | yes | no | n/a | no |

State invalidation did not stop the blocking HTTP wait and does not prove that backend inference stopped. The server remained externally managed, and neither run requested model unload.

## Observation control and descriptive timing

The stale observation control was 500.000 ms, compared with a 200 ms resource interval.

| Condition | Adapter total (ms) | Resource samples | In-adapter samples |
| --- | ---: | ---: | ---: |
| `llm_async` | 2555.156 | 23 | 13 |
| `llm_stale` | 1758.723 | 19 | 9 |

These single, fixed-order durations are descriptive. The stale duration includes the deliberate observation window and is not cancellation latency.

## Periodic-probe observations

| Condition | Ticks | Skipped | Deadline misses | Max lateness (ms) | Max gap (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `llm_async` | 46 | 0 | 0 | 0.209 | 100.062 |
| `llm_stale` | 38 | 0 | 0 | 0.393 | 100.224 |

## Resource observations

| Condition | Samples | Covered | RAM mean/max (MB) | GR3D mean/max (%) | Tj max (C) | VDD_IN mean/max (mW) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `llm_async` | 23 | 13 | 3936.696/3941.000 | 15.609/99.000 | 51.531 | 6800.000/9151.000 |
| `llm_stale` | 19 | 9 | 3958.105/3960.000 | 25.895/99.000 | 51.687 | 6988.105/9349.000 |

## Phase 1 Gate status

- The VLM and ASR components of G5 were satisfied by their previously reviewed pilots.
- The LLM correctness-pilot component of G5 is satisfied by this session.
- Phase 1 G5 is complete; G6 preregistration and formal data collection are next.

## Evidence gaps

- `single_run_per_condition`
- `fixed_condition_order`
- `no_real_workload_synchronous_condition`
- `formal_thresholds_not_frozen`
- `stale_observation_window_is_pilot_control`
- `cancellation_latency_not_measured`
- `blocking_http_wait_not_preempted`
- `backend_stop_not_confirmed`
- `response_content_not_serialized`
- `response_identity_not_a_quality_threshold`
- `server_lifetime_externally_managed`
- `resource_activity_not_attributed_to_llama_cpp_or_processor`
- `emc_unavailable`

Device-level GR3D activity is not attributed to llama.cpp or a specific processor. This evidence does not authorize a heterogeneous-inference claim.

Formal performance, backend-cancellation and cancellation-latency claims remain prohibited: `cancellation_latency_claim_permitted=False`, `backend_cancellation_claim_permitted=False`.

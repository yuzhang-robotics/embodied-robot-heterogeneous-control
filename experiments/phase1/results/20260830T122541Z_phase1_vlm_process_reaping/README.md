# Phase 1 Process-isolated Fixed-input VLM Pilot

This report records one motion-disabled spawned-process correctness pilot on the Jetson Orin Nano. It validates process ownership, bounded IPC, stale-result rejection and deterministic child reaping.

## Evidence boundary

- Workload: one fixed Phase 0 C100 image per condition.
- Conditions: one `vlm_async` run followed by one `vlm_stale` run.
- Isolation: one spawned child per VLM request; broker and probe remain in the parent.
- Physical motion and UART access: disabled.
- Permitted interpretation: integration, lifecycle and process-boundary correctness.
- Not permitted: causal performance claims, timing-domain isolation, hard-real-time or heterogeneous-inference claims.

## Provenance

| Field | Value |
| --- | --- |
| Session | `20260830T122541Z_phase1_vlm_process_reaping` |
| Source commit | `1818c83de574f44e0253216ab591d3be8c57d2f3` |
| Source branch | `main` |
| Transfer archive SHA-256 | `7074838c73ce23720b47decd9165f96fb6033e734fc010574969482d0d088dc2` |
| Independently valid runs | yes |
| Process Gates passed | yes |

Machine-readable derived data: [`analysis.json`](analysis.json).

## Frozen identities

| Field | Value |
| --- | --- |
| Input SHA-256 | `607c9faf3ea03b8b032d8c1d9e86c697d9fb48ca3c2f278e453941da6b871be7` |
| Input size | 9009 bytes |
| Moondream digest | `55fc3abd386771e5b5d1bbcc732f3c3f4df6e9f9f08f1131f9cc27ba2d1eec5b` |
| Qwen model | `qwen2.5-1.5b-instruct-q4_k_m.gguf` |

## Correctness and process results

| Condition | Execution | Disposition | Accepted | Cancel forwarded | Exit | Terminate | Slice/Process Gates |
| --- | --- | --- | ---: | --- | ---: | --- | --- |
| `vlm_async` | `ok` | consumed=1 | 1 | no | 0 | no | pass |
| `vlm_stale` | `cancel_observed` | rejected_state=1 | 0 | yes | 0 | no | pass |

The nominal result was consumed once. The stale result was rejected before consumption after cancellation was forwarded. Child exit does not confirm that the external model backend stopped inference.

## Process supervision timing

| Condition | Spawn -> start (ms) | Start -> inference (ms) | Inference -> completion receipt (ms) | Completion -> join (ms) | Total supervision (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `vlm_async` | 180.500 | 14382.295 | 53111.196 | 61.044 | 67735.035 |
| `vlm_stale` | 181.305 | 15282.647 | 44743.007 | 104.367 | 60311.326 |

## Pipeline timing

| Condition | Adapter total (ms) | Module import | Moondream | Qwen | Unload |
| --- | ---: | ---: | ---: | ---: | ---: |
| `vlm_async` | 67677.763 | 14380.694 | 30949.417 | 21756.256 | 400.828 |
| `vlm_stale` | 60210.021 | 15281.019 | 27780.618 | 16547.201 | 402.481 |

These single, fixed-order timings are descriptive and are not compared inferentially.

## Periodic-probe observations

| Condition | Ticks | Skipped | Deadline misses | Max lateness (ms) | Max gap (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `vlm_async` | 698 | 0 | 0 | 0.780 | 100.647 |
| `vlm_stale` | 624 | 0 | 0 | 0.419 | 100.272 |

## Descriptive thread reference

- Reference session: `20260830T073825Z_phase1_vlm_pilot`
- Reference commit: `aebd1a22f8d9a9bfce1cd8dfd2f089e1cc48a204`
- Reference analysis SHA-256: `9d5835453ffe857c33e180f9c75840396d199f8c6cb10a1facb63d1b6e15f04d`

| Condition | Thread skipped | Process skipped | Thread max gap (ms) | Process max gap (ms) |
| --- | ---: | ---: | ---: | ---: |
| `vlm_async` | 85 | 0 | 4262.876 | 100.647 |
| `vlm_stale` | 63 | 0 | 2700.375 | 100.272 |

The thread reference recorded 148 skipped releases and the spawned-process pilot recorded 0. This is a descriptive mitigation signal, not a causal or performance-superiority claim; the sessions used single fixed-order runs from different commits.

## Resource observations

| Condition | Samples | Covered | RAM mean/max (MB) | GR3D mean/max (%) | Tj max (C) | VDD_IN mean/max (mW) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `vlm_async` | 339 | 329 | 4682.702/5714.000 | 5.782/99.000 | 53.906 | 5926.339/16646.000 |
| `vlm_stale` | 303 | 293 | 4517.340/5662.000 | 6.347/99.000 | 53.375 | 5881.010/16215.000 |

## Evidence gaps

- `single_run_per_condition`
- `fixed_condition_order`
- `no_real_workload_synchronous_condition`
- `formal_thresholds_not_frozen`
- `model_unload_not_independently_confirmed`
- `resource_activity_not_attributed_to_a_model_or_processor`
- `emc_unavailable`
- `thread_process_comparison_crosses_sessions`
- `thread_process_order_not_randomized_or_balanced`
- `thread_process_source_commits_differ`
- `backend_stop_not_confirmed_by_process_exit`

Both process runs recorded loopback-only listener bindings. Process exit and cancellation forwarding are bounded local facts. This evidence does not prove backend preemption.

GR3D activity is device-level evidence and is not attributed to a particular model or processor. The result does not authorize a heterogeneous-inference claim.

A timing-domain or performance claim remains prohibited: `timing_domain_isolation_claim_permitted=False`.

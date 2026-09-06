# Phase 1 VLM Residency-order Diagnostic

This report reconstructs one motion-disabled, fixed-input diagnostic of the corrected VLM residency order. It is descriptive readiness evidence and is not part of a formal comparison.

## Provenance

| Field | Value |
| --- | --- |
| Session | `20260905T160805Z_phase1_vlm_residency_diag` |
| Source commit | `08e262dfe999bda9fd5a3c79aaa6ff2f611b50bd` |
| Collection archive SHA-256 | `f6c22ce6e396494af4d0dfcc16c30602d1dedd9f95d87ad8a5af22fdf599911e` |
| llama-server log archive SHA-256 | `a9be360fa20036d53f057f2190b74c3ff427f5de8fd5c7cfe2f8861b6fa5a0ad` |
| Raw inputs, model text, logs or paths published | no |

All run artifacts were independently validated before these derived facts were emitted.

## Frozen diagnostic contract

The successful path was `Moondream inference -> unload request -> Qwen rewrite`, using spawned-process protocol `0.2.0` and the existing 30 s Qwen request timeout. Unload confirmation is not available.

## Pipeline observations

| Condition | Route | Moondream (ms) | Unload (ms) | Qwen (ms) |
| --- | --- | ---: | ---: | ---: |
| `vlm_async` | `qwen` | 66796.177 | 649.694 | 18400.091 |
| `vlm_stale` | `qwen` | 31490.743 | 201.025 | 18864.649 |

## Lifecycle observations

| Condition | Final disposition | Accepted | Child exit | Process protocol |
| --- | --- | ---: | ---: | --- |
| `vlm_async` | consumed=1 | 1 | 0 | `0.2.0` |
| `vlm_stale` | rejected_state=1 | 0 | 0 | `0.2.0` |

Both slice and process Gate sets passed, both children exited normally, and the stale result was rejected before consumption. The llama-server log contains 2 completed requests and 0 cancellation records.

## Decision

The diagnostic supports freezing the corrected order for G6 v3 and retaining the 30 s Qwen timeout. It does not support changing that threshold or reopening G6 v2.

## Claim boundary

The single run per condition and fixed condition order do not establish residency-order causality or performance superiority. Device-wide resources are not attributed to a model or processor. Backend preemption, timing-domain isolation, hard-real-time behavior and heterogeneous inference remain unproven.

`residency_order_causality_established=False`; `performance_comparison_permitted=False`.

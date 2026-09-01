# Phase 1 Fixed-input ASR Pilot

This report records one motion-disabled correctness pilot on the Jetson Orin Nano. It validates native Whisper integration, transcript identity, stale-result rejection and local process reaping.

## Evidence boundary

- Workload: one fixed Phase 0 WAV per condition.
- Conditions: one `asr_async` run followed by one `asr_stale` run.
- Backend: one native `whisper-cli` subprocess per request.
- Physical motion and UART access: disabled.
- Permitted interpretation: integration and lifecycle correctness evidence.
- Not permitted: cancellation-latency, asynchronous superiority, hard-real-time, performance or heterogeneous-inference claims.

## Provenance

| Field | Value |
| --- | --- |
| Session | `20260831T140705Z_phase1_asr_pilot_v2` |
| Source commit | `bc1ca3578807fbe2fae440155c57076435ac5d5d` |
| Source branch | `main` |
| Transfer archive SHA-256 | `d8029a2820bc68d1995e62693bf0ae89098350f1cb8b70f9b2de7d31adde29b5` |
| Independently valid runs | yes |
| ASR G5 component | satisfied |

Machine-readable derived data: [`analysis.json`](analysis.json).

## Frozen identities

| Field | Value |
| --- | --- |
| Input SHA-256 | `3fffeee1e04250faa483174a423878bf220b95f6706684f6e109ed8f9b731440` |
| Input size | 114136 bytes |
| Model SHA-256 | `1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b` |
| Model size | 487601967 bytes |
| whisper.cpp source | `v1.8.4-326-gafa2ea54` |
| Transcript SHA-256 | `9b718ac6e824461152cb5dd402453b7b43bf000f708b257cd6d2d10d109f4a49` |
| Transcript length | 21 characters |

The transcript text and private filesystem paths are not serialized.

## Correctness results

| Condition | Execution | Disposition | Accepted | Stale consumed | Exit | Terminated | Reaped | Gates |
| --- | --- | --- | ---: | ---: | ---: | --- | --- | --- |
| `asr_async` | `ok` | consumed=1 | 1 | 0 | 0 | no | yes | pass |
| `asr_stale` | `cancel_observed` | rejected_state=1 | 0 | 0 | -15 | yes | yes | pass |

The nominal transcript identity was consumed exactly once. The stale request was rejected before consumption; its local Whisper child was terminated and reaped with backend-stop confirmation scoped only to that child process.

## Observation control and descriptive timing

The stale observation control was 500.000 ms, compared with a 200 ms resource interval.

| Condition | Adapter total (ms) | Resource samples | In-adapter samples |
| --- | ---: | ---: | ---: |
| `asr_async` | 1510.738 | 18 | 8 |
| `asr_stale` | 619.968 | 13 | 3 |

These single, fixed-order durations are descriptive. The stale duration contains the deliberate observation window and is not cancellation latency.

## Periodic-probe observations

| Condition | Ticks | Skipped | Deadline misses | Max lateness (ms) | Max gap (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `asr_async` | 36 | 0 | 0 | 0.203 | 100.072 |
| `asr_stale` | 27 | 0 | 0 | 0.417 | 100.257 |

## Resource observations

| Condition | Samples | Covered | RAM mean/max (MB) | GR3D mean/max (%) | Tj max (C) | VDD_IN mean/max (mW) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `asr_async` | 18 | 8 | 3790.500/3950.000 | 18.389/99.000 | 51.687 | 6371.444/9032.000 |
| `asr_stale` | 13 | 3 | 3773.692/3917.000 | 4.385/53.000 | 51.468 | 5512.231/6061.000 |

## Phase 1 Gate status

- The ASR correctness-pilot component of G5 is satisfied.
- Phase 1 G5 remains open because the real LLM correctness slice is pending.
- G6 preregistration and formal data collection remain downstream of G5.

## Evidence gaps

- `single_run_per_condition`
- `fixed_condition_order`
- `no_real_workload_synchronous_condition`
- `formal_thresholds_not_frozen`
- `stale_observation_window_is_pilot_control`
- `cancellation_latency_not_measured`
- `transcript_content_not_serialized`
- `resource_activity_not_attributed_to_whisper_or_processor`
- `emc_unavailable`

Device-level GR3D activity is not attributed to Whisper or a specific processor. This evidence does not authorize a heterogeneous-inference claim.

A formal performance or cancellation-latency claim remains prohibited: `cancellation_latency_claim_permitted=False`.

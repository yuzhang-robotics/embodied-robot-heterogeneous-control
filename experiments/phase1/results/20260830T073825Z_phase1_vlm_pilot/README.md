# Phase 1 Fixed-input VLM Pilot

This report records one motion-disabled correctness pilot on the Jetson Orin Nano. It validates real-model integration and stale-result rejection; it is not a synchronous/asynchronous performance comparison.

## Evidence boundary

- Workload: one fixed Phase 0 C100 image per condition.
- Conditions: one `vlm_async` run followed by one `vlm_stale` run.
- Model path: Ollama/Moondream description followed by llama.cpp/Qwen rewriting.
- Physical motion and UART access: disabled.
- Permitted interpretation: integration and lifecycle correctness evidence only.
- Not permitted: asynchronous superiority, hard-real-time, timing-isolation or heterogeneous-inference claims.

## Provenance

| Field | Value |
| --- | --- |
| Session | `20260830T073825Z_phase1_vlm_pilot` |
| Source commit | `aebd1a22f8d9a9bfce1cd8dfd2f089e1cc48a204` |
| Source branch | `main` |
| Transfer archive SHA-256 | `01869a420203929bc895c589082ec1d60244b2ba2ec8865fc6249bfaf07cae23` |
| Independently valid runs | yes |

Machine-readable derived data: [`analysis.json`](analysis.json).

## Frozen identities

| Field | Value |
| --- | --- |
| Input SHA-256 | `607c9faf3ea03b8b032d8c1d9e86c697d9fb48ca3c2f278e453941da6b871be7` |
| Input size | 9009 bytes |
| Moondream digest | `55fc3abd386771e5b5d1bbcc732f3c3f4df6e9f9f08f1131f9cc27ba2d1eec5b` |
| Qwen model | `qwen2.5-1.5b-instruct-q4_k_m.gguf` |

## Correctness results

| Condition | Execution | Route | Disposition | Accepted | Stale consumed | Gates |
| --- | --- | --- | --- | ---: | ---: | --- |
| `vlm_async` | `ok` | `qwen` | consumed=1 | 1 | 0 | pass |
| `vlm_stale` | `cancel_observed` | `qwen` | rejected_state=1 | 0 | 0 | pass |

The nominal result was consumed once. After the state generation advanced, the stale result completed but was rejected before consumption. Backend stop remains unconfirmed by design.

## Pipeline timing

| Condition | Adapter total (ms) | Module import | Moondream | Qwen | Unload |
| --- | ---: | ---: | ---: | ---: | ---: |
| `vlm_async` | 79757.102 | 18886.883 | 39249.051 | 21057.203 | 559.293 |
| `vlm_stale` | 61776.670 | 15130.227 | 27933.338 | 18367.115 | 342.301 |

These single, fixed-order observations are descriptive and are not compared inferentially.

## Periodic-probe observations

The probe used a 100 ms absolute schedule. Skipped releases are reported separately from callbacks that started after their deadline.

| Condition | Ticks | Skipped | Skip rate (%) | Deadline misses | Max lateness (ms) | Max gap (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `vlm_async` | 733 | 85 | 10.391 | 0 | 88.396 | 4262.876 |
| `vlm_stale` | 575 | 63 | 9.875 | 0 | 93.881 | 2700.375 |

Skipped-release attribution from scheduled timestamps:

| Condition | Adapter stage | Skipped releases |
| --- | --- | ---: |
| `vlm_async` | `module_import` | 85 |
| `vlm_stale` | `module_import` | 63 |

Both runs missed scheduled releases during lazy module import. The simulated sleep pilot therefore does not establish that a Python worker thread isolates every real workload. Process-level isolation or an equivalent mitigation must be evaluated before a timing-isolation claim.

## Resource observations

| Condition | Samples | Covered | RAM mean/max (MB) | GR3D mean/max (%) | Tj max (C) | VDD_IN mean/max (mW) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `vlm_async` | 398 | 388 | 5344.666/6366.000 | 4.131/99.000 | 54.343 | 5778.158/16242.000 |
| `vlm_stale` | 310 | 300 | 4978.861/5990.000 | 6.129/99.000 | 54.343 | 5962.639/16803.000 |

## Evidence gaps

- `single_run_per_condition`
- `fixed_condition_order`
- `no_real_workload_synchronous_condition`
- `formal_thresholds_not_frozen`
- `model_unload_not_independently_confirmed`
- `resource_activity_not_attributed_to_a_model_or_processor`
- `listener_binding_evidence_not_recorded`
- `probe_skipped_releases_observed`
- `emc_unavailable`

The source runs used VLM preflight schema 0.1.0, which recorded a loopback request URL but not the TCP listener addresses. The operator verified loopback binding before execution, but that observation is not contained in the archived artifacts and is not elevated into a reproducible claim.

GR3D activity confirms only that the device reported GPU activity during the run window. The trace does not attribute activity to Moondream, Qwen or a particular processor, so it does not authorize a heterogeneous-inference claim.

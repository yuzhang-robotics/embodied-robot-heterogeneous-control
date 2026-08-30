# Phase 1 Jetson Simulation Pilot

This report records one descriptive, motion-disabled pilot on the Jetson Orin Nano. It validates the measurement protocol and runtime semantics; it is not a formal performance comparison.

## Evidence boundary

- Workload: deterministic simulated service time; no model inference.
- Repetitions: one fixed-order R0--R4 matrix.
- Physical motion and UART access: disabled.
- Permitted interpretation: descriptive timing and correctness evidence only.
- Not permitted: asynchronous superiority, hard-real-time or heterogeneous-inference claims.

## Provenance

| Field | Value |
| --- | --- |
| Session | `20260828T121142Z_phase1_jetson_pilot` |
| Source commit | `77138f2eb5db73acad24ef1fd61c03ddfbb336bc` |
| Source branch | `main` |
| Transfer archive SHA-256 | `dd7c33fa0a19b8a709cdfc7eca29a178234b39e842ad756d763d3fd33810697a` |
| Session status | `completed` |
| Independently valid | yes |

Machine-readable derived data: [`analysis.json`](analysis.json).

## Device context

| Field | Value |
| --- | --- |
| Platform | Linux-5.15.185-tegra-aarch64-with-glibc2.35 |
| Architecture | aarch64 |
| Python | 3.10.12 |
| JetPack packages | nvidia-jetpack 6.2.2+b24; nvidia-l4t-core 36.5.0-20260115194252 |
| Power mode | NV Power Mode: MAXN_SUPER; 2 |
| `jetson_clocks` snapshot | unavailable |

## Validation Gates

| Gate | Passed |
| --- | --- |
| `preflight_passed` | yes |
| `run_matrix_complete` | yes |
| `all_run_gates_passed` | yes |
| `resource_stream_valid` | yes |
| `resource_sampler_stopped` | yes |
| `every_run_has_resource_coverage` | yes |
| `stale_consumed_zero` | yes |

## Responsiveness decomposition

| Seq. | Condition | Service (s) | Ticks | Skipped | Deadline misses | Lateness p99 (ms) | Maximum gap (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `r0_idle` | 2.000 | 41 | 0 | 0 | 0.842 | 100.650 |
| 2 | `r1_inline_sync` | 2.000 | 22 | 19 | 0 | 1.455 | 2001.267 |
| 3 | `r2_threaded_sync` | 2.000 | 41 | 0 | 0 | 0.377 | 100.226 |
| 4 | `r3_async` | 2.000 | 41 | 0 | 0 | 0.365 | 100.253 |
| 5 | `r0_idle` | 5.000 | 71 | 0 | 0 | 1.006 | 100.210 |
| 6 | `r1_inline_sync` | 5.000 | 22 | 49 | 0 | 1.302 | 5001.142 |
| 7 | `r2_threaded_sync` | 5.000 | 71 | 0 | 0 | 0.471 | 100.374 |
| 8 | `r3_async` | 5.000 | 71 | 0 | 0 | 0.365 | 100.260 |
| 9 | `r0_idle` | 70.000 | 721 | 0 | 0 | 0.381 | 100.276 |
| 10 | `r1_inline_sync` | 70.000 | 22 | 699 | 0 | 1.310 | 70001.149 |
| 11 | `r2_threaded_sync` | 70.000 | 721 | 0 | 0 | 0.579 | 101.452 |
| 12 | `r3_async` | 70.000 | 721 | 0 | 0 | 0.351 | 100.342 |
| 13 | `r4_stale` | 2.000 | 41 | 0 | 0 | 0.233 | 100.081 |
| 14 | `r4_overflow` | 2.000 | 41 | 0 | 0 | 0.247 | 100.068 |

The inline condition recorded skipped releases rather than executed ticks that missed their deadline. R2 and R3 both isolate the periodic probe from the slow call; R3 additionally supplies bounded ownership, freshness and cancellation semantics.

## R3 runtime timing

| Service (s) | Queue wait (ms) | Measured service (ms) | Terminal age (ms) | Excess over configured (ms) |
| ---: | ---: | ---: | ---: | ---: |
| 2.000 | 0.628 | 2000.106 | 2001.812 | 1.812 |
| 5.000 | 0.618 | 5000.088 | 5001.336 | 1.336 |
| 70.000 | 0.608 | 70000.113 | 70001.753 | 1.753 |

## Runtime correctness

| Condition | Service (s) | Submissions | Accepted | Stale consumed | Max pending | Max result | Dispositions |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `r3_async` | 2.000 | 1 | 1 | 0 | 1 | 1 | consumed=1 |
| `r3_async` | 5.000 | 1 | 1 | 0 | 1 | 1 | consumed=1 |
| `r3_async` | 70.000 | 1 | 1 | 0 | 1 | 1 | consumed=1 |
| `r4_stale` | 2.000 | 1 | 0 | 0 | 1 | 0 | rejected_state=1 |
| `r4_overflow` | 2.000 | 4 | 1 | 0 | 1 | 1 | consumed=1, dropped_overflow=2, rejected_cancelled=1 |

## Session resources

| Metric | Mean | p95 | Maximum |
| --- | ---: | ---: | ---: |
| RAM used (MB) | 3433.157 | 3501.000 | 3555.000 |
| GR3D usage (%) | 0.450 | 0.000 | 80.000 |
| Junction temperature (C) | 52.068 | 52.593 | 52.875 |
| VDD_IN instantaneous power (mW) | 5293.238 | 6348.000 | 7697.000 |

Telemetry coverage: 1660 samples across 341.590 s; mean interval 205.901 ms, p99 interval 207.612 ms, parse errors 0.

## Data-quality observations

- `simulated_workload_only`
- `fixed_condition_order`
- `single_repetition`
- `emc_unavailable`
- `jetson_clocks_snapshot_unavailable`
- `unattributed_cpu_activity_detected`
- EMC coverage: 0/1660 samples; missing values are not interpreted as zero.
- Resource parse warnings: emc_missing=1660.

### Unattributed CPU activity screen

The screen sums per-core usage percentages and marks sustained intervals at or above 80.0%. It does not identify a process and does not exclude any sample.

| Start offset (s) | End offset (s) | Samples | CPU mean (%) | CPU max (%) | Overlapping runs |
| ---: | ---: | ---: | ---: | ---: | --- |
| 36.656 | 46.733 | 50 | 112.780 | 151.000 | 7:r2_threaded_sync, 8:r3_async, 9:r0_idle |
| 178.943 | 189.444 | 52 | 138.942 | 311.000 | 10:r1_inline_sync, 11:r2_threaded_sync |
| 195.211 | 202.620 | 37 | 131.108 | 386.000 | 11:r2_threaded_sync |
| 208.383 | 230.590 | 108 | 202.185 | 547.000 | 11:r2_threaded_sync |
| 234.090 | 243.338 | 46 | 111.370 | 181.000 | 11:r2_threaded_sync |
| 243.960 | 246.447 | 11 | 113.545 | 158.000 | 11:r2_threaded_sync |
| 247.069 | 257.617 | 45 | 97.133 | 152.000 | 11:r2_threaded_sync |
| 313.191 | 315.041 | 10 | 114.600 | 136.000 | 12:r3_async |

## Interpretation

The pilot shows that an inline slow call creates a control-proxy gap on the same scale as the configured service time. For this simulated delay, a separate thread preserved the probe schedule, while the bounded runtime added explicit queue, cancellation and freshness behavior. This result does not establish isolation for real adapters that hold the Python GIL.

Resource differences between conditions are not attributed to the runtime because the matrix has one fixed-order repetition and the CPU screen crosses condition boundaries. The simulated adapter also does not exercise a heterogeneous inference workload. A fixed-input VLM slice and a balanced repeated protocol are required before those questions can be tested.

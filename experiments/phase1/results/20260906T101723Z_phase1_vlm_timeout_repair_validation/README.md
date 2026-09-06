# Phase 1 VLM Timeout-repair Target Validation

This report reconstructs direct Jetson execution of the modified repository VLM path. It is a motion-disabled, nonformal repair validation and does not replace or reopen G6 v3.

## Provenance and integrity

| Field | Value |
| --- | --- |
| Validation | `20260906T101723Z_phase1_vlm_timeout_repair_validation` |
| Repair base commit | `52c041d2969dd8029c00e8c49f2009164c1debf9` |
| Temporary validation commit | `9bd2bcec49ad9faca972ffade515eea99fb4e9b2` |
| Collection archive SHA-256 | `f8e4df5000f64cc26f18f03b92677b4f3f061433d6bed6ccb2a73ed7efae1b78` |
| llama-server log archive SHA-256 | `64792cc3a8aaa32146ca617192390657699af7bf529b65311426270d867f11ea` |
| Validation source bundle SHA-256 | `e344b0461ac9f96d70f56f1561d8b5cd214487f5f75cd5a080b432bb8b5132e5` |
| Source files matched to this repository | 11 / 11 |
| Raw inputs, prompts, model text, logs or private paths published | no |

The source bundle was applied to a clean temporary Jetson checkout from the recorded repair base. Its validation-only commit is provenance for the target run and is not part of the project history. Every bundled source file is byte-identical to the corresponding reviewed file.

## Contract exercised

Both conditions used the fixed C100 input, spawned-process protocol `0.2.0`, deterministic temperatures and seed, the existing prompts and token bounds, `Moondream -> confirmed unload -> Qwen` order, and the repaired 60 s Qwen client boundary. Physical motion remained disabled and UART was not accessed.

## Target observations

| Condition | Outcome | Route | Confirmed unload (ms) | Qwen (ms) | Resource samples | Max Tj (C) |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `vlm_async` | `ok` | `qwen` | 715.947 | 23704.782 | 490 | 55.562 |
| `vlm_stale` | `cancel_observed` | `qwen` | 626.087 | 26854.584 | 446 | 55.750 |

Both independent slice validators returned valid results. All slice and process Gates passed, the nominal result was consumed once, the stale result was rejected before consumption, both child processes exited normally, and both unload operations recorded positive Ollama process-list absence confirmation.

The llama-server log contains 2 launches and 2 matching releases, with zero cancellation, timeout or error records, and returns to idle after the final request.

## Decision and boundary

The modified repository repair path is validated on the target and is ready for review. This single fixed-order run per condition establishes repair-path correctness only; it is not a synchronous/asynchronous performance comparison and does not establish backend preemption, timing-domain isolation, hard-real-time behavior, heterogeneous inference or condition-level resource attribution.

G6 v3 remains permanently closed and immutable. Phase 1 remains incomplete: no successor formal protocol is active, no formal collection is authorized, and the application slice remains blocked until a later reviewed formal result satisfies the completion Gates.

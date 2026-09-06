# Phase 1 VLM Timeout-repair Diagnostic

This report independently reconstructs a three-repetition, motion-disabled Jetson diagnostic of the proposed VLM request and residency contract. It is descriptive repair evidence, not formal G6 evidence.

## Source and integrity

| Field | Value |
| --- | --- |
| Diagnostic | `20260906T082627Z_phase1_vlm_timeout_diag` |
| Operator-recorded source commit | `52c041d2969dd8029c00e8c49f2009164c1debf9` |
| Transfer archive SHA-256 | `fb76b78c0d54895ddcd44682dbc1fe688451444c9682c976ff9719b7f6740500` |
| Files independently hashed | 6 |
| Formal evidence eligible | no |
| Physical motion / UART | disabled / not accessed |

Raw prompts, model text, service logs, telemetry and private paths are not included in this report or its machine-readable derivative.

## Repair contract exercised

The diagnostic used temperature `0.0` and seed `20260906` for both model requests, retained the existing prompts, models and output-token bounds, extended only the Qwen client timeout from 30 s to 60 s, and polled the Ollama process list after each Moondream stop request. The llama-server arguments were unchanged.

## Reconstructed runs

| Repetition | Moondream (ms) | Unload confirmed (ms) | Qwen client (ms) | Qwen usage |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 56900.817 | yes (507.303) | 21753.498 | 164 + 32 = 196 |
| 2 | 30673.955 | yes (541.286) | 10883.012 | 164 + 32 = 196 |
| 3 | 20169.432 | yes (477.571) | 10203.343 | 164 + 32 = 196 |

All three Qwen client calls completed below the former 30 s boundary; the observed range was 10203.343 to 21753.498 ms. The Moondream description identity and Qwen request size were stable. The Qwen output had two identities, so deterministic request construction is supported but byte-identical output is not claimed.

## Service and resource checks

The llama-server log contains 3 launches, 3 matching releases and zero cancellation, timeout or error records. It returned to idle after the final request. The first request evaluated 164 prompt tokens and all three generated 32 tokens; later prompt evaluation reused the server cache.

All 739 tegrastats lines parsed without error. RAM use ranged from 2824 to 5681 MB, maximum GR3D use was 99%, maximum Tj was 54.062 C, and maximum instantaneous VDD_IN was 17831 mW.

## Decision and boundary

The observations support the proposed deterministic request contract, 60 s Qwen timeout and fail-closed unload polling. They do not support a llama-server argument change.

The diagnostic reproduced the proposed contract in an inline harness; it did not execute the modified repository adapter. The unload process-list responses were also not retained, although each client record reports positive absence confirmation. Direct execution of the repaired path on the Jetson therefore remains required before this repair is considered validated.

G6 v3 remains permanently closed and is not rerun or replaced. Phase 1 remains incomplete during corrective work, and no formal collection, sync/async performance claim or application integration is authorized.

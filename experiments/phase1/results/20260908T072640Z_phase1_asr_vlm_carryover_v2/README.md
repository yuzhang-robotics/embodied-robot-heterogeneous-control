# Phase 1 ASR/VLM Carryover Diagnostic Result

This report publishes the completed six-session ASR/VLM carryover diagnostic.
It is a motion-disabled exploratory result, separate from the closed G6 v4
formal comparison.

## Source and integrity

| Field | Value |
| --- | --- |
| Collection | `20260908T072640Z_phase1_asr_vlm_carryover_v2` |
| Runner commit | `5fc8c4ef9572e6934095c45f0f8b499e462a5591` |
| Protocol | `phase1-asr-vlm-carryover-diagnostic-v2` |
| Protocol SHA-256 | `7382293989c684c2c796f5e9243247efd1c56fd7a66193fb6d020aabe554cd12` |
| Collection archive SHA-256 | `970e1dc38eb13e8f721c3c1bf477bc52c5fa1ade1857490b7fcabb4098a712cf` |
| Analysis archive SHA-256 | `aa5db704231322d47f4a17004f821350a5977e5cecb2cda2bac905e9213f0f2b` |
| llama-server log archive SHA-256 | `581633fc0edaa5803886ed68b173e147476cf75486cec4e8544e238792b96292` |
| Public `analysis.json` SHA-256 | `73f8cb5308317add7153a3befe590e9547285fc04651b9cf620f6bcd7b5eea06` |
| Analyzer Markdown SHA-256 | `a3184a53fd0b3f4b10a74f17937a432dbe5e6bc202d618b032cf5e3e2f8711a6` |
| Raw inputs, model text, logs or private paths published | no |

The source collection, service logs and transfer archives remain outside Git.
The tracked [`analysis.json`](analysis.json) contains only validated derived
observations.

## Design and validation

Each unit warmed ASR twice, applied an `idle`, `llm` or `vlm` interposer, and
started the measured ASR invocation 150 seconds after the second primer. An
immediate recovery ASR invocation followed. Six sessions covered every
interposer order, with Ollama and llama-server restarted before each session.

All six sessions and 18 units passed their invocation, timing and lifecycle
Gates. The independent analyzer also verified service-identity changes and
resource-trace coverage. The collection contains 14,272 resource samples; the
maximum observed Tj was 54.125 C, below the 85 C stop threshold. Physical
motion remained disabled and UART was not accessed.

## Paired observations

| Interposer | ASR duration ratio vs idle | Post-interposer resident-fraction difference | Minor-fault difference | Major-fault difference |
| --- | ---: | ---: | ---: | ---: |
| `vlm` | 7.3440 | -0.997861 | -11116.0 | +904.2 |
| `llm` | 0.9033 | +0.002139 | -3148.7 | -10.5 |

The duration ratio is the geometric mean of six within-session ratios. The
other columns are means of the corresponding within-session differences.

| Session | Interposer order | VLM / idle duration ratio | VLM - idle resident fraction | VLM - idle major faults |
| --- | --- | ---: | ---: | ---: |
| 1 | `idle, llm, vlm` | 8.5178 | -1.000000 | +1315 |
| 2 | `idle, vlm, llm` | 4.3460 | -0.987164 | +398 |
| 3 | `llm, idle, vlm` | 8.4604 | -1.000000 | +1235 |
| 4 | `llm, vlm, idle` | 7.8597 | -1.000000 | +856 |
| 5 | `vlm, idle, llm` | 8.2332 | -1.000000 | +877 |
| 6 | `vlm, llm, idle` | 7.7410 | -1.000000 | +744 |

The measured ASR invocation after VLM averaged 13,021.9 ms. Its immediate
recovery invocation averaged 1,617.1 ms, while the median duration-matched idle
measurement was 1,609.4 ms. Whisper model-file residency after VLM was zero in
all six sessions; the corresponding idle observation was complete in five
sessions and 0.987 in one. Every VLM-minus-idle major-fault difference was
positive.

The negative minor-fault difference is not treated as a performance benefit.
Together with the lost file residency and increased major faults, it reflects
a shift from resident-page access toward storage-backed fault handling.

## Interpretation

Under the fixed inputs and target Jetson configuration, the repeated sequence
is consistent with VLM-induced eviction of the warmed Whisper model file from
the Linux page cache. The next ASR invocation repopulates those pages and pays
a large cold-start cost; the immediate recovery invocation returns to the warm
latency range. The duration-matched idle condition and the LLM active control
did not reproduce this sequence.

This result identifies a plausible mechanism behind the strong ASR position
effect in G6 v4. It supports evaluating a narrowly scoped residency-management
or post-VLM rewarming repair under a new frozen comparison. It does not select
between those repairs.

## Claim boundary

The diagnostic is exploratory and configuration-specific. It does not reopen,
replace or reclassify G6 v4, does not emit a formal pass/fail decision, and does
not authorize the Phase 1 application slice or Phase 2.

## Reconstruction

Reconstruct the derived result from a private copy of the collection with:

```bash
python3 -m experiments.phase1.analyze_carryover_diagnostic \
  /path/to/20260908T072640Z_phase1_asr_vlm_carryover_v2 \
  --json-output /tmp/phase1-carryover-analysis.json \
  --markdown-output /tmp/phase1-carryover-analysis.md
```

# Phase 1 G6 v4 Formal Result

This report publishes the completed G6 v4 fixed-input synchronous/asynchronous
comparison. The collection is valid and complete, but the preregistered
intersection-union decision is **FAIL** because workload-performance
noninferiority was not established for ASR, LLM or VLM.

## Source and integrity

| Field | Value |
| --- | --- |
| Collection | `20260907T051448Z_phase1_formal_g6_v4` |
| Runner and protocol commit | `6904e5f3c366ac2847ee4eb58196a04125c5cce5` |
| Protocol | `phase1-g6-fixed-input-sync-async-v4` |
| Protocol SHA-256 | `84da36aa9b4a804ecc5692b12902321e42254f707463d1a5937e7049ffa0d054` |
| Full collection archive SHA-256 | `8632e497cae4cff14dbde2eeb252da8def8d698f6e1dbf2f8b829b20aaba9659` |
| Formal-analysis archive SHA-256 | `1b4ff7370ab2d7e2eb730f44cb5026813f917d234d3093511706d8845000bedf` |
| Public `analysis.json` SHA-256 | `003e233c6603d909e6396dea8868b7381cf9e953e526d9d3cf78cdd884c87066` |
| Analyzer Markdown SHA-256 | `5b9beb6da7f19a1f7e84488f47fe6b13e78b7153328c3408266bfdc1c4b93f0f` |

The five sessions each completed on the frozen runner and protocol commit.
Each contains five warm-ups, two idle references and 36 measured runs. The
sessions were separated by more than 30 minutes, and both model services had a
new process identity for every session. All 90 manifest-declared artifacts per
session validated; the manifest itself is the 91st file. Raw inputs, model
text, private paths, service logs and archives are not included in Git.

| Session | Session archive SHA-256 | llama-server log archive SHA-256 |
| --- | --- | --- |
| 1 | `9cf00a7350f77a7ca56460943cb00a6c3fd0d1ce973219ec36b686c8e4fcc142` | `a27fddb3d9b5426e45094f464ab05d4d4a34ed77f44f97205e997845f0d5157a` |
| 2 | `9fa04726aec76fd66756d01d9a93db520e3e13d0f0234c097c88cad510be3d91` | `d71978487c1a164c3a14c01d0cf5bd3e1e6b862b80310bcd5a1a5685e6c8e62c` |
| 3 | `93e33784cd5d695770a20ddf3e5083cddc68c295eea4891e90fb3d8dfc1400ee` | `c404af117798a03f9784b9519adbd1ae8750106bf08456dab956871bb1dd970a` |
| 4 | `80cbb862e94a4d1edcbf2dd32cf4a451772a9d5a892e5ea415cc0740ff76a038` | `f7a57b3bf4ba6bf0bdce1c4856b2548a5fe0fe3c91faccf3ff8fd74c8cc36bd3` |
| 5 | `c1dc966177358583f4846d10cd9bbae750ef2eae83054777d25a5c42ab69697e` | `eb05e4ec897f121214aafa67547815537372f5fd36f74eecc6499a10947afc19` |

## Dataset and run Gates

The analyzer validated all 180 planned measured runs: 30 within-session,
within-workload paired units for each workload. There were no replacement
attempts or post-hoc exclusions. The collection contains 36,348 resource
samples, and maximum observed Tj was 57.406 C, below the 85 C stop threshold.
Physical motion remained disabled and UART was not accessed.

Every run Gate passed. The aggregate lifecycle counts were all zero:

| Lifecycle check | Count |
| --- | ---: |
| Stale results consumed | 0 |
| Capacity violations | 0 |
| Unreaped processes | 0 |
| Unjoined threads | 0 |

Each session's bound llama-server log recorded 26 launches and 26 releases,
with no cancellation or error record.

## Confirmatory endpoints

| Workload | Async p95 gap (ms) | Paired gap difference, async - sync (95% CI, ms) | Performance ratio, async / sync (95% CI) | Decision |
| --- | ---: | ---: | ---: | --- |
| ASR | 100.511 | -10245.455 [-14555.749, -6193.006] | 1.1296 [0.4329, 2.8935] | FAIL |
| LLM | 100.396 | -2511.689 [-2936.675, -2167.848] | 1.0097 [0.8787, 1.1617] | FAIL |
| VLM | 100.664 | -87261.216 [-94644.461, -79995.975] | 1.0367 [0.9089, 1.1841] | FAIL |

All three workloads met the 300 ms asynchronous p95 bound and had a paired
gap-difference confidence interval entirely below zero. This supports the
preregistered responsiveness endpoint for the tested fixed inputs.

All three workloads failed the separate noninferiority endpoint because the
upper confidence bound of the paired geometric-mean performance ratio exceeded
`1.10`. The LLM and VLM point estimates were close to one, but their confidence
intervals were too wide to establish the frozen margin. The ASR estimate and
its confidence interval were also insufficient. This is failure to establish
noninferiority, not evidence that every asynchronous invocation was slower.

The decision rule requires every responsiveness, noninferiority and lifecycle
criterion to pass for all three workloads. Therefore the overall decision is
`FAIL` and `formal_claim_permitted` is `false`.

## Independent reconstruction

The transferred collection and analysis archives were verified before use. A
clean checkout of the recorded commit independently reconstructed the analysis
on the Jetson with Python 3.10.12 and NumPy 1.26.4. Both JSON and Markdown were
byte-identical to the reference outputs and reproduced the hashes above.

A separate Windows reconstruction under Python 3.12.4 reproduced every primary
endpoint, criterion, decision and the Markdown byte-for-byte. Five secondary
`sample_stddev` or `cv_pct` values differed at approximately `1e-17`, reflecting
last-bit floating-point representation across Python versions. Reconstruction
in the target environment resolves the canonical byte-level check; this did not
affect any endpoint or decision.

## Decision and closure

G6 v4 is a valid negative formal result. The bounded asynchronous path met the
preregistered lifecycle and 100 ms probe-responsiveness criteria for all three
fixed-input workloads, but the study did not establish the frozen 10% workload-
performance noninferiority margin. Consequently G6 is not met, Phase 1 has not
met its success Gate, and the motion-disabled application slice remains
unauthorized.

The v4 collection and decision are closed and immutable. They will not be
rerun, extended, replaced, reclassified or evaluated with a changed margin.
No v2 or v3 observation enters this analysis.

## Scope

The result applies only to the preregistered fixed inputs and the validated
Jetson configuration. It does not establish hard-real-time behavior, resource
attribution, general model performance or a heterogeneous-inference comparison.

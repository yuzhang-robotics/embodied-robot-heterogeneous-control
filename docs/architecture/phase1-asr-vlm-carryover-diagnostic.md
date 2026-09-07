# Phase 1 ASR/VLM Carryover Diagnostic

This document freezes an exploratory diagnostic prompted by the closed G6 v4
negative result. It does not reopen, extend, replace, reclassify or change that
formal result. It also does not authorize the application slice or Phase 2.

> 中文简介：G6 v4 的 180 次正式测量全部有效，运行时生命周期与 100 ms 周期探针
> 判据均通过，但同步/异步工作负载性能比受到强烈的成对位置效应影响，三个工作负载
> 都未能证明冻结的 10% 非劣效界限。本诊断用于验证 ASR 与 VLM 之间的模型驻留干扰，
> 不修改或重开 v4，也不产生新的正式通过/失败判定。

The machine-readable contract is
[`phase1-asr-vlm-carryover-v1.json`](../../experiments/phase1/diagnostic/phase1-asr-vlm-carryover-v1.json),
protocol ID `phase1-asr-vlm-carryover-diagnostic-v1`, SHA-256
`7b12b83d8a08699107e4776ba40a43ce86dafaceb2bffaf8d785d224301092ee`.

## Motivation

Post-result exploration found that pair position explained substantially more
log-duration variation than sync/async condition. First-position and
second-position geometric means were 15,068.7 and 1,634.5 ms for ASR, 97.08 and
70.95 ms/token for LLM, and 98,204.3 and 76,716.5 ms for VLM. The condition-only
log-metric R-squared values were 0.002, 0.001 and 0.009; adding pair position
raised them to 0.704, 0.591 and 0.443.

The ASR association is especially narrow. Twenty-five first-position ASR runs
with an intervening VLM were cold, averaging 23,881 ms. The five first-position
runs separated from the preceding ASR pair only by LLM remained warm, averaging
1,569 ms. Cold runs averaged about 3.4% GPU activity over the long run window,
whereas warm runs averaged about 21--22%; both groups reached a 99% observed
peak. Phase 0 also showed that explicitly warmed ASR remained near 1,564 ms
after 66--71 seconds of idle time. Idle duration alone therefore does not
explain the cold regime.

These observations motivate a causal diagnostic but cannot retrospectively
change G6 v4.

## Primary question and unit

The primary question is whether one VLM invocation, after ASR has been warmed,
causes more subsequent ASR latency and less Whisper model-file page residency
than a duration-matched idle interval. One LLM invocation is a secondary active
control.

Each unit is:

1. ASR primer 1;
2. ASR primer 2, which must pass all invocation Gates and complete in at most
   5,000 ms;
3. one `idle`, `llm` or `vlm` interposer;
4. padding to a fixed 150-second interval from the end of primer 2 to the start
   of measured ASR;
5. one measured ASR invocation;
6. one immediate ASR recovery invocation.

An interposer that leaves less than 250 ms before the target boundary invalidates
the session. Measured ASR cannot start early and may start no more than 250 ms
late. The 150-second interval exceeds the 122.308-second maximum VLM duration
observed in G6 v4.

## Schedule and observations

Six sessions cover all six permutations of `idle`, `llm` and `vlm`, one unit
per condition per session. Ollama and llama-server restart before every session,
and both process identities must change. Every unit independently re-primes
ASR.

Continuous `tegrastats` sampling runs at 200 ms. At four unit boundaries the
runner records `MemAvailable`, `Cached`, `SReclaimable` and Whisper model-file
resident pages using a non-touching `mmap` plus `mincore` observation. Each ASR
child is sampled through Linux `/proc` for user/system time, high-water RSS,
minor/major faults, filesystem-read bytes and context switches. Fixed-input
identities, output correctness and lifecycle Gates remain enforced.

Raw audio, images, prompts, model responses, private paths, service logs and run
archives remain outside Git.

## Analysis and claim boundary

The primary within-session contrasts are measured-ASR duration ratio `vlm / idle`,
post-interposer resident-fraction difference `vlm - idle`, and measured-ASR
fault/read differences `vlm - idle`. The same `llm - idle` contrasts are
secondary. The report exposes all units and descriptive aggregates.

No diagnostic artifact may contain a formal claim field. The result can justify
a narrowly scoped repair and a later, newly preregistered comparison, but it
cannot make v4 pass. Phase 1 closes successfully only if that new formal
comparison independently passes all frozen Gates.

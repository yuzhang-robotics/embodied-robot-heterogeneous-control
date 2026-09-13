# Phase 2 Experiments

This package implements the bounded Whisper residency-restoration study
defined in the
[Phase 2 design](../../docs/architecture/phase2-residency-restoration-design.md).

> 中文简介：本目录只承载 Phase 2 的独立实验实现。P2-D3 correctness-pilot contract、
> strict preflight、完整 runner 与 host-injected failure tests 已准备完成；尚未运行
> Jetson 数据或授权 application、UART 和物理运动。

## Current boundary

- Phase 1 protocols, results and analyzers remain frozen.
- The prefetch action reads a preflight-selected file sequentially in one owned
  spawned child.
- Returned records contain bounded lifecycle metadata, not paths or contents.
- A timeout is a retained action outcome, and the child must be reaped.
- Boundary observations reuse the validated non-touching Linux `mmap` plus
  `mincore` primitive without changing Phase 1 evidence.
- VDD_IN energy uses exact monotonic boundaries, linear boundary interpolation
  and trapezoidal integration, and fails closed on missing or gapped coverage.
- Injected control and treatment paths bind observations, process evidence,
  fully charged latency, device-wide energy and privacy checks without running
  a target workload.
- The fixed nonformal pilot contains exactly one control and one prefetch unit;
  every unit reprimes ASR, verifies VLM unload and child reaping, captures the
  common and post-ASR boundaries, and runs an immediate recovery ASR.
- Target preflight requires motion-disabled Linux ARM64, clean synchronized
  `main`, frozen input/model/service identities, telemetry and thermal readiness.
- The target pair remains unexecuted. Commissioning, machine preregistration,
  confirmatory collection and application integration remain inactive.

Run the current host-only tests from the repository root:

```bash
python3 -m unittest discover -s experiments/phase2/tests -t .
```

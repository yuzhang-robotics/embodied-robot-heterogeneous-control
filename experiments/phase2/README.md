# Phase 2 Experiments

This package implements the bounded Whisper residency-restoration study
defined in the
[Phase 2 design](../../docs/architecture/phase2-residency-restoration-design.md).

> 中文简介：本目录只承载 Phase 2 的独立实验实现。P2-D2 已建立 host-only action、
> observation、exact-window energy 和 privacy-preserving injected evidence path；尚未
> 运行 Jetson 数据或授权 application、UART 和物理运动。

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
- Jetson correctness runs, machine protocols, confirmatory collection and
  application integration belong to later reviewed work packages.

Run the current host-only tests from the repository root:

```bash
python3 -m unittest discover -s experiments/phase2/tests -t .
```

# Phase 2 Experiments

This package implements the bounded Whisper residency-restoration study
defined in the
[Phase 2 design](../../docs/architecture/phase2-residency-restoration-design.md).

> 中文简介：本目录只承载 Phase 2 的独立实验实现。当前 P2-D1 仅建立 host-only
> 顺序文件预取及其 owned-child 生命周期；尚未建立正式协议、运行 Jetson 数据或授权
> application、UART 和物理运动。

## Current boundary

- Phase 1 protocols, results and analyzers remain frozen.
- The prefetch action reads a preflight-selected file sequentially in one owned
  spawned child.
- Returned records contain bounded lifecycle metadata, not paths or contents.
- A timeout is a retained action outcome, and the child must be reaped.
- File-residency observation, resource/energy analysis, experiment runners and
  target commissioning belong to later reviewed work packages.

Run the current host-only tests from the repository root:

```bash
python3 -m unittest discover -s experiments/phase2/tests -t .
```

# Phase 2 Experiments

This package implements the bounded Whisper residency-restoration study
defined in the
[Phase 2 design](../../docs/architecture/phase2-residency-restoration-design.md).

> 中文简介：本目录只承载 Phase 2 的独立实验实现。P2-D3 Jetson correctness pair 已
> 完成并通过独立证据审查；P2-D4 的两组最小 commissioning session、严格 session 链和
> deterministic reconstruction 已完成 host-only 准备。尚未采集 commissioning 数据，
> 也未授权 confirmatory collection、application、UART 或物理运动。

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
- The P2-D3 target pair completed on `main@5ef832b`; all 30 declared artifacts,
  10 invocations, 1,624 resource samples and owned lifecycles independently
  validated.
- The treatment restored observed Whisper file residency from zero after VLM
  to full after prefetch. In this single nonformal pair, fully charged treatment
  time was 25.521 s versus 22.791 s for control; this is not a confirmatory
  performance result and does not authorize tuning.
- P2-D4 contains exactly two sessions with one adjacent pair each,
  complementary orders, attempt 1, changed model-service identities and at
  least 30 minutes of separation.
- Deterministic reconstruction verifies session and artifact inventory,
  protocol identity, ledger order, unit evidence, invocation Gates, resource
  coverage, exact-window energy and privacy before granting protocol-freeze
  readiness.
- Commissioning deliberately reuses the reviewed P2-D3 unit and invocation
  emitters, so those nested artifacts retain their correctness-pilot schema
  kinds. The enclosing commissioning session and reconstruction namespaces
  establish their nonformal commissioning authority.
- Commissioning, machine preregistration, confirmatory collection and
  application integration remain inactive until the required review and target
  evidence are complete.

The private P2-D3 archive is identified by SHA-256
`5666bd96b83124d446f9f1582318b7a852bfe6f2dc98d3107fd2ff89691d18b3`;
the complete archive remains outside Git.

Run the current host-only tests from the repository root:

```bash
python3 -m unittest discover -s experiments/phase2/tests -t .
```

Inspect the reviewed commissioning and reconstruction interfaces without
starting a target collection:

```bash
python3 -m experiments.phase2.run_commissioning_session --help
python3 -m experiments.phase2.reconstruct_commissioning --help
```

Target commissioning still requires a reviewed merge to synchronized `main`
and a separate explicit operator decision. The runner never imports the robot
application, motion planner or UART module.

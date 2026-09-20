# Phase 2 Experiments

This package implements the bounded Whisper residency-restoration study
defined in the
[Phase 2 design](../../docs/architecture/phase2-residency-restoration-design.md).

> 中文简介：本目录只承载 Phase 2 的独立实验实现。P2-D4 两组 commissioning sessions
> 与完整重建已通过 Jetson 和 Windows 独立审查；P2-D5 的冻结协议、六会话 chain、
> runner 和分层 analyzer 已完成 host-only 实现。尚未采集或授权 confirmatory data，
> 也未授权 application、UART 或物理运动。

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
- P2-D4 completed two valid nonformal sessions: 60 artifacts, 20 invocations
  and 3,255 resource samples reconstructed without failed invocation Gates.
- The [P2-D5 protocol](confirmatory-v1.json) freezes six sessions, four adjacent
  pairs per session, attempt 1, 4 MiB/20 s prefetch, 100,000 hierarchical
  bootstrap resamples and seed `20260920`.
- Confirmatory sessions reuse the same reviewed unit and invocation emitters;
  their nested records therefore retain correctness-pilot schema kinds and
  false local authority flags. Only the validated confirmatory parent session
  and complete collection analyzer establish formal interpretation authority.
- The analyzer reports study validity, operational benefit, residency
  mechanism, responsiveness and application authority separately. It retains
  treatment timeouts and refuses replacement, incomplete or tampered evidence.
- Confirmatory collection and application integration remain inactive until
  reviewed merge and a separate explicit operator decision.

The complete P2-D4 archive remains outside Git. Its
[public result](commissioning-result-v1.json) binds archive SHA-256
`5e1178cb77e2d74dfabf02a2e259241d195960ff525b88d88bf249ca8c9d014a` to
the independently reconstructed commissioning record without publishing
private paths, inputs, model text or service logs.

Run the current host-only tests from the repository root:

```bash
python3 -m unittest discover -s experiments/phase2/tests -t .
```

Inspect the confirmatory interfaces without starting a target collection:

```bash
python3 -m experiments.phase2.run_confirmatory_session --help
python3 -m experiments.phase2.analyze_confirmatory --help
```

Confirmatory collection requires a reviewed merge to synchronized `main` and a
separate explicit operator decision. The runner never imports the robot
application, motion planner or UART module.

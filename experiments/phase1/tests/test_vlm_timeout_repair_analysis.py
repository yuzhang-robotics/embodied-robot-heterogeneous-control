from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.phase1.analyze_vlm_timeout_repair import (
    EXPECTED_SOURCE_FILES,
    VALIDATION_COMMIT,
    VALIDATION_SESSION_ID,
    VLM_TIMEOUT_REPAIR_ANALYSIS_KIND,
    analyze_vlm_timeout_repair,
    main,
    render_markdown,
)
from experiments.phase1.tests.test_vlm_process_pilot_analysis import (
    make_process_session,
)
from jetson.vlm_request_contract import current_vlm_workload_contract


COLLECTION_ARCHIVE_SHA256 = "a" * 64
LOG_ARCHIVE_SHA256 = "b" * 64


def _make_repair_session(root: Path) -> Path:
    session = make_process_session(root)
    target = root / VALIDATION_SESSION_ID
    session.rename(target)
    for manifest_path in target.glob("vlm_process_*/*/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["session_id"] = VALIDATION_SESSION_ID
        manifest["environment"]["git"] = {
            "commit": VALIDATION_COMMIT,
            "branch": "main",
        }
        manifest["workload_contract"] = current_vlm_workload_contract()
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

        summary_path = manifest_path.with_name("summary.json")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        adapter = summary["adapter"]
        adapter["model_residency"] = {
            "unload_requested": True,
            "unload_confirmed": True,
        }
        adapter["stage_error_codes"] = {}
        summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")
    return target


def _make_source_bundle(root: Path) -> tuple[Path, Path, str]:
    repository = root / "repository"
    bundle = root / "private" / "source.tar.gz"
    bundle.parent.mkdir()
    records: dict[str, bytes] = {}
    for index, name in enumerate(EXPECTED_SOURCE_FILES):
        data = f"source {index}: {name}\n".encode("utf-8")
        path = repository / Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        records[name] = data
    with tarfile.open(bundle, "w:gz") as archive:
        for name, data in records.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mtime = 0
            archive.addfile(member, io.BytesIO(data))
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    return repository, bundle, digest


def _write_llama_log(path: Path, *, cancelled: bool = False) -> None:
    lines: list[str] = []
    for index, task in enumerate((0, 33)):
        prompt_tokens = 164 if index == 0 else 1
        lines.extend(
            [
                "1.00.000.000 I slot launch_slot_: id 0 | "
                f"task {task} | processing task",
                "1.01.000.000 I slot print_timing: id 0 | "
                f"task {task} | prompt eval time = 100.00 ms / "
                f"{prompt_tokens} tokens",
                "1.02.000.000 I slot print_timing: id 0 | "
                f"task {task} |        eval time = 100.00 ms / 32 tokens",
                "1.03.000.000 I slot print_timing: id 0 | "
                f"task {task} |       total time = 200.00 ms / 196 tokens",
            ]
        )
        if cancelled and index == 1:
            lines.append("1.03.500.000 I srv cancel task: id_task = 33")
        lines.extend(
            [
                "1.04.000.000 I slot      release: id 0 | "
                f"task {task} | stop processing",
                "1.05.000.000 I srv update_slots: all slots are idle",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class VLMTimeoutRepairAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session = _make_repair_session(self.root)
        self.repository, self.bundle, self.bundle_hash = _make_source_bundle(
            self.root
        )
        self.log = self.root / "private" / "llama-server.log"
        _write_llama_log(self.log)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def analyze(self) -> dict[str, object]:
        with patch(
            "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
            return_value=[],
        ):
            return analyze_vlm_timeout_repair(
                self.session,
                llama_log=self.log,
                source_bundle=self.bundle,
                repository_root=self.repository,
                collection_archive_sha256=COLLECTION_ARCHIVE_SHA256,
                llama_log_archive_sha256=LOG_ARCHIVE_SHA256,
                source_bundle_sha256=self.bundle_hash,
            )

    def test_reconstructs_target_repair_validation(self) -> None:
        analysis = self.analyze()

        self.assertEqual(
            analysis["analysis_kind"],
            VLM_TIMEOUT_REPAIR_ANALYSIS_KIND,
        )
        self.assertTrue(analysis["validation"]["repair_contract_verified"])
        self.assertTrue(
            analysis["validation"]["source_bundle_matches_repository"]
        )
        self.assertTrue(analysis["validation"]["all_model_unloads_confirmed"])
        self.assertEqual(analysis["validation"]["llama_server"]["request_count"], 2)
        self.assertTrue(analysis["decision"]["repair_validated_on_target"])
        self.assertFalse(analysis["decision"]["phase1_complete"])
        self.assertFalse(analysis["decision"]["formal_collection_authorized"])
        serialized = json.dumps(analysis)
        self.assertNotIn(str(self.session), serialized)
        self.assertNotIn(str(self.bundle), serialized)

    def test_rendered_report_preserves_completion_boundary(self) -> None:
        report = render_markdown(self.analyze())

        self.assertIn("direct Jetson execution", report)
        self.assertIn("ready for review", report)
        self.assertIn("G6 v3 remains permanently closed", report)
        self.assertIn("Phase 1 remains incomplete", report)
        self.assertNotIn(str(self.root), report)

    def test_rejects_source_bundle_repository_difference(self) -> None:
        changed = self.repository / Path(EXPECTED_SOURCE_FILES[0])
        changed.write_text("changed\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "validated source differs"):
            self.analyze()

    def test_rejects_unconfirmed_model_unload(self) -> None:
        summary_path = next(
            self.session.glob("vlm_process_stale/*/summary.json")
        )
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["adapter"]["model_residency"]["unload_confirmed"] = False
        summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "does not confirm model unload"):
            self.analyze()

    def test_rejects_llama_server_cancellation(self) -> None:
        _write_llama_log(self.log, cancelled=True)

        with self.assertRaisesRegex(ValueError, "cancellation, timeout or error"):
            self.analyze()

    def test_cli_is_deterministic_and_keeps_private_sources_private(self) -> None:
        output = self.root / "published"
        arguments = [
            str(self.session),
            "--llama-log",
            str(self.log),
            "--source-bundle",
            str(self.bundle),
            "--repository-root",
            str(self.repository),
            "--collection-archive-sha256",
            COLLECTION_ARCHIVE_SHA256,
            "--llama-log-archive-sha256",
            LOG_ARCHIVE_SHA256,
            "--source-bundle-sha256",
            self.bundle_hash,
            "--json-output",
            str(output / "analysis.json"),
            "--markdown-output",
            str(output / "README.md"),
        ]
        with patch(
            "experiments.phase1.analyze_vlm_pilot.validate_vlm_slice_dir",
            return_value=[],
        ):
            first = main(arguments)
            first_json = (output / "analysis.json").read_text(encoding="utf-8")
            first_markdown = (output / "README.md").read_text(encoding="utf-8")
            second = main(arguments)
            second_json = (output / "analysis.json").read_text(encoding="utf-8")
            second_markdown = (output / "README.md").read_text(encoding="utf-8")

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(first_json, second_json)
        self.assertEqual(first_markdown, second_markdown)
        self.assertNotIn(str(self.session), first_json)
        self.assertNotIn(str(self.bundle), first_json)


if __name__ == "__main__":
    unittest.main()

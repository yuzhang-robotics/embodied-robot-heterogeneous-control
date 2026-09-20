"""Fail-closed session-chain checks for Phase 2 confirmatory collection."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from experiments.phase1.common.manifest import (
    command_snapshot,
    sha256_file,
    utc_now_iso,
)
from experiments.phase2.confirmatory import (
    COMMISSIONING_RESULT_SHA256,
    CONFIRMATORY_ATTEMPT,
    CONFIRMATORY_PROTOCOL_ID,
    CONFIRMATORY_PROTOCOL_SHA256,
    CONFIRMATORY_SESSION_COUNT,
    DEFAULT_COMMISSIONING_RESULT_PATH,
    DEFAULT_CONFIRMATORY_PROTOCOL_PATH,
    MINIMUM_SESSION_SEPARATION_S,
    ConfirmatoryProtocolError,
    confirmatory_conditions,
    confirmatory_protocol_errors,
    confirmatory_protocol_sha256,
    load_confirmatory_protocol,
)
from experiments.phase2.correctness import (
    DEFAULT_PROTOCOL_PATH as CORRECTNESS_PROTOCOL_PATH,
    load_protocol as load_correctness_protocol,
)
from experiments.phase2.preflight import (
    build_correctness_preflight,
    correctness_preflight_errors,
)


CONFIRMATORY_PREFLIGHT_SCHEMA_VERSION = "0.1.0"
_COLLECTION_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_phase2_confirmatory_v1$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_CHECKS = {
    "confirmatory_protocol_identity",
    "commissioning_result_identity",
    "explicit_collection_authorization",
    "target_preflight",
    "session_schedule",
    "prior_session_chain",
    "minimum_session_separation",
    "service_identity_changed",
}


class ConfirmatoryPreflightError(ValueError):
    """A confirmatory session cannot establish its frozen evidence chain."""


def _check(
    name: str, passed: bool, observed: object, requirement: str
) -> dict[str, object]:
    return {
        "name": name,
        "required": True,
        "passed": bool(passed),
        "observed": observed,
        "requirement": requirement,
    }


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _service_identity_valid(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"llama-server", "ollama"}:
        return False
    for name in ("llama-server", "ollama"):
        service = value.get(name)
        record = service if isinstance(service, Mapping) else {}
        if (
            record.get("process_count") != 1
            or _HASH_RE.fullmatch(str(record.get("process_start_identity_sha256", "")))
            is None
            or record.get("arguments_recorded") is not False
            or record.get("pid_recorded") is not False
        ):
            return False
    return True


def _service_identities_changed(previous: object, current: object) -> bool:
    if not _service_identity_valid(previous) or not _service_identity_valid(current):
        return False
    assert isinstance(previous, Mapping)
    assert isinstance(current, Mapping)
    return all(
        previous[name].get("process_start_identity_sha256")
        != current[name].get("process_start_identity_sha256")
        for name in ("llama-server", "ollama")
        if isinstance(previous[name], Mapping) and isinstance(current[name], Mapping)
    )


def _prior_summary(previous_session: Mapping[str, object] | None) -> dict[str, object]:
    if previous_session is None:
        return {
            "present": False,
            "collection_id": None,
            "session_index": None,
            "attempt": None,
            "status": None,
            "completed_at": None,
            "protocol_id": None,
            "protocol_sha256": None,
            "service_identity": None,
        }
    preflight = previous_session.get("preflight")
    preflight_record = preflight if isinstance(preflight, Mapping) else {}
    return {
        "present": True,
        "collection_id": previous_session.get("collection_id"),
        "session_index": previous_session.get("session_index"),
        "attempt": previous_session.get("attempt"),
        "status": previous_session.get("status"),
        "completed_at": previous_session.get("completed_at"),
        "protocol_id": previous_session.get("protocol_id"),
        "protocol_sha256": previous_session.get("protocol_sha256"),
        "service_identity": preflight_record.get("service_identity"),
    }


def _load_commissioning_result(path: Path) -> tuple[dict[str, object], list[str]]:
    errors: list[str] = []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, ["commissioning result could not be loaded"]
    if not isinstance(value, Mapping):
        return {}, ["commissioning result is not an object"]
    result = dict(value)
    expected = {
        "artifact_kind": "phase2_commissioning_public_result",
        "collection_id": "20260920T123241Z_phase2_commissioning_v1",
        "protocol_id": "phase2_bounded_whisper_residency_commissioning_v1",
        "protocol_sha256": (
            "49a4efdee5a0ba3f231c1b3149a431768acd080db0d2697a33139d34c536ee45"
        ),
        "commissioning_valid": True,
        "evidence_path_executable": True,
        "formal_protocol_parameters_may_be_frozen": True,
        "formal_evidence": False,
        "confirmatory_data": False,
        "confirmatory_collection_authorized": False,
        "application_slice_authorized": False,
        "raw_input_recorded": False,
        "raw_model_text_recorded": False,
        "private_paths_recorded": False,
    }
    for name, expected_value in expected.items():
        if result.get(name) != expected_value:
            errors.append(f"commissioning result {name} is invalid")
    if (
        result.get("session_count") != 2
        or result.get("pair_count") != 2
        or result.get("artifact_count") != 60
        or result.get("invocation_count") != 20
        or result.get("failed_invocation_gate_count") != 0
    ):
        errors.append("commissioning result inventory is invalid")
    return result, errors


def build_confirmatory_preflight(
    repo_root: Path | str,
    protocol: Mapping[str, object],
    *,
    collection_id: str,
    session_index: int,
    previous_session: Mapping[str, object] | None,
    asr_input: Path | str,
    vlm_input: Path | str,
    services_restarted: bool,
    dynamic_dvfs_confirmed: bool,
    collection_authorized: bool,
    protocol_path: Path | str = DEFAULT_CONFIRMATORY_PROTOCOL_PATH,
    commissioning_result_path: Path | str = DEFAULT_COMMISSIONING_RESULT_PATH,
    target_preflight: Mapping[str, object] | None = None,
    target_preflight_builder: Callable[..., dict[str, object]] = (
        build_correctness_preflight
    ),
    snapshotter: Callable[..., Mapping[str, object]] = command_snapshot,
    captured_at: str | None = None,
) -> dict[str, object]:
    if (
        not isinstance(collection_id, str)
        or _COLLECTION_RE.fullmatch(collection_id) is None
    ):
        raise ConfirmatoryPreflightError("confirmatory collection id is invalid")
    if (
        isinstance(session_index, bool)
        or not isinstance(session_index, int)
        or not 1 <= session_index <= CONFIRMATORY_SESSION_COUNT
    ):
        raise ConfirmatoryPreflightError("confirmatory session index is invalid")
    if (
        not isinstance(services_restarted, bool)
        or not isinstance(dynamic_dvfs_confirmed, bool)
        or not isinstance(collection_authorized, bool)
    ):
        raise TypeError("confirmatory confirmations must be boolean")

    root = Path(repo_root).resolve()
    protocol_file = Path(protocol_path).resolve()
    result_file = Path(commissioning_result_path).resolve()
    tracked_protocol = (
        root / "experiments" / "phase2" / DEFAULT_CONFIRMATORY_PROTOCOL_PATH.name
    ).resolve()
    tracked_result = (
        root / "experiments" / "phase2" / DEFAULT_COMMISSIONING_RESULT_PATH.name
    ).resolve()
    target = dict(
        target_preflight
        if target_preflight is not None
        else target_preflight_builder(
            root,
            load_correctness_protocol(),
            asr_input=asr_input,
            vlm_input=vlm_input,
            services_restarted=services_restarted,
            dynamic_dvfs_confirmed=dynamic_dvfs_confirmed,
            protocol_path=CORRECTNESS_PROTOCOL_PATH,
        )
    )
    target_errors = correctness_preflight_errors(target)
    base = target.get("base")
    base_record = base if isinstance(base, Mapping) else {}
    environment = base_record.get("environment")
    environment_record = environment if isinstance(environment, Mapping) else {}
    git = environment_record.get("git")
    git_record = git if isinstance(git, Mapping) else {}

    def committed_identity(path: Path, tracked: Path) -> Mapping[str, object]:
        if path != tracked:
            return {"returncode": 1, "output": "", "error_code": "path"}
        return snapshotter(
            [
                "git",
                "log",
                "-1",
                "--format=%H",
                "--",
                path.relative_to(root).as_posix(),
            ],
            cwd=root,
        )

    protocol_commit = committed_identity(protocol_file, tracked_protocol)
    result_commit = committed_identity(result_file, tracked_result)
    contract_errors = confirmatory_protocol_errors(protocol)
    digest = confirmatory_protocol_sha256(protocol) if not contract_errors else None
    result, result_errors = _load_commissioning_result(result_file)
    result_digest = sha256_file(result_file) if result_file.is_file() else None
    conditions = (
        confirmatory_conditions(protocol, session_index) if not contract_errors else ()
    )
    captured = captured_at or utc_now_iso()
    captured_time = _parse_utc(captured)
    current_services = target.get("service_identity")
    prior = _prior_summary(previous_session)
    prior_services = prior.get("service_identity")
    first_session = session_index == 1
    prior_valid = (
        previous_session is None
        if first_session
        else (
            prior.get("present") is True
            and prior.get("collection_id") == collection_id
            and prior.get("session_index") == session_index - 1
            and prior.get("attempt") == CONFIRMATORY_ATTEMPT
            and prior.get("status") == "completed"
            and prior.get("protocol_id") == CONFIRMATORY_PROTOCOL_ID
            and prior.get("protocol_sha256") == CONFIRMATORY_PROTOCOL_SHA256
            and _parse_utc(prior.get("completed_at")) is not None
            and _service_identity_valid(prior_services)
        )
    )
    prior_completed = _parse_utc(prior.get("completed_at"))
    separation_s = (
        (captured_time - prior_completed).total_seconds()
        if captured_time is not None and prior_completed is not None
        else None
    )
    separation_passed = first_session or (
        isinstance(separation_s, float) and separation_s >= MINIMUM_SESSION_SEPARATION_S
    )
    identities_changed = first_session or _service_identities_changed(
        prior_services, current_services
    )
    schedule_valid = len(conditions) == 8 and all(
        set(conditions[offset : offset + 2])
        == {"control_vlm_then_asr", "prefetch_vlm_then_asr"}
        for offset in range(0, 8, 2)
    )
    checks = [
        _check(
            "confirmatory_protocol_identity",
            not contract_errors
            and digest == CONFIRMATORY_PROTOCOL_SHA256
            and protocol_file == tracked_protocol
            and protocol_commit.get("returncode") == 0
            and _COMMIT_RE.fullmatch(str(protocol_commit.get("output", "")))
            is not None,
            {
                "id": protocol.get("protocol_id"),
                "sha256": digest,
                "errors": contract_errors,
                "tracked_path": protocol_file == tracked_protocol,
                "protocol_commit": protocol_commit.get("output"),
                "runner_commit": git_record.get("commit"),
            },
            "the reviewed confirmatory protocol is tracked and committed",
        ),
        _check(
            "commissioning_result_identity",
            not result_errors
            and result_digest == COMMISSIONING_RESULT_SHA256
            and result_file == tracked_result
            and result_commit.get("returncode") == 0
            and _COMMIT_RE.fullmatch(str(result_commit.get("output", ""))) is not None,
            {
                "sha256": result_digest,
                "errors": result_errors,
                "tracked_path": result_file == tracked_result,
                "result_commit": result_commit.get("output"),
                "collection_id": result.get("collection_id"),
            },
            "the privacy-preserving commissioning result is tracked and valid",
        ),
        _check(
            "explicit_collection_authorization",
            collection_authorized,
            {
                "operator_confirmed": collection_authorized,
                "application_slice_authorized": False,
            },
            "the operator has separately authorized confirmatory collection",
        ),
        _check(
            "target_preflight",
            not target_errors,
            {"errors": target_errors},
            "the tested motion-disabled target preflight passes unchanged",
        ),
        _check(
            "session_schedule",
            schedule_valid,
            {"session_index": session_index, "conditions": list(conditions)},
            "the session contains exactly four frozen adjacent pairs",
        ),
        _check(
            "prior_session_chain",
            prior_valid,
            {
                "required": not first_session,
                "present": prior.get("present"),
                "session_index": prior.get("session_index"),
                "status": prior.get("status"),
            },
            "the exact completed prior session exists when required",
        ),
        _check(
            "minimum_session_separation",
            separation_passed,
            {
                "required": not first_session,
                "minimum_s": MINIMUM_SESSION_SEPARATION_S,
                "observed_s": separation_s,
            },
            "successive confirmatory sessions are separated by at least 30 minutes",
        ),
        _check(
            "service_identity_changed",
            identities_changed,
            {"required": not first_session, "both_changed": identities_changed},
            "both model service identities change between sessions",
        ),
    ]
    return {
        "confirmatory_preflight_schema_version": CONFIRMATORY_PREFLIGHT_SCHEMA_VERSION,
        "captured_at": captured,
        "protocol": {
            "id": protocol.get("protocol_id"),
            "sha256": digest,
            "protocol_commit": protocol_commit.get("output"),
            "runner_commit": git_record.get("commit"),
            "path_recorded": False,
        },
        "commissioning_result": {
            "sha256": result_digest,
            "result_commit": result_commit.get("output"),
            "collection_id": result.get("collection_id"),
            "path_recorded": False,
        },
        "collection_authorization": {
            "explicit_operator_confirmation": collection_authorized,
            "application_slice_authorized": False,
        },
        "session": {
            "collection_id": collection_id,
            "session_index": session_index,
            "attempt": CONFIRMATORY_ATTEMPT,
            "conditions": list(conditions),
        },
        "prior_session": prior,
        "separation": {
            "required": not first_session,
            "minimum_s": MINIMUM_SESSION_SEPARATION_S,
            "observed_s": separation_s,
        },
        "service_identity": current_services,
        "target_preflight": target,
        "checks": checks,
        "eligible": all(check["passed"] is True for check in checks),
        "formal_evidence": True,
        "confirmatory_data": True,
        "application_slice_authorized": False,
    }


def confirmatory_preflight_errors(preflight: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    expected_fields = {
        "confirmatory_preflight_schema_version",
        "captured_at",
        "protocol",
        "commissioning_result",
        "collection_authorization",
        "session",
        "prior_session",
        "separation",
        "service_identity",
        "target_preflight",
        "checks",
        "eligible",
        "formal_evidence",
        "confirmatory_data",
        "application_slice_authorized",
    }
    if set(preflight) != expected_fields:
        errors.append("confirmatory preflight fields are invalid")
    if (
        preflight.get("confirmatory_preflight_schema_version")
        != CONFIRMATORY_PREFLIGHT_SCHEMA_VERSION
    ):
        errors.append("confirmatory preflight schema version is invalid")
    if _parse_utc(preflight.get("captured_at")) is None:
        errors.append("confirmatory preflight capture time is invalid")

    protocol = preflight.get("protocol")
    if not isinstance(protocol, Mapping) or (
        protocol.get("id") != CONFIRMATORY_PROTOCOL_ID
        or protocol.get("sha256") != CONFIRMATORY_PROTOCOL_SHA256
        or _COMMIT_RE.fullmatch(str(protocol.get("protocol_commit", ""))) is None
        or _COMMIT_RE.fullmatch(str(protocol.get("runner_commit", ""))) is None
        or protocol.get("path_recorded") is not False
    ):
        errors.append("confirmatory preflight protocol identity is invalid")
    result = preflight.get("commissioning_result")
    if not isinstance(result, Mapping) or (
        result.get("sha256") != COMMISSIONING_RESULT_SHA256
        or _COMMIT_RE.fullmatch(str(result.get("result_commit", ""))) is None
        or result.get("collection_id") != "20260920T123241Z_phase2_commissioning_v1"
        or result.get("path_recorded") is not False
    ):
        errors.append("confirmatory commissioning-result identity is invalid")
    authorization = preflight.get("collection_authorization")
    if not isinstance(authorization, Mapping) or authorization != {
        "explicit_operator_confirmation": True,
        "application_slice_authorized": False,
    }:
        errors.append("confirmatory collection authorization is missing")

    session = preflight.get("session")
    session_index = (
        session.get("session_index") if isinstance(session, Mapping) else None
    )
    if (
        not isinstance(session, Mapping)
        or _COLLECTION_RE.fullmatch(str(session.get("collection_id", ""))) is None
        or isinstance(session_index, bool)
        or not isinstance(session_index, int)
        or not 1 <= session_index <= CONFIRMATORY_SESSION_COUNT
        or session.get("attempt") != CONFIRMATORY_ATTEMPT
    ):
        errors.append("confirmatory preflight session identity is invalid")
    else:
        try:
            expected = list(
                confirmatory_conditions(load_confirmatory_protocol(), session_index)
            )
            if session.get("conditions") != expected:
                errors.append("confirmatory preflight session schedule is invalid")
        except ConfirmatoryProtocolError:
            errors.append("confirmatory preflight session schedule is invalid")

    target = preflight.get("target_preflight")
    if not isinstance(target, Mapping):
        errors.append("confirmatory target preflight is missing")
    else:
        errors.extend(
            f"target: {item}" for item in correctness_preflight_errors(target)
        )
    current_services = preflight.get("service_identity")
    if not _service_identity_valid(current_services):
        errors.append("confirmatory service identity is invalid")
    elif isinstance(target, Mapping) and current_services != target.get(
        "service_identity"
    ):
        errors.append("confirmatory and target service identities differ")

    prior = preflight.get("prior_session")
    prior_fields = {
        "present",
        "collection_id",
        "session_index",
        "attempt",
        "status",
        "completed_at",
        "protocol_id",
        "protocol_sha256",
        "service_identity",
    }
    if not isinstance(prior, Mapping) or set(prior) != prior_fields:
        errors.append("confirmatory prior-session summary is invalid")
    elif isinstance(session, Mapping) and isinstance(session_index, int):
        if session_index == 1:
            if prior.get("present") is not False or any(
                prior.get(name) is not None for name in prior_fields - {"present"}
            ):
                errors.append("first confirmatory session has an unexpected prior")
        elif (
            prior.get("present") is not True
            or prior.get("collection_id") != session.get("collection_id")
            or prior.get("session_index") != session_index - 1
            or prior.get("attempt") != CONFIRMATORY_ATTEMPT
            or prior.get("status") != "completed"
            or prior.get("protocol_id") != CONFIRMATORY_PROTOCOL_ID
            or prior.get("protocol_sha256") != CONFIRMATORY_PROTOCOL_SHA256
            or _parse_utc(prior.get("completed_at")) is None
            or not _service_identity_valid(prior.get("service_identity"))
        ):
            errors.append("confirmatory prior-session chain is invalid")

    separation = preflight.get("separation")
    if not isinstance(separation, Mapping) or set(separation) != {
        "required",
        "minimum_s",
        "observed_s",
    }:
        errors.append("confirmatory session-separation record is invalid")
    elif isinstance(session_index, int):
        observed_s = separation.get("observed_s")
        if session_index == 1:
            if (
                separation.get("required") is not False
                or separation.get("minimum_s") != MINIMUM_SESSION_SEPARATION_S
                or observed_s is not None
            ):
                errors.append("first confirmatory separation record is invalid")
        elif (
            separation.get("required") is not True
            or separation.get("minimum_s") != MINIMUM_SESSION_SEPARATION_S
            or isinstance(observed_s, bool)
            or not isinstance(observed_s, (int, float))
            or observed_s < MINIMUM_SESSION_SEPARATION_S
        ):
            errors.append("confirmatory minimum separation was not met")
        elif isinstance(prior, Mapping):
            captured_time = _parse_utc(preflight.get("captured_at"))
            prior_time = _parse_utc(prior.get("completed_at"))
            if (
                captured_time is None
                or prior_time is None
                or abs((captured_time - prior_time).total_seconds() - observed_s) > 1e-6
            ):
                errors.append("confirmatory separation duration is inconsistent")
            if not _service_identities_changed(
                prior.get("service_identity"), current_services
            ):
                errors.append("confirmatory service identities did not change")

    checks = preflight.get("checks")
    by_name: dict[str, Mapping[str, object]] = {}
    if not isinstance(checks, list):
        errors.append("confirmatory preflight checks are missing")
    else:
        for item in checks:
            if not isinstance(item, Mapping):
                errors.append("confirmatory preflight check is not an object")
                continue
            name = item.get("name")
            if not isinstance(name, str) or name in by_name:
                errors.append("confirmatory preflight check identity is invalid")
                continue
            by_name[name] = item
            if item.get("required") is not True or item.get("passed") is not True:
                errors.append(f"confirmatory preflight check failed: {name}")
    if set(by_name) != _REQUIRED_CHECKS:
        errors.append("confirmatory preflight check set is incomplete")
    if preflight.get("formal_evidence") is not True:
        errors.append("confirmatory preflight formal authority is invalid")
    if preflight.get("confirmatory_data") is not True:
        errors.append("confirmatory preflight data authority is invalid")
    if preflight.get("application_slice_authorized") is not False:
        errors.append("confirmatory preflight cannot authorize application integration")
    if preflight.get("eligible") is not (not errors):
        errors.append("confirmatory preflight eligibility is inconsistent")
    return errors

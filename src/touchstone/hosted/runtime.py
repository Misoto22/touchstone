"""Credential-isolated GitHub-hosted stage contracts and execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from touchstone.config import Config, ConfigError
from touchstone.hosted.crypto import (
    BundleManifest,
    EncryptedBundle,
    decode_state_key,
    decrypt_bundle,
    encrypt_bundle,
)
from touchstone.hosted.snapshot import compatibility, config_digest, snapshot_state
from touchstone.outcomes import ChangeState, RunOutcome, RunResult

HostedStage = Literal["prepare", "analysis", "verify", "publish", "snapshot"]
ResumeDecision = Literal["approve", "close", "reanalyze"]

_STAGES = {"prepare", "analysis", "verify", "publish", "snapshot"}
_DECISIONS = {"approve", "close", "reanalyze"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BRANCH = re.compile(r"^touchstone/[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_MODEL_CREDENTIALS = {
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
}
_PUBLISH_CREDENTIALS = {
    "TOUCHSTONE_APP_ID",
    "TOUCHSTONE_APP_PRIVATE_KEY",
}
_WRITE_TOKENS = {"GH_TOKEN", "GITHUB_TOKEN"}


class CandidateIntegrityError(ValueError):
    """A hosted credential boundary or reviewed candidate is invalid."""


@dataclass(frozen=True, slots=True)
class ResumeInput:
    candidate_id: str = ""
    decision: ResumeDecision | str = ""

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> ResumeInput:
        candidate = env.get("TOUCHSTONE_CANDIDATE_ID", "").strip()
        decision = env.get("TOUCHSTONE_DECISION", "").strip()
        if bool(candidate) != bool(decision):
            raise CandidateIntegrityError("resume candidate and decision must be supplied together")
        if not candidate:
            return cls()
        if not _IDENTIFIER.fullmatch(candidate):
            raise CandidateIntegrityError(
                "resume candidate ID is invalid or exceeds 128 characters"
            )
        if decision not in _DECISIONS:
            raise CandidateIntegrityError("resume decision must be approve, close, or reanalyze")
        return cls(candidate, decision)


@dataclass(frozen=True, slots=True)
class CandidateMetadata:
    repository: str
    loop: str
    finding_id: str
    candidate_id: str
    run_id: str
    base_sha: str
    patch_digest: str
    branch: str
    finding: dict[str, str]
    risk: str
    verdict: str
    verdict_reason: str
    escalation: str = ""
    resume_candidate_id: str = ""
    resume_decision: str = ""
    version: int = 2

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> CandidateMetadata:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError
            metadata = cls(**payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CandidateIntegrityError("candidate metadata is invalid") from exc
        metadata.validate()
        return metadata

    def validate(self) -> None:
        strings = (
            self.repository,
            self.loop,
            self.finding_id,
            self.candidate_id,
            self.run_id,
            self.base_sha,
            self.patch_digest,
            self.branch,
            self.risk,
            self.verdict,
            self.verdict_reason,
        )
        if any(not isinstance(value, str) or not value for value in strings):
            raise CandidateIntegrityError("candidate metadata has missing string fields")
        if self.version != 2:
            raise CandidateIntegrityError("candidate metadata version is unsupported")
        if not _IDENTIFIER.fullmatch(self.finding_id):
            raise CandidateIntegrityError("finding ID is invalid")
        if not _IDENTIFIER.fullmatch(self.candidate_id):
            raise CandidateIntegrityError("candidate ID is invalid")
        if not _IDENTIFIER.fullmatch(self.run_id):
            raise CandidateIntegrityError("candidate run ID is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", self.base_sha):
            raise CandidateIntegrityError("candidate base SHA is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.patch_digest):
            raise CandidateIntegrityError("candidate patch digest is invalid")
        if not _BRANCH.fullmatch(self.branch) or ".." in self.branch or "//" in self.branch:
            raise CandidateIntegrityError("candidate branch is invalid")
        if self.risk not in {"low", "medium", "high"}:
            raise CandidateIntegrityError("candidate risk is invalid")
        if self.verdict not in {"approve", "reject", "skipped"}:
            raise CandidateIntegrityError("candidate verdict is invalid")
        if not isinstance(self.finding, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.finding.items()
        ):
            raise CandidateIntegrityError("candidate finding is invalid")
        if not self.finding.get("title") or not self.finding.get("commit_subject"):
            raise CandidateIntegrityError("candidate finding identity is incomplete")
        if bool(self.resume_candidate_id) != bool(self.resume_decision):
            raise CandidateIntegrityError("candidate resume fields must be supplied together")
        if self.resume_candidate_id:
            ResumeInput(self.resume_candidate_id, self.resume_decision)
            ResumeInput.from_environment(
                {
                    "TOUCHSTONE_CANDIDATE_ID": self.resume_candidate_id,
                    "TOUCHSTONE_DECISION": self.resume_decision,
                }
            )


@dataclass(frozen=True, slots=True)
class VerificationAttestation:
    candidate_id: str
    run_id: str
    base_sha: str
    patch_digest: str
    config_digest: str
    worktree: str
    version: int = 1

    def validate(self) -> None:
        if self.version != 1:
            raise CandidateIntegrityError("verification attestation version is unsupported")
        if not _IDENTIFIER.fullmatch(self.candidate_id) or not _IDENTIFIER.fullmatch(self.run_id):
            raise CandidateIntegrityError("verification attestation identity is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", self.base_sha):
            raise CandidateIntegrityError("verification attestation base SHA is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.patch_digest):
            raise CandidateIntegrityError("verification attestation patch digest is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.config_digest):
            raise CandidateIntegrityError("verification attestation config digest is invalid")
        path = Path(self.worktree)
        if not path.is_absolute() or ".." in path.parts:
            raise CandidateIntegrityError("verification attestation worktree is invalid")

    def write(self, path: Path) -> None:
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def read(cls, path: Path) -> VerificationAttestation:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError
            attestation = cls(**payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CandidateIntegrityError("verification attestation is invalid") from exc
        attestation.validate()
        return attestation


@dataclass(frozen=True, slots=True)
class HostedOutputs:
    stage: HostedStage | str
    run_id: str
    outcome: str
    loop: str = ""
    candidate_id: str = ""
    change_state: str = ""
    reason_code: str = ""
    clean_start_reason: str = ""
    should_run: bool = False
    partial: bool = False
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, path: Path, *, env: Mapping[str, str] | None = None) -> None:
        target = path.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)

        environment = env if env is not None else os.environ
        output_path = environment.get("GITHUB_OUTPUT", "")
        if output_path:
            with Path(output_path).open("a", encoding="utf-8") as handle:
                for key, value in self._github_values().items():
                    handle.write(f"{key}={value}\n")
        summary_path = environment.get("GITHUB_STEP_SUMMARY", "")
        if summary_path:
            with Path(summary_path).open("a", encoding="utf-8") as handle:
                handle.write(f"### Touchstone {self.stage.title()}\n\n")
                handle.write(f"Outcome: `{self.outcome}`\n")
                if self.reason_code:
                    handle.write(f"\nReason: `{self.reason_code}`\n")

    def _github_values(self) -> dict[str, str]:
        values = {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "loop": self.loop,
            "candidate_id": self.candidate_id,
            "change_state": self.change_state,
            "reason_code": self.reason_code,
            "clean_start_reason": self.clean_start_reason,
            "should_run": str(self.should_run).lower(),
            "partial": str(self.partial).lower(),
        }
        if any("\n" in value or "\r" in value for value in values.values()):
            raise CandidateIntegrityError("hosted output values must be single-line")
        return values


def validate_stage_environment(stage: str, env: Mapping[str, str]) -> None:
    if stage not in _STAGES:
        raise CandidateIntegrityError("hosted stage is invalid")
    present = {name for name, value in env.items() if value}
    if stage == "prepare":
        prohibited = present & (
            _MODEL_CREDENTIALS | _PUBLISH_CREDENTIALS | {"TOUCHSTONE_STATE_KEY"}
        )
        if prohibited:
            raise CandidateIntegrityError("prepare stage received a prohibited credential")
    elif stage == "analysis":
        prohibited = present & _PUBLISH_CREDENTIALS
        if prohibited:
            raise CandidateIntegrityError("analysis stage received a publishing credential")
    elif stage in {"verify", "publish"}:
        prohibited = present & _MODEL_CREDENTIALS
        if prohibited:
            raise CandidateIntegrityError(f"{stage} stage received a model credential")
        if stage == "verify":
            prohibited = present & _PUBLISH_CREDENTIALS
            if prohibited:
                raise CandidateIntegrityError("verify stage received a publishing credential")
    else:
        prohibited = present & (_MODEL_CREDENTIALS | _PUBLISH_CREDENTIALS | _WRITE_TOKENS)
        if prohibited:
            raise CandidateIntegrityError("snapshot stage received a prohibited credential")


def verify_candidate(
    config: Config,
    bundle_path: Path,
    *,
    key: bytes,
    destination: Path,
    expected_base: str,
    expected_loop: str,
    expected_lineage: str,
) -> CandidateMetadata:
    try:
        bundle = EncryptedBundle.from_json(bundle_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CandidateIntegrityError("candidate bundle is unavailable") from exc
    manifest = bundle.manifest
    checked = compatibility(
        manifest,
        config,
        loop=expected_loop,
        lineage=expected_lineage,
    )
    if not checked.ok:
        raise CandidateIntegrityError(
            f"candidate bundle is incompatible: {checked.clean_start_reason}"
        )
    if set(manifest.files) != {"candidate.json", "candidate.patch"}:
        raise CandidateIntegrityError("candidate bundle membership is invalid")
    try:
        decrypt_bundle(bundle, key, destination)
    except (ValueError, OSError) as exc:
        raise CandidateIntegrityError("candidate bundle authentication failed") from exc
    metadata = CandidateMetadata.from_json(
        (destination / "candidate.json").read_text(encoding="utf-8")
    )
    if (
        metadata.repository != manifest.repository
        or metadata.loop != manifest.loop
        or metadata.candidate_id != manifest.lineage
        or metadata.run_id != manifest.run_id
    ):
        raise CandidateIntegrityError("candidate identity does not match its encrypted manifest")
    if metadata.base_sha != expected_base:
        raise CandidateIntegrityError("candidate base SHA is not the checked-out default branch")
    patch = destination / "candidate.patch"
    actual = f"sha256:{hashlib.sha256(patch.read_bytes()).hexdigest()}"
    if actual != metadata.patch_digest:
        raise CandidateIntegrityError("candidate patch digest does not match")
    return metadata


def run_stage(
    config: Config,
    stage: HostedStage | str,
    *,
    env: Mapping[str, str] | None = None,
) -> HostedOutputs:
    """Run one hosted stage and write its versioned machine contracts."""

    environment = dict(os.environ if env is None else env)
    validate_stage_environment(stage, environment)
    if config.execution.target != "local":
        raise ConfigError("GitHub-hosted execution requires execution.target = 'local'")
    run_id = _run_id(environment)
    root = config.repo_path / ".touchstone" / "hosted"
    handler = {
        "prepare": _prepare_stage,
        "analysis": _analysis_stage,
        "verify": _verify_stage,
        "publish": _publish_stage,
        "snapshot": _snapshot_stage,
    }.get(stage)
    if handler is None:
        raise CandidateIntegrityError("hosted stage is invalid")
    return handler(config, root, run_id, environment)


def _prepare_stage(
    config: Config,
    root: Path,
    run_id: str,
    env: Mapping[str, str],
) -> HostedOutputs:
    directory = _fresh_directory(root, "prepare")
    resume = ResumeInput.from_environment(env)
    clean_start = ""

    explicit = env.get("TOUCHSTONE_PREVIOUS_STATE_BUNDLE", "")
    if explicit:
        source = Path(explicit).expanduser().resolve()
        _copy_envelope(source, directory / "state.bundle.json")
    else:
        clean_start = _download_artifact_file(
            config,
            env,
            artifact_prefix="touchstone-state-",
            member="state.bundle.json",
            destination=directory / "state.bundle.json",
        )

    if resume.candidate_id:
        explicit_candidate = env.get("TOUCHSTONE_PREVIOUS_CANDIDATE_BUNDLE", "")
        if explicit_candidate:
            source = Path(explicit_candidate).expanduser().resolve()
            _copy_envelope(
                source,
                directory / "candidate.bundle.json",
                lineage=resume.candidate_id,
            )
        else:
            reason = _download_artifact_file(
                config,
                env,
                artifact_prefix="touchstone-candidate-",
                member="candidate.bundle.json",
                destination=directory / "candidate.bundle.json",
                lineage=resume.candidate_id,
            )
            if reason:
                raise CandidateIntegrityError(f"resume candidate artifact is unavailable: {reason}")

    output = HostedOutputs(
        stage="prepare",
        run_id=run_id,
        outcome="completed",
        candidate_id=resume.candidate_id,
        clean_start_reason=clean_start,
        should_run=True,
    )
    output.write(directory / "result.json", env=env)
    return output


def _analysis_stage(
    config: Config,
    root: Path,
    run_id: str,
    env: Mapping[str, str],
) -> HostedOutputs:
    import datetime as dt

    from touchstone.scheduling.due import DueEvaluator, DueLoop, DueSlot
    from touchstone.scheduling.store import DueStore

    directory = _fresh_directory(root, "candidate")
    key = _state_key(env)
    clean_start = _restore_state_bundle(
        config,
        root / "prepare" / "state.bundle.json",
        key,
    )
    from touchstone import runner as hosted_runner

    hosted_runner._partial_write_gate(config)
    resume = ResumeInput.from_environment(env)

    if resume.candidate_id and resume.decision in {"approve", "close"}:
        from touchstone.ledger import Ledger

        projection = Ledger(Path(config.state_dir) / "ledger.jsonl").projection(resume.candidate_id)
        if projection is None:
            raise CandidateIntegrityError("resume candidate is absent from restored state")
        source = root / "prepare" / "candidate.bundle.json"
        _copy_envelope(source, directory / "candidate.bundle.json", lineage=resume.candidate_id)
        state = _write_state_bundle(
            config,
            directory / "analysis-state.bundle.json",
            key=key,
            run_id=run_id,
            result=RunResult(
                RunOutcome.COMPLETED,
                lifecycle=ChangeState.AWAITING_HUMAN,
                candidate_id=resume.candidate_id,
            ),
        )
        del state
        output = HostedOutputs(
            stage="analysis",
            run_id=run_id,
            outcome="proposed",
            loop=projection.loop,
            candidate_id=resume.candidate_id,
            change_state=ChangeState.AWAITING_HUMAN.value,
            clean_start_reason=clean_start,
            should_run=True,
        )
        output.write(directory / "result.json", env=env)
        return output

    current = _now(env)
    store = DueStore(Path(config.state_dir) / "due.sqlite")
    if resume.candidate_id and resume.decision == "reanalyze":
        from touchstone.ledger import Ledger

        projection = Ledger(Path(config.state_dir) / "ledger.jsonl").projection(resume.candidate_id)
        if projection is None:
            raise CandidateIntegrityError("resume candidate is absent from restored state")
        due = (
            DueLoop(
                DueSlot(projection.loop, "manual", current, manual=True),
                0,
                dt.timedelta(),
                config.loop(projection.loop).priority,
            ),
        )
    else:
        due = DueEvaluator(store).evaluate(config, current)

    if not due:
        result = RunResult(RunOutcome.NO_CHANGE, reason_code="not-due")
        _write_state_bundle(
            config,
            directory / "analysis-state.bundle.json",
            key=key,
            run_id=run_id,
            result=result,
        )
        output = HostedOutputs(
            stage="analysis",
            run_id=run_id,
            outcome=result.outcome.value,
            reason_code=result.reason_code,
            clean_start_reason=clean_start,
            should_run=False,
        )
        output.write(directory / "result.json", env=env)
        return output

    selected = due[0]
    owner = f"github:{run_id}"
    claimed = store.claim(
        selected.slot,
        owner=owner,
        now=current,
        ttl=dt.timedelta(hours=2),
        missed_count=selected.missed_count,
    )
    if not claimed.acquired or claimed.claim is None:
        result = RunResult(RunOutcome.BLOCKED, reason_code=claimed.reason or "due-claimed")
        _write_state_bundle(
            config,
            directory / "analysis-state.bundle.json",
            key=key,
            run_id=run_id,
            result=result,
        )
        output = HostedOutputs(
            stage="analysis",
            run_id=run_id,
            outcome=result.outcome.value,
            loop=selected.slot.loop_id,
            reason_code=result.reason_code,
            clean_start_reason=clean_start,
        )
        output.write(directory / "result.json", env=env)
        return output

    try:
        _ensure_engine(config, env)
        result, candidate = _analyze_loop(
            config,
            loop=selected.slot.loop_id,
            run_id=run_id,
            key=key,
            destination=directory / "candidate.bundle.json",
            resume=resume,
        )
    except Exception as exc:
        result = RunResult(
            RunOutcome.FAILED,
            reason_code="hosted-analysis-error",
            detail=type(exc).__name__,
            retryable=True,
        )
        candidate = None
    _write_pending_finalization(
        directory / "pending-finalization.json",
        claim=claimed.claim,
        result=result,
        now=current,
    )
    _write_state_bundle(
        config,
        directory / "analysis-state.bundle.json",
        key=key,
        run_id=run_id,
        result=result,
    )
    outcome = "proposed" if candidate is not None else result.outcome.value
    output = HostedOutputs(
        stage="analysis",
        run_id=run_id,
        outcome=outcome,
        loop=selected.slot.loop_id,
        candidate_id=candidate.candidate_id if candidate else "",
        change_state=result.lifecycle.value if result.lifecycle else "",
        reason_code=result.reason_code,
        clean_start_reason=clean_start,
        should_run=True,
        partial=result.partial,
    )
    output.write(directory / "result.json", env=env)
    return output


def _analyze_loop(
    config: Config,
    *,
    loop: str,
    run_id: str,
    key: bytes,
    destination: Path,
    resume: ResumeInput,
) -> tuple[RunResult, CandidateMetadata | None]:
    from touchstone.ledger import candidate_id, finding_id
    from touchstone.nodes import audit, classify, review
    from touchstone.nodes.context import configure
    from touchstone.validation import prepare, validate

    context = configure(config)
    worktree = Path(config.execution_worktree)
    _remove_worktree(config, worktree)
    added = context.executor.run(
        ["git", "-C", config.execution_repo, "worktree", "add", "--detach", str(worktree), "HEAD"],
        timeout=180,
    )
    if not added.ok:
        return (
            RunResult(
                RunOutcome.BLOCKED,
                reason_code="worktree-prepare",
                detail="could not create the hosted analysis worktree",
            ),
            None,
        )
    try:
        loop_config = config.loop(loop)
        preparation = prepare(
            config,
            loop_config.targets,
            context.executor,
            repository=worktree,
        )
        if preparation.outcome == "blocked":
            return RunResult(RunOutcome.BLOCKED, reason_code="preparation-gate"), None
        state: dict[str, Any] = {
            "loop": loop,
            "worktree": str(worktree),
            "branch": "",
            "dry_run": True,
            "cost": [],
            "notes": [],
        }
        state = _merge_node_state(state, audit.run(state))
        if state.get("outcome") in {"held", "blocked"}:
            return RunResult(RunOutcome.BLOCKED, reason_code="agent-unavailable"), None
        if state.get("outcome") == "inconclusive":
            return RunResult(
                RunOutcome.FAILED,
                reason_code="contract-inconclusive",
                retryable=True,
            ), None
        if state.get("finding", {}).get("status") != "proposed":
            return RunResult(RunOutcome.NO_CHANGE), None
        state = _merge_node_state(state, classify.run(state))
        if state.get("finding", {}).get("status") != "proposed" or state.get("outcome") == "clean":
            return RunResult(RunOutcome.NO_CHANGE), None
        if state.get("risk") == "low":
            state = _merge_node_state(state, review.run(state))
            if state.get("outcome") == "inconclusive":
                return RunResult(
                    RunOutcome.FAILED,
                    reason_code="review-inconclusive",
                    retryable=True,
                ), None
        else:
            state.setdefault("verdict", "skipped")
            state.setdefault("verdict_reason", "risk requires operator review")
        validation = validate(
            config,
            loop_config.targets,
            context.executor,
            repository=worktree,
        )
        if validation.blocked:
            return RunResult(RunOutcome.BLOCKED, reason_code="validation-gate"), None
        context.executor.run(["git", "-C", str(worktree), "add", "-N", "."], timeout=60)
        patch_result = context.executor.run(
            ["git", "-C", str(worktree), "diff", "--binary", "--full-index", "HEAD"],
            timeout=180,
        )
        if not patch_result.ok or not patch_result.stdout.strip():
            return RunResult(RunOutcome.NO_CHANGE, reason_code="empty-candidate"), None
        base_result = context.executor.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout=60
        )
        base_sha = base_result.stdout.strip()
        if not base_result.ok or not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            return RunResult(RunOutcome.BLOCKED, reason_code="base-sha-unavailable"), None
        finding = {
            key: str(value)
            for key, value in state.get("finding", {}).items()
            if key in {"title", "summary", "rationale", "commit_subject"} and isinstance(value, str)
        }
        stable_finding_id = finding_id(loop, finding.get("title", "Touchstone finding"))
        with tempfile.TemporaryDirectory(prefix="touchstone-candidate-") as temporary:
            candidate_root = Path(temporary)
            patch_path = candidate_root / "candidate.patch"
            patch_path.write_text(patch_result.stdout, encoding="utf-8")
            patch_digest = f"sha256:{hashlib.sha256(patch_path.read_bytes()).hexdigest()}"
            identifier = candidate_id(stable_finding_id, base_sha, patch_digest, run_id)
            branch = f"touchstone/{stable_finding_id[:8]}-{identifier[:16]}"
            metadata = CandidateMetadata(
                repository=config.forge.slug,
                loop=loop,
                finding_id=stable_finding_id,
                candidate_id=identifier,
                run_id=run_id,
                base_sha=base_sha,
                patch_digest=patch_digest,
                branch=branch,
                finding=finding,
                risk=str(state.get("risk") or "high"),
                verdict=str(state.get("verdict") or "skipped"),
                verdict_reason=str(state.get("verdict_reason") or "review unavailable"),
                escalation=str(state.get("escalation") or ""),
                resume_candidate_id=resume.candidate_id,
                resume_decision=resume.decision,
            )
            metadata.validate()
            metadata_path = candidate_root / "candidate.json"
            metadata_path.write_text(metadata.to_json(), encoding="utf-8")
            bundle = encrypt_bundle(
                BundleManifest(
                    repository=config.forge.slug,
                    loop=loop,
                    schema_version=config.source.schema_version,
                    config_digest=config_digest(config),
                    profile_digest=_profile_digest(config),
                    lineage=identifier,
                    run_id=run_id,
                    created_at=_iso_now(),
                ),
                {"candidate.json": metadata_path, "candidate.patch": patch_path},
                key,
            )
            destination.write_text(bundle.to_json(), encoding="utf-8")
        return (
            RunResult(
                RunOutcome.COMPLETED,
                lifecycle=ChangeState.PROPOSED,
                candidate_id=identifier,
            ),
            metadata,
        )
    finally:
        _remove_worktree(config, worktree)


def _verify_stage(
    config: Config,
    root: Path,
    run_id: str,
    env: Mapping[str, str],
) -> HostedOutputs:
    from touchstone import runner
    from touchstone.nodes.context import configure

    directory = _fresh_directory(root, "verified")
    key = _state_key(env)
    clean_start = _restore_state_bundle(
        config,
        root / "candidate" / "analysis-state.bundle.json",
        key,
    )
    if clean_start:
        raise CandidateIntegrityError(
            f"verification cannot continue without exact analysis state: {clean_start}"
        )
    resume = ResumeInput.from_environment(env)
    context = configure(config)

    if resume.candidate_id and resume.decision in {"approve", "close"}:
        projection = context.ledger.projection(resume.candidate_id)
        if projection is None or projection.pr is None or not projection.head_sha:
            raise CandidateIntegrityError("resume candidate is not a parked publication")
        if resume.decision == "approve":
            runner._health_gate(config)
            runner._publication_gate(config, config.loop(projection.loop))
        (directory / "resume.json").write_text(
            json.dumps(
                {
                    "candidate_id": resume.candidate_id,
                    "decision": resume.decision,
                    "config_digest": config_digest(config),
                    "run_id": run_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        output = HostedOutputs(
            stage="verify",
            run_id=run_id,
            outcome="completed",
            candidate_id=resume.candidate_id,
            change_state=ChangeState.AWAITING_HUMAN.value,
            should_run=True,
        )
        output.write(directory / "result.json", env=env)
        return output

    expected_base = _git_head(config.repo_path)
    expected_loop, expected_lineage = _expected_candidate_identity(env)
    candidate_path = root / "candidate" / "candidate.bundle.json"
    with tempfile.TemporaryDirectory(prefix="touchstone-verify-") as temporary:
        restored = Path(temporary)
        metadata = verify_candidate(
            config,
            candidate_path,
            key=key,
            destination=restored,
            expected_base=expected_base,
            expected_loop=expected_loop,
            expected_lineage=expected_lineage,
        )
        worktree = _prepare_verified_worktree(config, metadata, restored / "candidate.patch")
    VerificationAttestation(
        candidate_id=metadata.candidate_id,
        run_id=metadata.run_id,
        base_sha=metadata.base_sha,
        patch_digest=metadata.patch_digest,
        config_digest=config_digest(config),
        worktree=str(worktree),
    ).write(directory / "verified.json")
    output = HostedOutputs(
        stage="verify",
        run_id=run_id,
        outcome="completed",
        loop=metadata.loop,
        candidate_id=metadata.candidate_id,
        change_state=ChangeState.PROPOSED.value,
        should_run=True,
    )
    output.write(directory / "result.json", env=env)
    return output


def _publish_stage(
    config: Config,
    root: Path,
    run_id: str,
    env: Mapping[str, str],
) -> HostedOutputs:
    from touchstone.ledger import Ledger
    from touchstone.lifecycle import RepositoryLifecycle, ResumeRequest
    from touchstone.nodes.context import configure

    directory = _fresh_directory(root, "publish")
    key = _state_key(env)
    state_source = root / "candidate" / "analysis-state.bundle.json"
    clean_start = _restore_state_bundle(config, state_source, key)
    if clean_start:
        raise CandidateIntegrityError(
            f"publish cannot continue without exact analysis state: {clean_start}"
        )
    resume = ResumeInput.from_environment(env)
    context = configure(config)

    if resume.candidate_id and resume.decision in {"approve", "close"}:
        verified_resume = _read_json_object(root / "verified" / "resume.json")
        if verified_resume != {
            "candidate_id": resume.candidate_id,
            "decision": resume.decision,
            "config_digest": config_digest(config),
            "run_id": run_id,
        }:
            raise CandidateIntegrityError("resume decision lacks an exact verification attestation")
        projection = context.ledger.projection(resume.candidate_id)
        if projection is None or projection.pr is None or not projection.head_sha:
            raise CandidateIntegrityError("resume candidate is not a parked publication")
        if resume.decision == "approve":
            from touchstone import runner

            runner._health_gate(config)
            runner._publication_gate(config, config.loop(projection.loop))
        lifecycle = RepositoryLifecycle(
            context.forge,
            context.ledger,
            reap_after_hours=config.forge.reap_after_hours,
            executor=context.executor,
        )
        resumed = lifecycle.resume(
            ResumeRequest(
                finding_id=resume.candidate_id,
                pr=projection.pr,
                decision=resume.decision,
                reviewed_head_sha=projection.head_sha,
                lineage=resume.candidate_id,
            )
        )
        if resumed.outcome in {"held", "failed"}:
            result = RunResult(
                RunOutcome.BLOCKED if resumed.outcome == "held" else RunOutcome.FAILED,
                reason_code="resume-verification",
                detail=resumed.detail,
            )
        else:
            lifecycle_state = {
                "awaiting_checks": ChangeState.AWAITING_CHECKS,
                "closed": ChangeState.CLOSED,
                "merged": ChangeState.MERGED,
            }.get(resumed.outcome)
            result = RunResult(
                RunOutcome.COMPLETED,
                lifecycle=lifecycle_state,
                candidate_id=resume.candidate_id,
                pr_number=resumed.pr,
            )
    else:
        expected_base = _git_head(config.repo_path)
        expected_loop, expected_lineage = _expected_candidate_identity(env)
        candidate_path = root / "candidate" / "candidate.bundle.json"
        with tempfile.TemporaryDirectory(prefix="touchstone-publish-") as temporary:
            restored = Path(temporary)
            metadata = verify_candidate(
                config,
                candidate_path,
                key=key,
                destination=restored,
                expected_base=expected_base,
                expected_loop=expected_loop,
                expected_lineage=expected_lineage,
            )
            attestation = VerificationAttestation.read(root / "verified" / "verified.json")
            expected_worktree = (Path(config.state_dir) / "publish-worktree").resolve()
            if (
                attestation.candidate_id != metadata.candidate_id
                or attestation.run_id != metadata.run_id
                or attestation.base_sha != metadata.base_sha
                or attestation.patch_digest != metadata.patch_digest
                or attestation.config_digest != config_digest(config)
                or Path(attestation.worktree).resolve() != expected_worktree
            ):
                raise CandidateIntegrityError(
                    "verification attestation does not match the candidate"
                )
            _verify_staged_worktree(config, metadata, expected_worktree)
            if metadata.resume_candidate_id and metadata.resume_decision == "reanalyze":
                previous = Ledger(Path(config.state_dir) / "ledger.jsonl").projection(
                    metadata.resume_candidate_id
                )
                if previous is None or previous.pr is None or not previous.head_sha:
                    raise CandidateIntegrityError("reanalysis source is not a parked candidate")
                lifecycle = RepositoryLifecycle(
                    context.forge,
                    context.ledger,
                    reap_after_hours=config.forge.reap_after_hours,
                    executor=context.executor,
                )
                closed = lifecycle.resume(
                    ResumeRequest(
                        finding_id=previous.finding_id,
                        pr=previous.pr,
                        decision="reanalyze",
                        reviewed_head_sha=previous.head_sha,
                        lineage=previous.finding_id,
                    )
                )
                if closed.outcome != "reanalyze":
                    raise CandidateIntegrityError(
                        "could not close the prior candidate for reanalysis"
                    )
            result = _publish_verified_candidate(config, metadata, expected_worktree)

    _write_state_bundle(
        config,
        directory / "publish-state.bundle.json",
        key=key,
        run_id=run_id,
        result=result,
    )
    output = HostedOutputs(
        stage="publish",
        run_id=run_id,
        outcome=result.outcome.value,
        loop=env.get("TOUCHSTONE_EXPECTED_LOOP", "").strip(),
        candidate_id=result.candidate_id or resume.candidate_id,
        change_state=result.lifecycle.value if result.lifecycle else "",
        reason_code=result.reason_code,
        should_run=True,
        partial=result.partial,
    )
    output.write(directory / "result.json", env=env)
    return output


def _prepare_verified_worktree(
    config: Config,
    metadata: CandidateMetadata,
    patch: Path,
) -> Path:
    from touchstone import runner
    from touchstone.nodes.context import configure
    from touchstone.validation import validate

    context = configure(config)
    worktree = (Path(config.state_dir) / "publish-worktree").resolve()
    _remove_worktree(config, worktree, delete_branch=True, branch=metadata.branch)
    added = context.executor.run(
        [
            "git",
            "-C",
            config.execution_repo,
            "worktree",
            "add",
            "-b",
            metadata.branch,
            str(worktree),
            metadata.base_sha,
        ],
        timeout=180,
    )
    if not added.ok:
        raise CandidateIntegrityError("could not create the verified publication worktree")
    try:
        applied = context.executor.run(
            ["git", "-C", str(worktree), "apply", "--index", "--binary", str(patch)],
            timeout=180,
        )
        if not applied.ok:
            raise CandidateIntegrityError("could not apply and stage the candidate patch")
        runner._health_gate(config)
        runner._publication_gate(config, config.loop(metadata.loop))
        validation = validate(
            config,
            config.loop(metadata.loop).targets,
            context.executor,
            repository=worktree,
        )
        if validation.blocked:
            raise CandidateIntegrityError("candidate failed credential-free publication validation")
        _verify_staged_worktree(config, metadata, worktree)
        return worktree
    except Exception:
        _remove_worktree(
            config,
            worktree,
            delete_branch=True,
            branch=metadata.branch,
        )
        raise


def _verify_staged_worktree(
    config: Config,
    metadata: CandidateMetadata,
    worktree: Path,
) -> None:
    from touchstone.nodes.context import configure

    if not worktree.is_dir():
        raise CandidateIntegrityError("verified publication worktree is unavailable")
    context = configure(config)
    base = context.executor.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout=60)
    if not base.ok or base.stdout.strip() != metadata.base_sha:
        raise CandidateIntegrityError("verified publication base changed")
    patch = context.executor.run(
        ["git", "-C", str(worktree), "diff", "--cached", "--binary", "--full-index", "HEAD"],
        timeout=180,
    )
    digest = f"sha256:{hashlib.sha256(patch.stdout.encode()).hexdigest()}"
    if not patch.ok or digest != metadata.patch_digest:
        raise CandidateIntegrityError("verified staged patch changed after validation")


def _publish_verified_candidate(
    config: Config,
    metadata: CandidateMetadata,
    worktree: Path,
) -> RunResult:
    from touchstone.nodes import publish
    from touchstone.nodes.context import configure

    configure(config)
    published = False
    try:
        state: dict[str, Any] = {
            "loop": metadata.loop,
            "worktree": str(worktree),
            "branch": metadata.branch,
            "finding": metadata.finding,
            "finding_id": metadata.candidate_id,
            "risk": metadata.risk,
            "verdict": metadata.verdict,
            "verdict_reason": metadata.verdict_reason,
            "escalation": metadata.escalation,
            "pre_staged": True,
        }
        payload = publish._publish_verified(state)
        legacy = str(payload.get("outcome") or "failed")
        lifecycle = {
            "awaiting_human": ChangeState.AWAITING_HUMAN,
            "awaiting_checks": ChangeState.AWAITING_CHECKS,
        }.get(legacy)
        outcome = (
            RunOutcome.COMPLETED
            if lifecycle is not None
            else RunOutcome.BLOCKED
            if legacy == "blocked"
            else RunOutcome.FAILED
        )
        partial = payload.get("partial") is True
        published = lifecycle is not None or partial
        return RunResult(
            outcome,
            lifecycle=lifecycle,
            reason_code="" if outcome == RunOutcome.COMPLETED else "publication",
            detail="; ".join(str(note) for note in payload.get("notes", [])),
            pr_number=int(payload["pr"]) if payload.get("pr") is not None else None,
            candidate_id=metadata.candidate_id,
            partial=partial,
            retryable=outcome == RunOutcome.FAILED and not published,
        )
    finally:
        _remove_worktree(config, worktree, delete_branch=not published, branch=metadata.branch)


def _snapshot_stage(
    config: Config,
    root: Path,
    run_id: str,
    env: Mapping[str, str],
) -> HostedOutputs:
    directory = _fresh_directory(root, "snapshot")
    key = _state_key(env)
    inputs = root / "inputs"
    candidates = (
        sorted(inputs.rglob("publish-state.bundle.json"), reverse=True)
        + sorted(inputs.rglob("analysis-state.bundle.json"), reverse=True)
        + [root / "publish" / "publish-state.bundle.json"]
        + [root / "candidate" / "analysis-state.bundle.json"]
    )
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        final_result = RunResult(RunOutcome.NO_CHANGE, reason_code="clean-start")
        _write_state_bundle(
            config,
            directory / "state.bundle.json",
            key=key,
            run_id=run_id,
            result=final_result,
        )
        clean_start = "no-stage-state"
    else:
        reason = _restore_state_bundle(config, source, key)
        if reason:
            raise CandidateIntegrityError(f"snapshot input is incompatible: {reason}")
        pending_candidates = [
            *sorted(inputs.rglob("pending-finalization.json"), reverse=True),
            root / "candidate" / "pending-finalization.json",
        ]
        pending_path = next((path for path in pending_candidates if path.is_file()), None)
        if pending_path is not None:
            claim, analysis_result, analyzed_at = _read_pending_finalization(pending_path)
            final_result = _hosted_final_result(env, analysis_result)
            from touchstone.scheduling.store import DueStore

            DueStore(Path(config.state_dir) / "due.sqlite").finish(
                claim,
                final_result,
                now=analyzed_at,
                snapshot=f"github:{run_id}",
            )
        else:
            final_result = _hosted_final_result(
                env,
                RunResult(RunOutcome.NO_CHANGE, reason_code="no-pending-slot"),
            )
        _write_state_bundle(
            config,
            directory / "state.bundle.json",
            key=key,
            run_id=run_id,
            result=final_result,
        )
        clean_start = ""
    output = HostedOutputs(
        stage="snapshot",
        run_id=run_id,
        outcome="completed",
        clean_start_reason=clean_start,
        should_run=True,
    )
    output.write(directory / "result.json", env=env)
    return output


def _write_pending_finalization(
    path: Path,
    *,
    claim: Any,
    result: RunResult,
    now: Any,
) -> None:
    payload = {
        "version": 1,
        "claim": {
            "slot_id": claim.slot_id,
            "owner": claim.owner,
            "expires_at": claim.expires_at.isoformat(),
            "attempt": claim.attempt,
        },
        "result": result.to_dict(),
        "analyzed_at": now.isoformat(),
    }
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _read_pending_finalization(path: Path) -> tuple[Any, RunResult, Any]:
    import datetime as dt

    from touchstone.scheduling.store import DurableClaim

    payload = _read_json_object(path)
    try:
        if payload.get("version") != 1:
            raise ValueError
        claim_raw = payload["claim"]
        result_raw = payload["result"]
        claim = DurableClaim(
            slot_id=str(claim_raw["slot_id"]),
            owner=str(claim_raw["owner"]),
            expires_at=dt.datetime.fromisoformat(str(claim_raw["expires_at"])),
            attempt=int(claim_raw["attempt"]),
        )
        analyzed_at = dt.datetime.fromisoformat(str(payload["analyzed_at"]))
        result = _run_result_from_dict(result_raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateIntegrityError("pending Due Slot finalization is invalid") from exc
    if analyzed_at.tzinfo is None or claim.expires_at.tzinfo is None:
        raise CandidateIntegrityError("pending Due Slot timestamps must be aware")
    return claim, result, analyzed_at


def _run_result_from_dict(payload: Any) -> RunResult:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise CandidateIntegrityError("pending run result is invalid")
    try:
        outcome = RunOutcome(str(payload["outcome"]))
        lifecycle_raw = payload.get("lifecycle")
        lifecycle = ChangeState(str(lifecycle_raw)) if lifecycle_raw else None
        pr_raw = payload.get("pr_number")
        return RunResult(
            outcome,
            lifecycle=lifecycle,
            reason_code=str(payload.get("reason_code", "")),
            detail=str(payload.get("detail", "")),
            pr_url=str(payload.get("pr_url", "")),
            pr_number=int(pr_raw) if pr_raw is not None else None,
            candidate_id=str(payload.get("candidate_id", "")),
            partial=payload.get("partial") is True,
            retryable=payload.get("retryable") is True,
        )
    except (TypeError, ValueError) as exc:
        raise CandidateIntegrityError("pending run result is invalid") from exc


def _hosted_final_result(env: Mapping[str, str], analysis: RunResult) -> RunResult:
    job = env.get("TOUCHSTONE_PUBLISH_JOB_RESULT", "").strip()
    if job in {"failure", "cancelled"}:
        return RunResult(
            RunOutcome.FAILED,
            reason_code=f"hosted-publish-{job}",
            candidate_id=analysis.candidate_id,
            retryable=True,
        )
    raw_outcome = env.get("TOUCHSTONE_FINAL_OUTCOME", "").strip()
    if not raw_outcome:
        return analysis
    try:
        outcome = RunOutcome(raw_outcome)
    except ValueError:
        return RunResult(
            RunOutcome.FAILED,
            reason_code="hosted-final-outcome",
            candidate_id=analysis.candidate_id,
            retryable=True,
        )
    lifecycle_raw = env.get("TOUCHSTONE_FINAL_CHANGE_STATE", "").strip()
    try:
        lifecycle = ChangeState(lifecycle_raw) if lifecycle_raw else analysis.lifecycle
    except ValueError:
        lifecycle = ChangeState.FAILED
    return RunResult(
        outcome,
        lifecycle=lifecycle,
        reason_code=env.get("TOUCHSTONE_FINAL_REASON_CODE", "").strip(),
        candidate_id=(
            env.get("TOUCHSTONE_FINAL_CANDIDATE_ID", "").strip() or analysis.candidate_id
        ),
        partial=env.get("TOUCHSTONE_FINAL_PARTIAL", "").lower() == "true",
        retryable=outcome == RunOutcome.FAILED,
    )


def _write_state_bundle(
    config: Config,
    destination: Path,
    *,
    key: bytes,
    run_id: str,
    result: RunResult,
) -> EncryptedBundle:
    plan = snapshot_state(
        config,
        result,
        loop="__repository__",
        run_id=run_id,
        created_at=_iso_now(),
    )
    bundle = encrypt_bundle(plan.manifest, plan.files, key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(bundle.to_json(), encoding="utf-8")
    return bundle


def _restore_state_bundle(
    config: Config,
    source: Path,
    key: bytes,
    *,
    destination: Path | None = None,
) -> str:
    if not source.is_file():
        return "missing-snapshot"
    try:
        bundle = EncryptedBundle.from_json(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CandidateIntegrityError("state snapshot envelope is invalid") from exc
    checked = compatibility(
        bundle.manifest,
        config,
        loop="__repository__",
        lineage=None,
    )
    if not checked.ok:
        return checked.clean_start_reason
    try:
        decrypt_bundle(bundle, key, destination or Path(config.state_dir))
    except (OSError, ValueError) as exc:
        raise CandidateIntegrityError("state snapshot authentication failed") from exc
    return ""


def _copy_envelope(source: Path, destination: Path, *, lineage: str = "") -> None:
    try:
        raw = source.read_text(encoding="utf-8")
        bundle = EncryptedBundle.from_json(raw)
    except (OSError, ValueError) as exc:
        raise CandidateIntegrityError("encrypted artifact envelope is invalid") from exc
    actual = f"sha256:{hashlib.sha256(bundle.ciphertext).hexdigest()}"
    if actual != bundle.ciphertext_digest:
        raise CandidateIntegrityError("encrypted artifact ciphertext digest is invalid")
    if lineage and bundle.manifest.lineage != lineage:
        raise CandidateIntegrityError("encrypted artifact lineage does not match")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(raw, encoding="utf-8")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateIntegrityError("verified hosted contract is unavailable") from exc
    if not isinstance(payload, dict):
        raise CandidateIntegrityError("verified hosted contract is invalid")
    return payload


def _expected_candidate_identity(env: Mapping[str, str]) -> tuple[str, str]:
    loop = env.get("TOUCHSTONE_EXPECTED_LOOP", "").strip()
    lineage = env.get("TOUCHSTONE_EXPECTED_CANDIDATE_ID", "").strip()
    if not loop or not _IDENTIFIER.fullmatch(loop):
        raise CandidateIntegrityError("expected candidate loop is missing or invalid")
    if not lineage or not _IDENTIFIER.fullmatch(lineage):
        raise CandidateIntegrityError("expected candidate lineage is missing or invalid")
    return loop, lineage


def _download_artifact_file(
    config: Config,
    env: Mapping[str, str],
    *,
    artifact_prefix: str,
    member: str,
    destination: Path,
    lineage: str = "",
) -> str:
    token = env.get("GH_TOKEN", "") or env.get("GITHUB_TOKEN", "")
    repository = env.get("GITHUB_REPOSITORY", config.forge.slug)
    if repository != config.forge.slug:
        raise CandidateIntegrityError("GitHub repository does not match touchstone.toml")
    if not token:
        return "github-token-unavailable"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "touchstone-agent",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/artifacts?per_page=100",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return "artifact-list-unavailable"
    artifacts = payload.get("artifacts", []) if isinstance(payload, dict) else []
    matching = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict)
        and str(artifact.get("name", "")).startswith(artifact_prefix)
        and not artifact.get("expired", False)
        and isinstance(artifact.get("archive_download_url"), str)
    ]
    matching.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    for artifact in matching:
        download = urllib.request.Request(artifact["archive_download_url"], headers=headers)
        try:
            with urllib.request.urlopen(download, timeout=30) as response:
                archive_bytes = response.read(100 * 1024 * 1024 + 1)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError):
            continue
        if len(archive_bytes) > 100 * 1024 * 1024:
            continue
        try:
            import io

            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                names = [name for name in archive.namelist() if Path(name).name == member]
                for name in names:
                    info = archive.getinfo(name)
                    if info.file_size > 100 * 1024 * 1024 or ".." in Path(name).parts:
                        continue
                    raw = archive.read(name).decode("utf-8")
                    bundle = EncryptedBundle.from_json(raw)
                    if lineage and bundle.manifest.lineage != lineage:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(raw, encoding="utf-8")
                    return ""
        except (UnicodeDecodeError, ValueError, zipfile.BadZipFile):
            continue
    return "artifact-not-found"


def _remove_worktree(
    config: Config,
    worktree: Path,
    *,
    delete_branch: bool = False,
    branch: str = "",
) -> None:
    from touchstone.execution.local import LocalExecutor

    executor = LocalExecutor()
    executor.run(
        ["git", "-C", config.execution_repo, "worktree", "remove", "--force", str(worktree)],
        timeout=60,
    )
    if worktree.exists():
        shutil.rmtree(worktree)
    executor.run(["git", "-C", config.execution_repo, "worktree", "prune"], timeout=60)
    if delete_branch and branch:
        executor.run(["git", "-C", config.execution_repo, "branch", "-D", branch], timeout=60)


def _merge_node_state(state: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(state)
    for key, value in update.items():
        if key in {"cost", "notes"}:
            merged[key] = [*merged.get(key, []), *value]
        else:
            merged[key] = value
    return merged


def _fresh_directory(root: Path, name: str) -> Path:
    root = root.expanduser().resolve()
    directory = (root / name).resolve()
    if not directory.is_relative_to(root):  # pragma: no cover - fixed names
        raise CandidateIntegrityError("hosted output path escapes its root")
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory


def _state_key(env: Mapping[str, str]) -> bytes:
    encoded = env.get("TOUCHSTONE_STATE_KEY", "")
    if not encoded:
        raise CandidateIntegrityError("TOUCHSTONE_STATE_KEY is required for this hosted stage")
    try:
        return decode_state_key(encoded)
    except ValueError as exc:
        raise CandidateIntegrityError(str(exc)) from exc


def _ensure_engine(config: Config, env: Mapping[str, str]) -> None:
    engine = config.engine.name
    if shutil.which(engine):
        return
    if env.get("GITHUB_ACTIONS", "").lower() != "true":
        raise ConfigError(
            f"{engine} CLI is unavailable; install it or run this stage in GitHub Actions"
        )
    npm = shutil.which("npm")
    if npm is None:
        raise ConfigError("npm is required to install the configured hosted agent runtime")
    if engine == "codex":
        package = "@openai/codex"
        version = config.actions.codex_cli_version
        version_key = "codex_cli_version"
    else:
        package = "@anthropic-ai/claude-code"
        version = config.actions.claude_code_version
        version_key = "claude_code_version"
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version):
        raise ConfigError(f"actions.{version_key} must be an exact semantic version")
    runner_temp = Path(env.get("RUNNER_TEMP", tempfile.gettempdir())).expanduser().resolve()
    prefix = runner_temp / "touchstone-agent-runtime" / f"{engine}-{version}"
    binary = prefix / "bin" / engine
    if not binary.is_file():
        import subprocess

        completed = subprocess.run(
            [
                npm,
                "install",
                "--global",
                "--prefix",
                str(prefix),
                "--no-audit",
                "--no-fund",
                f"{package}@{version}",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0 or not binary.is_file():
            raise ConfigError(f"could not install {engine} CLI {version}")
    os.environ["PATH"] = f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}"


def _git_head(repository: Path) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise CandidateIntegrityError("could not resolve the checked-out default branch SHA")
    return value


def _profile_digest(config: Config) -> str:
    metadata = getattr(config, "generated_metadata", None)
    return metadata.source_digest if metadata is not None else "v1"


def _run_id(env: Mapping[str, str]) -> str:
    raw = env.get("GITHUB_RUN_ID", "").strip() or uuid.uuid4().hex
    attempt = env.get("GITHUB_RUN_ATTEMPT", "").strip()
    value = f"{raw}-{attempt}" if attempt else raw
    normalized = re.sub(r"[^A-Za-z0-9._:-]", "-", value)[:128]
    if not _IDENTIFIER.fullmatch(normalized):
        raise CandidateIntegrityError("GitHub run identity is invalid")
    return normalized


def _now(env: Mapping[str, str]):  # type: ignore[no-untyped-def]
    import datetime as dt

    override = env.get("TOUCHSTONE_NOW", "")
    if override:
        try:
            value = dt.datetime.fromisoformat(override.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CandidateIntegrityError("TOUCHSTONE_NOW must be an ISO-8601 timestamp") from exc
        if value.tzinfo is None:
            raise CandidateIntegrityError("TOUCHSTONE_NOW must include a timezone")
        return value.astimezone(dt.UTC).replace(microsecond=0)
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _iso_now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "CandidateIntegrityError",
    "CandidateMetadata",
    "HostedOutputs",
    "ResumeInput",
    "run_stage",
    "validate_stage_environment",
    "verify_candidate",
]

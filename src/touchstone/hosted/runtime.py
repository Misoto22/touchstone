"""Credential-isolated GitHub-hosted stage contracts and execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
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

_STAGES = {"install", "prepare", "analysis", "verify", "publish", "snapshot"}
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
_PREPARED_STAGES = {"analysis", "verify"}
_AGENT_PACKAGES = {
    "codex": "@openai/codex",
    "claude": "@anthropic-ai/claude-code",
}


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
    version: int = 2

    def validate(self) -> None:
        if self.version != 2:
            raise CandidateIntegrityError("verification attestation version is unsupported")
        if not _IDENTIFIER.fullmatch(self.candidate_id) or not _IDENTIFIER.fullmatch(self.run_id):
            raise CandidateIntegrityError("verification attestation identity is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}", self.base_sha):
            raise CandidateIntegrityError("verification attestation base SHA is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.patch_digest):
            raise CandidateIntegrityError("verification attestation patch digest is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.config_digest):
            raise CandidateIntegrityError("verification attestation config digest is invalid")

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
    if stage == "install":
        prohibited = present & (
            _MODEL_CREDENTIALS | _PUBLISH_CREDENTIALS | _WRITE_TOKENS | {"TOUCHSTONE_STATE_KEY"}
        )
        if prohibited:
            raise CandidateIntegrityError("install stage received a prohibited credential")
    elif stage == "prepare":
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


@dataclass(frozen=True, slots=True)
class PreparationAttestation:
    """A non-secret record of the dependency environment a stage may reuse."""

    head_sha: str
    config_digest: str
    targets: tuple[str, ...]
    lockfiles: tuple[tuple[str, str], ...]
    directories: tuple[str, ...]
    outcome: str
    version: int = 1

    def validate(self) -> None:
        if self.version != 1:
            raise CandidateIntegrityError("preparation attestation version is unsupported")
        if not re.fullmatch(r"[0-9a-f]{40}", self.head_sha):
            raise CandidateIntegrityError("preparation attestation HEAD is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.config_digest):
            raise CandidateIntegrityError("preparation attestation config digest is invalid")
        if self.outcome not in {"completed", "blocked"}:
            raise CandidateIntegrityError("preparation attestation outcome is invalid")
        if any(not isinstance(value, str) or not value for value in self.targets):
            raise CandidateIntegrityError("preparation attestation Targets are invalid")
        for value in self.directories:
            if PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
                raise CandidateIntegrityError("preparation attestation directory escapes the repo")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "head_sha": self.head_sha,
            "config_digest": self.config_digest,
            "targets": list(self.targets),
            "lockfiles": dict(self.lockfiles),
            "directories": list(self.directories),
            "outcome": self.outcome,
        }

    def write(self, path: Path) -> None:
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def read(cls, path: Path) -> PreparationAttestation:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError
            attestation = cls(
                head_sha=str(payload["head_sha"]),
                config_digest=str(payload["config_digest"]),
                targets=tuple(str(value) for value in payload["targets"]),
                lockfiles=tuple(
                    sorted((str(key), str(value)) for key, value in payload["lockfiles"].items())
                ),
                directories=tuple(str(value) for value in payload["directories"]),
                outcome=str(payload["outcome"]),
                version=int(payload.get("version", 0)),
            )
        except (
            AttributeError,
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise CandidateIntegrityError("preparation attestation is invalid") from exc
        attestation.validate()
        return attestation


def _build_preparation_attestation(config: Config, *, outcome: str) -> PreparationAttestation:
    from touchstone.validation import preparation_directories, preparation_lockfiles

    root = config.repo_path.expanduser().resolve()
    directories = tuple(
        value for value in preparation_directories(config) if (root / value).is_dir()
    )
    return PreparationAttestation(
        head_sha=_git_head(root),
        config_digest=config_digest(config),
        targets=tuple(sorted(config.targets)),
        lockfiles=_lockfile_digests(root, preparation_lockfiles(config)),
        directories=directories,
        outcome=outcome,
    )


def _lockfile_digests(root: Path, names: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Digest every candidate lockfile, recording absence explicitly."""

    digests: list[tuple[str, str]] = []
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            digests.append((name, "absent"))
            continue
        digests.append((name, f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"))
    return tuple(sorted(digests))


def _may_prepare_inline(stage: str, env: Mapping[str, str]) -> bool:
    """Allow inline preparation only where no model or publishing credential exists."""

    if stage == "publish":
        return False
    return not any(env.get(name) for name in _MODEL_CREDENTIALS)


def _reuse_prepared_dependencies(
    config: Config,
    worktree: Path,
    *,
    stage: str,
    targets: tuple[str, ...],
    env: Mapping[str, str],
) -> str:
    """Verify the Preparation Stage attestation and reuse its exact environment.

    Returns an empty string when the worktree is ready. Any mismatch fails
    closed unless this process still runs without a model credential, in which
    case preparation is repeated here — which keeps the documented invariant
    that dependencies are only ever installed before credentials exist.
    """

    root = config.repo_path.expanduser().resolve()
    path = root / ".touchstone" / "hosted" / "install" / "preparation.json"
    reason = ""
    attestation: PreparationAttestation | None = None
    if not path.is_file():
        reason = "preparation-attestation-missing"
    else:
        try:
            attestation = PreparationAttestation.read(path)
        except CandidateIntegrityError:
            # An unreadable attestation is a mismatch, not a crash: a stage that
            # still holds no model credential can recover by preparing itself.
            reason = "preparation-attestation-invalid"
        else:
            reason = _preparation_mismatch(config, attestation, worktree, targets=targets)
    if not reason and attestation is not None:
        reason = _link_prepared_directories(root, worktree, attestation.directories)
    if not reason:
        return ""
    if not _may_prepare_inline(stage, env):
        return reason
    from touchstone.nodes.context import configure
    from touchstone.validation import prepare

    report = prepare(config, targets, configure(config).executor, repository=worktree)
    return "" if report.outcome == "completed" else "preparation-gate"


def _preparation_mismatch(
    config: Config,
    attestation: PreparationAttestation,
    worktree: Path,
    *,
    targets: tuple[str, ...],
) -> str:
    root = config.repo_path.expanduser().resolve()
    if attestation.outcome != "completed":
        return "preparation-blocked"
    if attestation.head_sha != _git_head(root):
        return "preparation-head-mismatch"
    if attestation.config_digest != config_digest(config):
        return "preparation-config-mismatch"
    covered = set(attestation.targets)
    if not set(targets or tuple(config.targets)).issubset(covered):
        return "preparation-target-mismatch"
    expected = dict(attestation.lockfiles)
    if _lockfile_digests(worktree.expanduser().resolve(), tuple(expected)) != tuple(
        sorted(expected.items())
    ):
        return "preparation-lockfile-mismatch"
    return ""


def _link_prepared_directories(
    root: Path,
    worktree: Path,
    directories: tuple[str, ...],
) -> str:
    """Point a fresh worktree at the already-prepared dependency directories."""

    destination_root = worktree.expanduser().resolve()
    for value in directories:
        source = (root / value).resolve()
        destination = (destination_root / value).resolve()
        if not source.is_relative_to(root) or not destination.is_relative_to(destination_root):
            return "preparation-path-escape"
        if not source.is_dir():
            return "preparation-directory-missing"
        if destination.exists() or destination.is_symlink():
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source, target_is_directory=True)
        except OSError:
            return "preparation-link-failed"
    return ""


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


def install_stage(
    config: Config,
    *,
    for_stage: str = "analysis",
    env: Mapping[str, str] | None = None,
) -> PreparationAttestation | None:
    """Run every credential-free setup step the named stage will later depend on.

    This is the Preparation Stage. It runs in the composite Action's own install
    step, which maps no model, state, or publishing credential at all, so the
    locked Agent CLI and the locked project dependency environment both exist
    before any secret does. The attestation it returns binds that environment to
    the exact repository HEAD, configuration, Target set, and lockfiles a later
    stage must match.
    """

    environment = dict(os.environ if env is None else env)
    validate_stage_environment("install", environment)
    if for_stage not in _STAGES or for_stage == "install":
        raise CandidateIntegrityError("hosted stage is invalid")
    if config.execution.target != "local":
        raise ConfigError("GitHub-hosted execution requires execution.target = 'local'")
    if for_stage == "analysis":
        _ensure_engine(config, environment, allow_install=True)
    if for_stage not in _PREPARED_STAGES:
        return None
    from touchstone.execution.local import LocalExecutor
    from touchstone.validation import prepare

    directory = _fresh_directory(config.repo_path / ".touchstone" / "hosted", "install")
    report = prepare(config, (), LocalExecutor(), repository=config.repo_path)
    attestation = _build_preparation_attestation(config, outcome=report.outcome)
    attestation.write(directory / "preparation.json")
    if report.outcome == "blocked":
        detail = "; ".join(
            f"{' '.join(result.argv)}: {result.reason}"
            for result in report.results
            if not result.ok
        )
        raise ConfigError(f"locked project preparation failed before credentials: {detail}")
    return attestation


def install_agent_runtime(
    config: Config,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Compatibility entry point for the analysis install step."""

    install_stage(config, for_stage="analysis", env=env)


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
            artifact_name=_state_artifact_name(config),
            member="state.bundle.json",
            destination=directory / "state.bundle.json",
            require_compatible_state=True,
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
                artifact_name=f"touchstone-candidate-{resume.candidate_id}",
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
            env=env,
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
    env: Mapping[str, str],
) -> tuple[RunResult, CandidateMetadata | None]:
    from touchstone.ledger import candidate_id, finding_id
    from touchstone.nodes import audit, classify, review
    from touchstone.nodes.context import configure
    from touchstone.validation import validate_affected

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
        # Dependencies were installed by the credential-free Preparation Stage.
        # Analysis only verifies that attestation and reuses its exact result.
        preparation = _reuse_prepared_dependencies(
            config,
            worktree,
            stage="analysis",
            targets=loop_config.targets,
            env=env,
        )
        if preparation:
            return RunResult(
                RunOutcome.BLOCKED,
                reason_code="preparation-gate",
                detail=preparation,
            ), None
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
        validation = validate_affected(
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
            _validate_resume_candidate(config, projection, context, env)
        (directory / "resume.json").write_text(
            json.dumps(
                {
                    "candidate_id": resume.candidate_id,
                    "decision": resume.decision,
                    "head_sha": projection.head_sha,
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
        worktree = _prepare_verified_worktree(
            config,
            metadata,
            restored / "candidate.patch",
            env=env,
        )
        try:
            VerificationAttestation(
                candidate_id=metadata.candidate_id,
                run_id=metadata.run_id,
                base_sha=metadata.base_sha,
                patch_digest=metadata.patch_digest,
                config_digest=config_digest(config),
            ).write(directory / "verified.json")
        finally:
            _remove_worktree(
                config,
                worktree,
                delete_branch=True,
                branch=metadata.branch,
            )
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
    expected_candidate = env.get("TOUCHSTONE_EXPECTED_CANDIDATE_ID", "").strip()

    if resume.candidate_id and resume.decision == "reanalyze" and not expected_candidate:
        projection = context.ledger.projection(resume.candidate_id)
        if projection is None or projection.pr is None or not projection.head_sha:
            raise CandidateIntegrityError("reanalysis source is not a parked candidate")
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
                decision="reanalyze",
                reviewed_head_sha=projection.head_sha,
                lineage=resume.candidate_id,
            )
        )
        if resumed.outcome != "reanalyze":
            result = RunResult(
                RunOutcome.BLOCKED if resumed.outcome == "held" else RunOutcome.FAILED,
                reason_code="resume-verification",
                detail=resumed.detail,
            )
        else:
            result = RunResult(
                RunOutcome.COMPLETED,
                lifecycle=ChangeState.CLOSED,
                candidate_id=resume.candidate_id,
                pr_number=resumed.pr,
            )
    elif resume.candidate_id and resume.decision in {"approve", "close"}:
        projection = context.ledger.projection(resume.candidate_id)
        if projection is None or projection.pr is None or not projection.head_sha:
            raise CandidateIntegrityError("resume candidate is not a parked publication")
        verified_resume = _read_json_object(root / "verified" / "resume.json")
        if verified_resume != {
            "candidate_id": resume.candidate_id,
            "decision": resume.decision,
            "head_sha": projection.head_sha,
            "config_digest": config_digest(config),
            "run_id": run_id,
        }:
            raise CandidateIntegrityError("resume decision lacks an exact verification attestation")
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
            if (
                attestation.candidate_id != metadata.candidate_id
                or attestation.run_id != metadata.run_id
                or attestation.base_sha != metadata.base_sha
                or attestation.patch_digest != metadata.patch_digest
                or attestation.config_digest != config_digest(config)
            ):
                raise CandidateIntegrityError(
                    "verification attestation does not match the candidate"
                )
            publication_worktree = _materialize_publication_worktree(
                config,
                metadata,
                restored / "candidate.patch",
            )
            if metadata.resume_candidate_id and metadata.resume_decision == "reanalyze":
                try:
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
                except Exception:
                    _remove_worktree(
                        config,
                        publication_worktree,
                        delete_branch=True,
                        branch=metadata.branch,
                    )
                    raise
            result = _publish_verified_candidate(
                config,
                metadata,
                publication_worktree,
                author=_app_bot_identity(env),
            )

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
    *,
    env: Mapping[str, str],
) -> Path:
    from touchstone import runner
    from touchstone.nodes.context import configure
    from touchstone.validation import validate_affected

    context = configure(config)
    worktree = _materialize_publication_worktree(config, metadata, patch)
    try:
        runner._health_gate(config)
        runner._publication_gate(config, config.loop(metadata.loop))
        preparation = _reuse_prepared_dependencies(
            config,
            worktree,
            stage="verify",
            targets=config.loop(metadata.loop).targets,
            env=env,
        )
        if preparation:
            raise CandidateIntegrityError(
                f"candidate cannot reuse the credential-free preparation: {preparation}"
            )
        validation = validate_affected(
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


def _validate_resume_candidate(
    config: Config,
    projection: Any,
    context: Any,
    env: Mapping[str, str],
) -> None:
    from touchstone import runner
    from touchstone.validation import validate

    pull = context.forge.pull(projection.pr)
    if (
        pull is None
        or pull.closed
        or pull.merged_at
        or pull.head_sha != projection.head_sha
        or pull.branch != projection.branch
    ):
        raise CandidateIntegrityError("parked candidate head changed before approval")
    worktree = _checkout_resume_worktree(config, projection, context, env)
    try:
        runner._health_gate(config)
        runner._publication_gate(config, config.loop(projection.loop))
        # A checked-out parked head carries no worktree modification to
        # attribute, so approval revalidates every configured Loop Target.
        report = validate(
            config,
            config.loop(projection.loop).targets,
            context.executor,
            repository=worktree,
        )
        if report.blocked:
            raise CandidateIntegrityError("parked candidate failed approval validation")
    finally:
        _remove_worktree(config, worktree)


def _checkout_resume_worktree(
    config: Config,
    projection: Any,
    context: Any,
    env: Mapping[str, str],
) -> Path:
    if not _BRANCH.fullmatch(projection.branch) or not re.fullmatch(
        r"[0-9a-f]{40}", projection.head_sha
    ):
        raise CandidateIntegrityError("parked candidate Git identity is invalid")
    worktree = (Path(config.state_dir) / "resume-verify-worktree").resolve()
    _remove_worktree(config, worktree)
    git_environment = _trusted_git_environment(env)
    fetched = context.executor.run(
        [
            "git",
            "-C",
            config.execution_repo,
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "fetch",
            "--no-tags",
            "--depth=1",
            f"https://github.com/{config.forge.slug}.git",
            f"refs/heads/{projection.branch}",
        ],
        timeout=180,
        env=git_environment,
    )
    if not fetched.ok:
        raise CandidateIntegrityError("could not fetch the exact parked candidate head")
    fetched_head = context.executor.run(
        ["git", "-C", config.execution_repo, "rev-parse", "FETCH_HEAD"],
        timeout=60,
        env=git_environment,
    )
    if not fetched_head.ok or fetched_head.stdout.strip() != projection.head_sha:
        raise CandidateIntegrityError("fetched parked candidate head does not match the ledger")
    added = context.executor.run(
        [
            "git",
            "-C",
            config.execution_repo,
            "worktree",
            "add",
            "--detach",
            str(worktree),
            projection.head_sha,
        ],
        timeout=180,
        env=git_environment,
    )
    if not added.ok:
        raise CandidateIntegrityError("could not check out the parked candidate head")
    return worktree


def _materialize_publication_worktree(
    config: Config,
    metadata: CandidateMetadata,
    patch: Path,
) -> Path:
    from touchstone.nodes.context import configure

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
    *,
    author: tuple[str, str] | None = None,
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
            "isolated_push": True,
        }
        if author is not None:
            state["author_name"], state["author_email"] = author
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


_APP_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$", re.IGNORECASE)


def _app_bot_identity(env: Mapping[str, str]) -> tuple[str, str] | None:
    """Author hosted commits as the publishing App rather than the runner.

    Without this, git synthesizes an identity from the runner's own user and
    hostname, so a published commit carries no usable provenance. Returns None
    when the workflow supplied no slug, which keeps a locally driven hosted
    stage on the project's configured author.
    """

    slug = env.get("TOUCHSTONE_APP_SLUG", "").strip()
    if not slug or not _APP_SLUG.fullmatch(slug):
        return None
    login = f"{slug}[bot]"
    account = _github_account_id(login, env)
    local = f"{account}+{login}" if account is not None else login
    return login, f"{local}@users.noreply.github.com"


def _github_account_id(login: str, env: Mapping[str, str]) -> int | None:
    """Read the bot account's numeric ID so GitHub links the commit to the App."""

    token = env.get("GH_TOKEN", "") or env.get("GITHUB_TOKEN", "")
    if not token:
        return None
    request = urllib.request.Request(
        f"https://api.github.com/users/{urllib.parse.quote(login, safe='')}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "touchstone-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None
    identifier = payload.get("id") if isinstance(payload, dict) else None
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
        return None
    return identifier


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
        + sorted(inputs.rglob("state.bundle.json"), reverse=True)
        + [root / "prepare" / "state.bundle.json"]
    )
    source = next((path for path in candidates if path.is_file()), None)
    claim: Any | None = None
    analyzed_at: Any | None = None
    if source is None:
        final_result = RunResult(RunOutcome.NO_CHANGE, reason_code="clean-start")
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
        else:
            final_result = _hosted_final_result(
                env,
                RunResult(RunOutcome.NO_CHANGE, reason_code="no-pending-slot"),
            )
        clean_start = ""
    # Reconstruct the partial marker before anything durable records this run,
    # so the Due Slot and the encrypted snapshot agree that publication is
    # unresolved rather than merely failed.
    unrecorded = _record_abrupt_publish_failure(config, root, key=key, env=env)
    final_result = unrecorded or final_result
    if claim is not None and analyzed_at is not None:
        from touchstone.scheduling.store import DueStore

        DueStore(Path(config.state_dir) / "due.sqlite").finish(
            claim,
            final_result,
            now=analyzed_at,
            snapshot=f"github:{run_id}",
        )
    _write_state_bundle(
        config,
        directory / "state.bundle.json",
        key=key,
        run_id=run_id,
        result=final_result,
    )
    output = HostedOutputs(
        stage="snapshot",
        run_id=run_id,
        outcome="completed",
        loop=env.get("TOUCHSTONE_FINAL_LOOP", "").strip(),
        candidate_id=final_result.candidate_id,
        change_state=final_result.lifecycle.value if final_result.lifecycle else "",
        reason_code=final_result.reason_code,
        clean_start_reason=clean_start,
        should_run=True,
        partial=final_result.partial,
    )
    output.write(directory / "result.json", env=env)
    return output


def _record_abrupt_publish_failure(
    config: Config,
    root: Path,
    *,
    key: bytes,
    env: Mapping[str, str],
) -> RunResult | None:
    """Keep a partial publication visible when Publish never recorded its own outcome.

    Publish can push a branch or open a pull request and then die before writing
    its state bundle. Snapshot then restores the older Analysis state, which
    knows nothing about that remote write. Reconstructing the marker here from
    the authenticated candidate artifact keeps the next run blocked and gives
    `touchstone reconcile` the exact branch to inspect.
    """

    if env.get("TOUCHSTONE_PUBLISH_JOB_RESULT", "").strip() not in {"failure", "cancelled"}:
        return None
    metadata = _authenticated_partial_candidate(config, root, key=key, env=env)
    if metadata is None:
        return None
    from touchstone.ledger import Ledger, LifecycleEvent

    ledger = Ledger(Path(config.state_dir) / "ledger.jsonl")
    if ledger.projection(metadata.candidate_id) is not None:
        # Publish recorded its own outcome before dying; that row is the truth.
        return None
    ledger.append(
        LifecycleEvent(
            finding_id=metadata.candidate_id,
            state=ChangeState.FAILED,
            title=metadata.finding.get("title") or "Touchstone finding",
            loop=metadata.loop,
            risk=metadata.risk,
            branch=metadata.branch,
            detail=(
                "the Publish stage ended without recording an outcome; "
                f"reconcile branch {metadata.branch} at base {metadata.base_sha}"
            ),
            partial=True,
        )
    )
    return RunResult(
        RunOutcome.FAILED,
        lifecycle=ChangeState.FAILED,
        reason_code="hosted-publish-unrecorded",
        detail=f"partial publication on {metadata.branch}",
        candidate_id=metadata.candidate_id,
        partial=True,
        retryable=True,
    )


def _authenticated_partial_candidate(
    config: Config,
    root: Path,
    *,
    key: bytes,
    env: Mapping[str, str],
) -> CandidateMetadata | None:
    """Decrypt the exact candidate this run published, or return nothing."""

    identifier = env.get("TOUCHSTONE_FINAL_CANDIDATE_ID", "").strip()
    loop = env.get("TOUCHSTONE_FINAL_LOOP", "").strip()
    if not _IDENTIFIER.fullmatch(identifier) or not _IDENTIFIER.fullmatch(loop):
        return None
    source = next(
        (path for path in _candidate_bundle_paths(root) if path.is_file()),
        None,
    )
    if source is None:
        return None
    try:
        bundle = EncryptedBundle.from_json(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not compatibility(bundle.manifest, config, loop=loop, lineage=identifier).ok:
        return None
    if set(bundle.manifest.files) != {"candidate.json", "candidate.patch"}:
        return None
    with tempfile.TemporaryDirectory(prefix="touchstone-partial-") as temporary:
        destination = Path(temporary)
        try:
            decrypt_bundle(bundle, key, destination)
            metadata = CandidateMetadata.from_json(
                (destination / "candidate.json").read_text(encoding="utf-8")
            )
            patch = destination / "candidate.patch"
            digest = f"sha256:{hashlib.sha256(patch.read_bytes()).hexdigest()}"
        except (CandidateIntegrityError, OSError, ValueError):
            return None
    if (
        metadata.candidate_id != identifier
        or metadata.loop != loop
        or metadata.repository != config.forge.slug
        or metadata.run_id != bundle.manifest.run_id
        or digest != metadata.patch_digest
    ):
        return None
    return metadata


def _candidate_bundle_paths(root: Path) -> tuple[Path, ...]:
    inputs = root / "inputs"
    return (
        inputs / "candidate" / "candidate.bundle.json",
        *sorted(inputs.rglob("candidate.bundle.json")),
        root / "candidate" / "candidate.bundle.json",
    )


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
    artifact_name: str = "",
    member: str,
    destination: Path,
    lineage: str = "",
    require_compatible_state: bool = False,
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
    query = {"per_page": "100"}
    if artifact_name:
        query["name"] = artifact_name
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/artifacts?"
        f"{urllib.parse.urlencode(query)}",
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
        and (
            artifact.get("name") == artifact_name
            if artifact_name
            else str(artifact.get("name", "")).startswith(artifact_prefix)
        )
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
                    if (
                        require_compatible_state
                        and not compatibility(
                            bundle.manifest,
                            config,
                            loop="__repository__",
                            lineage=None,
                        ).ok
                    ):
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(raw, encoding="utf-8")
                    return ""
        except (UnicodeDecodeError, ValueError, zipfile.BadZipFile):
            continue
    return "artifact-not-found"


def _state_artifact_name(config: Config) -> str:
    return f"touchstone-state-{config_digest(config).removeprefix('sha256:')}"


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


def _ensure_engine(
    config: Config,
    env: Mapping[str, str],
    *,
    allow_install: bool = False,
) -> None:
    engine = config.engine.name
    github_actions = env.get("GITHUB_ACTIONS", "").lower() == "true"
    if not github_actions and shutil.which(engine):
        return
    if not github_actions:
        raise ConfigError(
            f"{engine} CLI is unavailable; install it or run this stage in GitHub Actions"
        )
    npm = shutil.which("npm")
    if npm is None:
        raise ConfigError("npm is required to install the configured hosted agent runtime")
    package = _AGENT_PACKAGES[engine]
    action_path_raw = env.get("GITHUB_ACTION_PATH", "").strip()
    if not action_path_raw:
        raise ConfigError("GITHUB_ACTION_PATH is required to load the locked agent runtime")
    action_path = Path(action_path_raw).expanduser().resolve()
    runtime_source = action_path / "agent-runtime" / engine
    manifest_source = runtime_source / "package.json"
    lock_source = runtime_source / "package-lock.json"
    version = _locked_agent_version(engine, package, manifest_source, lock_source)
    runner_temp = Path(env.get("RUNNER_TEMP", tempfile.gettempdir())).expanduser().resolve()
    prefix = runner_temp / "touchstone-agent-runtime" / f"{engine}-{version}"
    binary = prefix / "node_modules" / ".bin" / engine
    import subprocess

    if not binary.is_file():
        if not allow_install:
            raise ConfigError(
                f"{engine} CLI {version} was not prepared by the secret-free install step"
            )
        if prefix.exists():
            shutil.rmtree(prefix)
        prefix.mkdir(parents=True)
        shutil.copy2(manifest_source, prefix / "package.json")
        shutil.copy2(lock_source, prefix / "package-lock.json")
        installation_environment = _agent_install_environment(env, prefix)
        completed = subprocess.run(
            [
                npm,
                "ci",
                "--ignore-scripts",
                "--include=optional",
                "--no-audit",
                "--no-fund",
            ],
            cwd=prefix,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=installation_environment,
        )
        if completed.returncode != 0 or not binary.is_file():
            raise ConfigError(f"could not install {engine} CLI {version}")
        if engine == "claude":
            node = shutil.which("node")
            postinstall = prefix / "node_modules" / package / "install.cjs"
            if node is None or not postinstall.is_file():
                raise ConfigError("the locked Claude Code postinstall is unavailable")
            prepared = subprocess.run(
                [node, str(postinstall)],
                cwd=postinstall.parent,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                env=installation_environment,
            )
            if prepared.returncode != 0:
                raise ConfigError("could not prepare the locked Claude Code native binary")
    probe_environment = _agent_install_environment(env, prefix)
    probe = subprocess.run(
        [str(binary), "--version"],
        cwd=prefix,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=probe_environment,
    )
    if probe.returncode != 0 or version not in f"{probe.stdout}\n{probe.stderr}":
        raise ConfigError(f"could not execute {engine} CLI {version} from its Action lock")
    os.environ["PATH"] = f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}"


def _locked_agent_version(
    engine: str,
    package: str,
    manifest_source: Path,
    lock_source: Path,
) -> str:
    """Derive the exact Agent CLI version the Action itself committed."""

    try:
        manifest = json.loads(manifest_source.read_text(encoding="utf-8"))
        lock = json.loads(lock_source.read_text(encoding="utf-8"))
        declared = str(manifest["dependencies"][package])
        requested = str(lock["packages"][""]["dependencies"][package])
        resolved = str(lock["packages"][f"node_modules/{package}"]["version"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"the pinned {engine} runtime lock is missing or invalid") from exc
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", declared):
        raise ConfigError(f"the pinned {engine} runtime manifest must name an exact version")
    if declared != requested or declared != resolved:
        raise ConfigError(
            f"the pinned {engine} runtime manifest ({declared}) and lock "
            f"({requested}/{resolved}) disagree for {package}"
        )
    return declared


def _agent_install_environment(env: Mapping[str, str], prefix: Path) -> dict[str, str]:
    home = prefix / "install-home"
    cache = prefix / "npm-cache"
    home.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    allowed = {
        key: value
        for key, value in env.items()
        if key in {"LANG", "LC_ALL", "LC_CTYPE", "PATH", "TEMP", "TMP", "TMPDIR", "TZ"}
    }
    allowed["HOME"] = str(home)
    allowed["npm_config_cache"] = str(cache)
    allowed["npm_config_userconfig"] = os.devnull
    return allowed


def _trusted_git_environment(env: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        key: value
        for key, value in env.items()
        if key
        in {
            "GH_TOKEN",
            "HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "PATH",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "TZ",
        }
        and value
    }
    allowed["GIT_CONFIG_NOSYSTEM"] = "1"
    allowed["GIT_TERMINAL_PROMPT"] = "0"
    return allowed


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
    "PreparationAttestation",
    "ResumeInput",
    "install_agent_runtime",
    "install_stage",
    "run_stage",
    "validate_stage_environment",
    "verify_candidate",
]

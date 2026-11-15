# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Native sandbox deployment through the existing gateway-owned worker lifecycle."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

import httpx
from openhands.sdk.tool.client_tool import ClientToolSpec
from openhands.sdk.tool.registry import get_tool_module_qualnames
from pydantic import AnyHttpUrl, SecretStr

from heartwood.core_adapter.worker_attempts import (
    WorkerAttemptError,
    WorkerAttemptIntent,
    WorkerAttemptLease,
    WorkerAttemptRecord,
    WorkerRevocationEvidence,
    WorkerRuntimeBinding,
)
from heartwood.gateway._openhands_sdk import WorkerConversationContext, prepare_openhands_sdk
from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.gateway._remote_workspace import _client_tool_contract
from heartwood.gateway._worker_events import AdmittedWorkerWorkspace, require_worker_event_hooks
from heartwood.gateway._workspace import WorkspaceInspector
from heartwood.gateway.worker_control import NativeWorkerControl, NativeWorkerForward
from heartwood.gateway.worker_deployment import OpenShellWorkerDeployment
from heartwood.gateway.worker_inputs import stage_worker_inputs
from heartwood.gateway.worker_sessions import (
    WorkerPlanFactory,
    WorkerSessionPlan,
    WorkerSessionRequest,
)
from heartwood.gateway.worker_volumes import WorkerVolumes, WorkerVolumeSettings


def native_worker_plan_factory(
    settings: OpenShellWorkerDeployment,
    project: ProjectContext,
    *,
    client_tools: Sequence[ClientToolSpec] = (),
) -> WorkerPlanFactory:
    """Select native execution explicitly, failing before any fallback or service grant."""
    specifications = tuple(_client_tool_contract(client_tools).values())
    try:
        require_worker_event_hooks()
        NativeWorkerControl(settings, project).policy()
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        raise ProjectStateError("native worker deployment is unavailable or unsupported") from error

    def prepare(request: WorkerSessionRequest) -> WorkerSessionPlan:
        if request.project != project:
            raise WorkerAttemptError("native worker deployment belongs to another project")
        return _NativeWorkerAttempt(
            settings,
            request,
            client_tools=tuple(ClientToolSpec.model_validate_json(spec) for spec in specifications),
        ).plan()

    return prepare


class _NativeWorkerAttempt:
    def __init__(
        self,
        settings: OpenShellWorkerDeployment,
        request: WorkerSessionRequest,
        *,
        client_tools: Sequence[ClientToolSpec] = (),
    ) -> None:
        self.settings = settings
        self.request = request
        self._client_tools = tuple(_client_tool_contract(client_tools).values())
        if (
            request.profile.profile_id != settings.profile_id
            or request.profile.model != settings.model
            or request.profile.policy_endpoint != settings.model_endpoint
        ):
            raise WorkerAttemptError(
                "selected model does not match the qualified native provider route"
            )
        self.control = NativeWorkerControl(settings, request.project)
        self._policy = self.control.policy()
        self._preparation = ExitStack()
        self._intent: WorkerAttemptIntent | None = None
        self._volumes: WorkerVolumes | None = None
        self._forward: NativeWorkerForward | None = None
        self._workspace: AdmittedWorkerWorkspace | None = None
        self._sandbox_id: str | None = None
        self._generation: str | None = None
        self._key = secrets.token_urlsafe(32)
        self._placeholder: str | None = None
        try:
            parent = Path(
                self._preparation.enter_context(
                    TemporaryDirectory(prefix="worker-projection-", dir=request.project.state_root)
                )
            )
            inputs = self._preparation.enter_context(
                stage_worker_inputs(
                    WorkspaceInspector(request.project),
                    settings.inputs,
                    staging_parent=parent,
                )
            )
            skills = self._preparation.enter_context(
                request.skills.stage_worker_skills(
                    project_root=request.project.root,
                    staging_parent=parent,
                )
            )
            self._sources = {"inputs": inputs, "skills": skills.root}
            self._skills = tuple(skills.agent_skills())
            self._files = {
                mount: {
                    p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in source.rglob("*")
                    if p.is_file()
                }
                for mount, source in self._sources.items()
            }
            if len(json.dumps(self._files).encode()) > 512 * 1024:
                raise WorkerAttemptError("worker projection inventory exceeds the admission limit")
            self._input_identity = hashlib.sha256(
                json.dumps(
                    {
                        "inputs": settings.inputs.sha256,
                        "skills": skills.sha256,
                        "specialists": request.specialists.fingerprint,
                        **({"client_tools": self._client_tools} if self._client_tools else {}),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        except BaseException:
            self._preparation.close()
            raise

    def plan(self) -> WorkerSessionPlan:
        return WorkerSessionPlan(
            image_digest=self.settings.image_digest,
            sandbox_policy_sha256=self.settings.sandbox_policy_sha256,
            input_manifest_sha256=self._input_identity,
            launch=self.launch,
            connect=self.connect,
            revoke=self.revoke,
            release_preparation=self._preparation.close,
        )

    def launch(self, intent: WorkerAttemptIntent) -> WorkerRuntimeBinding:
        if self._intent is not None:
            raise WorkerAttemptError("native worker creation cannot be repeated")
        self._intent = intent
        try:
            if (
                intent.project_id != self.request.project.project_id
                or intent.session_id != self.request.session_id
                or intent.input_manifest_sha256 != self._input_identity
                or intent.sandbox_policy_sha256 != self.settings.sandbox_policy_sha256
            ):
                raise WorkerAttemptError("native worker intent does not match preparation")
            self.control.connect()
            volumes = self._volumes = WorkerVolumes(
                WorkerVolumeSettings(
                    docker_cli=self.settings.docker_cli,
                    docker_host=self.settings.docker_host,
                    workspace=self.settings.workspace,
                    image_digest=self.settings.image_digest,
                ),
                intent,
            )
            try:
                volumes.stage(self._sources)
            finally:
                self._preparation.close()
            template = self.control.pb.SandboxTemplate(
                image=self.settings.image_digest,
                resources={"limits": {"cpu": self.settings.cpu, "memory": self.settings.memory}},
                labels={**volumes.labels, "heartwood.role": "worker"},
            )
            template.driver_config.update({"docker": {"mounts": volumes.mounts()}})
            spec = self.control.pb.SandboxSpec(
                template=template,
                policy=self._policy,
                providers=[self.settings.provider],
                environment={"SESSION_API_KEY": self._key},
                command=[
                    "/opt/heartwood/.venv/bin/python",
                    "-I",
                    "-B",
                    "-u",
                    "/opt/heartwood/images/worker/launch.py",
                ],
            )
            created = self.control.client.create(
                workspace=self.settings.workspace,
                name=volumes.worker_name,
                spec=spec,
                labels=volumes.labels,
            )
            self._sandbox_id = created.id
            ready = self.control.client.wait_ready(
                volumes.worker_name,
                workspace=self.settings.workspace,
                timeout_seconds=90,
            )
            self._generation = ready.status.main_process_instance_id
            self._admit_native(ready)
            volumes.inspect_worker(self._container_name())
            self._probe()
            self._forward = NativeWorkerForward(self.control, volumes.worker_name)
            endpoint = self._forward.start()
            self._wait_api(endpoint)
            self._admit_native(
                self.control.client.get(volumes.worker_name, workspace=self.settings.workspace)
            )
            return WorkerRuntimeBinding(
                sandbox_id=self._sandbox_id,
                generation=self._generation,
                endpoint=AnyHttpUrl(endpoint),
                image_digest=self.settings.image_digest,
                sandbox_policy_sha256=self.settings.sandbox_policy_sha256,
            )
        except Exception:
            self._preparation.close()
            raise WorkerAttemptError(
                "native worker launch requires outside reconciliation"
            ) from None

    def _container_name(self) -> str:
        assert self._volumes is not None
        assert self._sandbox_id is not None
        return (
            f"openshell-{self.settings.workspace}--{self._volumes.worker_name}-{self._sandbox_id}"
        )

    def _admit_native(self, observed: object) -> None:
        assert self._volumes is not None
        fields = {name: getattr(observed, name) for name in ("status", "id", "labels", "phase")}
        status = fields["status"]
        admission = status.configuration_admission
        if (
            fields["id"] != self._sandbox_id
            or dict(fields["labels"]) != self._volumes.labels
            or fields["phase"] != self.control.pb.SANDBOX_PHASE_READY
            or not self._generation
            or status.main_process_instance_id != self._generation
            or not status.configuration_activated
            or admission.state != self.control.pb.CONFIGURATION_ADMISSION_STATE_ACCEPTED
            or admission.policy_hash != self.settings.sandbox_policy_sha256
        ):
            raise WorkerAttemptError("native sandbox identity or admission changed")

    def _observe(self, runtime: WorkerRuntimeBinding) -> None:
        assert self._volumes is not None
        if (
            runtime.sandbox_id != self._sandbox_id
            or runtime.generation != self._generation
            or self._forward is None
            or not self._forward.active
        ):
            raise WorkerAttemptError("native worker forwarding or runtime changed")
        self._admit_native(
            self.control.client.get(self._volumes.worker_name, workspace=self.settings.workspace)
        )

    def _probe(self) -> None:
        assert self._volumes is not None
        program = """import errno,hashlib,json,os,sys
from pathlib import Path
from heartwood.gateway._specialists import load_specialist_catalog
files={}
for mount in ('inputs','skills'):
 root=Path('/'+mount)
 files[mount]={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
               for p in root.rglob('*') if p.is_file()}
for mount in ('inputs','skills'):
 try: (Path('/'+mount)/'write-probe').write_text('test')
 except OSError as error: assert error.errno==errno.EROFS
 else: raise AssertionError('writable projection')
assert not Path('/workspace/.heartwood').exists()
catalog=load_specialist_catalog(
 Path('/opt/heartwood/agents/verified'),
 Path('/opt/heartwood/vendor/heartwood-skills/skills'))
print(json.dumps({'files':files,'uid':os.getuid(),
 'placeholder':os.environ[sys.argv[1]],'catalog':catalog.fingerprint}))
"""
        result = self.control.client.exec(
            self._volumes.worker_name,
            [
                "/opt/heartwood/.venv/bin/python",
                "-I",
                "-B",
                "-c",
                program,
                self.settings.placeholder_env,
            ],
            workspace=self.settings.workspace,
            timeout_seconds=30,
            no_login_shell=True,
        )
        if result.exit_code != 0 or len(result.stdout) > 1024 * 1024:
            raise WorkerAttemptError("native worker projection observation failed")
        observed = json.loads(result.stdout)
        placeholder = observed.get("placeholder")
        if (
            observed.get("files") != self._files
            or observed.get("uid") != 10001
            or observed.get("catalog") != self.request.specialists.fingerprint
            or not isinstance(placeholder, str)
            or not placeholder.startswith("openshell:resolve:env:")
            or len(placeholder) > 4096
            or any(c in placeholder for c in "\0\r\n")
        ):
            raise WorkerAttemptError("native worker projections or placeholder did not match")
        self._placeholder = placeholder

    def _wait_api(self, endpoint: str) -> None:
        deadline = monotonic() + 30
        with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
            while monotonic() < deadline:
                try:
                    with client.stream(
                        "GET",
                        endpoint + "api/conversations/search",
                        headers={"X-Session-API-Key": self._key},
                    ) as response:
                        ready = response.status_code == 200
                    if ready:
                        with client.stream(
                            "GET", endpoint + "api/conversations/search"
                        ) as response:
                            if response.status_code != 401:
                                raise WorkerAttemptError(
                                    "native worker API must require authentication"
                                )
                        return
                except httpx.TransportError:
                    pass
                sleep(0.2)
        raise WorkerAttemptError("native worker API did not become ready")

    def connect(self, lease: WorkerAttemptLease) -> WorkerConversationContext:
        if lease.intent != self._intent or self._placeholder is None:
            raise WorkerAttemptError("native worker connection does not match its launch")
        lease.require_runtime_observation(self._observe)
        prepare_openhands_sdk()
        self._workspace = AdmittedWorkerWorkspace(
            host=str(lease.runtime.endpoint),
            working_dir="/workspace",
            api_key=self._key,
            attempt=lease,
            tool_modules=tuple(get_tool_module_qualnames().items()),
            client_tools=tuple(
                ClientToolSpec.model_validate_json(spec) for spec in self._client_tools
            ),
        )
        return WorkerConversationContext(
            workspace=self._workspace,
            inference_placeholder=SecretStr(self._placeholder),
            skills=self._skills,
            specialist_catalog_sha256=self.request.specialists.fingerprint,
        )

    def revoke(self, record: WorkerAttemptRecord) -> WorkerRevocationEvidence:
        if record.intent != self._intent or record.phase != "fenced":
            raise WorkerAttemptError("native revocation requires this process's fenced attempt")
        if record.runtime is not None and (
            record.runtime.sandbox_id != self._sandbox_id
            or record.runtime.generation != self._generation
        ):
            raise WorkerAttemptError("retained runtime does not match native revocation")
        self._preparation.close()
        if self._forward is not None:
            self._forward.close()
        if self._volumes is not None:
            self.control.verify_configuration()
            try:
                observed = self.control.client.get(
                    self._volumes.worker_name, workspace=self.settings.workspace
                )
            except self.control.grpc.RpcError as error:
                if error.code() != self.control.grpc.StatusCode.NOT_FOUND:
                    raise WorkerAttemptError("native worker absence was not observed") from None
            else:
                if dict(observed.labels) != self._volumes.labels or (
                    self._sandbox_id is not None and observed.id != self._sandbox_id
                ):
                    raise WorkerAttemptError("native worker identity changed before revocation")
                self._sandbox_id = observed.id
                self.control.client.delete(
                    self._volumes.worker_name, workspace=self.settings.workspace, timeout_seconds=90
                )
                self.control.client.wait_deleted(
                    self._volumes.worker_name,
                    workspace=self.settings.workspace,
                    expected_sandbox_id=self._sandbox_id,
                    timeout_seconds=90,
                )
            self._volumes.remove_after_revocation()
        if self._workspace is not None:
            self._workspace.reset_client()
        self.control.close()
        payload = json.dumps(
            {
                "attempt": str(record.intent.attempt_id),
                "sandbox": self._sandbox_id,
                "worker_absent": True,
                "projections_removed": True,
            },
            sort_keys=True,
        )
        return WorkerRevocationEvidence(
            attempt_id=record.intent.attempt_id,
            sandbox_id=self._sandbox_id,
            observer_id="native-" + self.settings.gateway,
            evidence_sha256=hashlib.sha256(payload.encode()).hexdigest(),
            outcome="revoked" if self._sandbox_id is not None else "not-created",
        )

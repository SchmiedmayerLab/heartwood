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
from contextlib import ExitStack, suppress
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
    WorkerControlPlane,
    WorkerControlPlaneError,
    WorkerGatewayCapabilityError,
    WorkerGatewayReport,
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
    WorkerReconciler,
    WorkerSessionPlan,
    WorkerSessionRequest,
)
from heartwood.gateway.worker_volumes import (
    WorkerVolumes,
    WorkerVolumeSettings,
    container_runtime_sha256,
)

# The pinned sandbox gateway returns these statuses for a creation request only before it
# records or requests the sandbox; any other failure may still leave one created. They were
# checked against this gateway source revision and patch digests; recheck on any change.
_CREATION_REJECTED = ("INVALID_ARGUMENT", "PERMISSION_DENIED", "UNAUTHENTICATED")
_CREATION_REJECTED_GATEWAY = (
    "f3d976590d3eb1379690a2abaf0f0e26c21d487d",
    (
        "f36989e0cddf843d79ee8d43376704c4358decc906ad3c92bf40db6c429534c1",
        "206aab5e89fcb6f338781e3b51c2b54bb7502483a06fadca6f482aef9bcc41ee",
    ),
)


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
        client_tools = (
            *(ClientToolSpec.model_validate_json(spec) for spec in specifications),
            *request.client_tools,
        )
        names = [spec.name for spec in client_tools]
        if len(names) != len(set(names)):
            raise WorkerAttemptError("worker client tool names must be unique")
        return _NativeWorkerAttempt(settings, request, client_tools=client_tools).plan()

    return prepare


def require_gateway_capabilities(
    settings: OpenShellWorkerDeployment, project: ProjectContext
) -> None:
    """Require the registered gateway's deletion capabilities once before serving sessions."""
    control = NativeWorkerControl(settings, project)
    try:
        control.connect()
    finally:
        control.close()


def native_worker_reconciler(
    settings: OpenShellWorkerDeployment, project: ProjectContext
) -> WorkerReconciler:
    """Revoke attempts whose gateway process ended, only on the infrastructure that ran them."""

    def reconcile(record: WorkerAttemptRecord) -> WorkerRevocationEvidence:
        if record.phase != "fenced" or record.intent.project_id != project.project_id:
            raise WorkerAttemptError("native reconciliation requires this project's fenced attempt")
        control = NativeWorkerControl(settings, project)
        volume_settings = _volume_settings(settings, project, record.intent.image_digest)
        # Absence is evidence only on the infrastructure that ran the attempt.
        if record.intent.control_plane is None:
            raise WorkerControlPlaneError(
                "worker attempt does not record the control plane and runtime it ran on"
            )
        if record.intent.control_plane != _control_plane(control, volume_settings):
            raise WorkerControlPlaneError(
                "worker attempt ran on another sandbox control plane or container runtime"
            )
        control.connect()
        try:
            volumes = WorkerVolumes(volume_settings, record.intent)
            retained = record.runtime.sandbox_id if record.runtime else None
            # Without a recorded runtime, an ended process may have left creation unresolved.
            sandbox_id = _owned_sandbox(
                control, volumes, retained, uncertain=record.runtime is None
            )
            if sandbox_id is not None:
                _delete_sandbox(control, volumes, sandbox_id)
            volumes.remove_after_revocation()
        finally:
            control.close()
        return _revocation_evidence(settings, record, sandbox_id or retained, control.gateway)

    return reconcile


def _volume_settings(
    settings: OpenShellWorkerDeployment, project: ProjectContext, image_digest: str
) -> WorkerVolumeSettings:
    return WorkerVolumeSettings(
        docker_cli=settings.docker_cli,
        docker_host=settings.docker_host,
        workspace=settings.workspace,
        image_digest=image_digest,
        protected_paths=(project.root, project.state_root, settings.native_config_home),
        cpu=settings.cpu,
        memory=settings.memory,
    )


def _control_plane(
    control: NativeWorkerControl, volume_settings: WorkerVolumeSettings
) -> WorkerControlPlane:
    return WorkerControlPlane(
        control_plane_sha256=control.control_plane_sha256(),
        container_runtime_sha256=container_runtime_sha256(volume_settings),
    )


def _owned_sandbox(
    control: NativeWorkerControl,
    volumes: WorkerVolumes,
    sandbox_id: str | None,
    *,
    uncertain: bool = False,
) -> str | None:
    """Resolve the uniquely named sandbox only while it carries the complete attempt identity.

    With `uncertain`, a creation request may still be queued or provisioning, and the
    sandbox gateway does not report a creation it has not recorded yet, so absence is not
    evidence that nothing was created and the attempt stays unresolved.
    """
    try:
        observed = control.client.get(volumes.worker_name, workspace=control.settings.workspace)
    except control.grpc.RpcError as error:
        if error.code() != control.grpc.StatusCode.NOT_FOUND:
            raise WorkerAttemptError("native worker absence was not observed") from None
        if uncertain:
            raise WorkerAttemptError(
                "native worker absence after an unresolved creation is not evidence of revocation"
            ) from None
        return None
    if dict(observed.labels) != volumes.labels or (
        sandbox_id is not None and observed.id != sandbox_id
    ):
        raise WorkerAttemptError("native worker identity changed before revocation")
    return str(observed.id)


def _delete_sandbox(control: NativeWorkerControl, volumes: WorkerVolumes, sandbox_id: str) -> None:
    workspace = control.settings.workspace
    control.client.delete(volumes.worker_name, workspace=workspace, timeout_seconds=90)
    control.client.wait_deleted(
        volumes.worker_name,
        workspace=workspace,
        expected_sandbox_id=sandbox_id,
        timeout_seconds=90,
    )


def _revocation_evidence(
    settings: OpenShellWorkerDeployment,
    record: WorkerAttemptRecord,
    sandbox_id: str | None,
    gateway: WorkerGatewayReport | None,
) -> WorkerRevocationEvidence:
    payload = json.dumps(
        {
            "attempt": str(record.intent.attempt_id),
            "sandbox": sandbox_id,
            "worker_absent": True,
            "projections_removed": True,
        },
        sort_keys=True,
    )
    return WorkerRevocationEvidence(
        attempt_id=record.intent.attempt_id,
        sandbox_id=sandbox_id,
        observer_id="native-" + settings.gateway,
        evidence_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        outcome="revoked" if sandbox_id is not None else "not-created",
        gateway=gateway,
    )


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
        self._control_plane = _control_plane(
            self.control, _volume_settings(settings, request.project, settings.image_digest)
        )
        self._creation_uncertain = False
        self._preparation = ExitStack()
        self._intent: WorkerAttemptIntent | None = None
        self._volumes: WorkerVolumes | None = None
        self._forward: NativeWorkerForward | None = None
        self._workspace: AdmittedWorkerWorkspace | None = None
        self._sandbox_id: str | None = None
        self._generation: str | None = None
        self._key = secrets.token_urlsafe(32)
        self._placeholder: str | None = None
        self._connected = False
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
            control_plane=self._control_plane,
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
                or intent.control_plane != self._control_plane
            ):
                raise WorkerAttemptError("native worker intent does not match preparation")
            self.control.connect()
            volume_settings = _volume_settings(
                self.settings, self.request.project, self.settings.image_digest
            )
            if _control_plane(self.control, volume_settings) != self._control_plane:
                raise WorkerControlPlaneError(
                    "sandbox control plane or container runtime changed after preparation"
                )
            volumes = self._volumes = WorkerVolumes(volume_settings, intent)
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
                environment={
                    "SESSION_API_KEY": self._key,
                    "HEARTWOOD_SKILL_PLATFORM": self.request.skills.platform_id,
                },
                command=[
                    "/opt/heartwood/.venv/bin/python",
                    "-I",
                    "-B",
                    "-u",
                    "/opt/heartwood/images/worker/launch.py",
                ],
            )
            # Until the control plane answers, the creation may complete later.
            self._creation_uncertain = True
            try:
                created = self.control.client.create(
                    workspace=self.settings.workspace,
                    name=volumes.worker_name,
                    spec=spec,
                    labels=volumes.labels,
                )
            except self.control.grpc.RpcError as error:
                status = self.control.grpc.StatusCode
                if error.code() in tuple(status[name] for name in _CREATION_REJECTED):
                    self._creation_uncertain = False
                raise
            self._sandbox_id = created.id
            self._creation_uncertain = False
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
                gateway=self.control.gateway,
            )
        except BaseException as error:
            self._stop_unadmitted()
            self._preparation.close()
            if not isinstance(error, Exception):
                raise
            if isinstance(error, WorkerGatewayCapabilityError):
                # Refused before any sandbox or volume request; the refusal is the diagnostic.
                raise
            raise WorkerAttemptError(
                "native worker launch requires outside reconciliation"
            ) from None

    def _stop_sandbox(self, *, revoking: bool = False) -> None:
        assert self._volumes is not None
        observed = _owned_sandbox(
            self.control,
            self._volumes,
            self._sandbox_id,
            uncertain=revoking and self._creation_uncertain and self._sandbox_id is None,
        )
        if observed is not None:
            # Retained before deletion so an interrupted deletion is still recorded as revoked.
            self._sandbox_id = observed
            _delete_sandbox(self.control, self._volumes, observed)

    def _stop_unadmitted(self) -> None:
        # Revocation still observes absence and removes projections before releasing the attempt.
        if self._volumes is not None and self.control.client is not None:
            with suppress(Exception):
                self._stop_sandbox()
        if self._forward is not None:
            with suppress(Exception):
                self._forward.close()

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
 Path('/opt/heartwood/vendor/heartwood-skills/skills'),platform_id=sys.argv[2])
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
                self.request.skills.platform_id,
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
        if lease.intent != self._intent or self._placeholder is None or self._connected:
            raise WorkerAttemptError("native worker connection does not match its launch")
        self._connected = True
        try:
            lease.require_runtime_observation(self._observe)
        except Exception:
            self._stop_unadmitted()
            raise
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
        if self._volumes is not None:
            # The connected client keeps the identity admitted at launch, so stopping the
            # worker does not wait for local cleanup or a repeated configuration check.
            try:
                self._stop_sandbox(revoking=True)
            except Exception:
                # A forwarder to a sandbox that is being stopped is never useful.
                if self._forward is not None:
                    with suppress(Exception):
                        self._forward.close()
                raise
        self._preparation.close()
        if self._forward is not None:
            self._forward.close()
        if self._volumes is not None:
            self.control.verify_configuration()
            self._volumes.remove_after_revocation()
        if self._workspace is not None:
            self._workspace.reset_client()
        self.control.close()
        return _revocation_evidence(self.settings, record, self._sandbox_id, self.control.gateway)

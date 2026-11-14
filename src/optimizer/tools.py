import time
from collections.abc import Callable

from kubernetes import client
from openai.types.responses import FunctionToolParam
from pydantic import BaseModel, ConfigDict

from optimizer.config import Settings
from optimizer.detect import same_configuration
from optimizer.models import Allocation, DeploymentSpec, Recommendation, utc_now
from optimizer.observe import deployment_spec


class ExecutionError(RuntimeError):
    """A guarded execution stopped; no automatic retry or rollback is allowed."""


class TargetArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    namespace: str
    workload: str


class ApplyArguments(Allocation):
    namespace: str
    workload: str


def tool_definitions() -> list[FunctionToolParam]:
    return [
        {
            "type": "function", "name": "get_deployment", "strict": True,
            "description": "Read the approved live Deployment and check it against the evidence.",
            "parameters": TargetArguments.model_json_schema(),
        },
        {
            "type": "function", "name": "apply_change", "strict": True,
            "description": "Apply exactly the approved values after a read; dry-run, patch and wait.",
            "parameters": ApplyArguments.model_json_schema(),
        },
    ]


class DeploymentTools:
    """The executor's only Kubernetes access, bound to one approved recommendation."""

    def __init__(
        self, apps: client.AppsV1Api, recommendation: Recommendation, settings: Settings,
        on_patch: Callable[[bool], None],
    ) -> None:
        self.apps = apps
        self.recommendation = recommendation
        self.settings = settings
        self.on_patch = on_patch
        self.read_completed = False
        self.patch_attempted = False
        self.applied = False
        self.rollout_completed = False

    def check_target(self, namespace: str, workload: str) -> None:
        approved = self.recommendation.evidence.workload.spec
        if (
            namespace != "workloads"
            or namespace != approved.namespace
            or workload != approved.name
        ):
            raise ExecutionError("Only the approved workload in workloads can be accessed")

    def read_unchanged(self) -> client.V1Deployment:
        approved = self.recommendation.evidence.workload.spec
        self.check_target(approved.namespace, approved.name)
        if approved.uid is None or approved.spec_hash is None:
            raise ExecutionError("This recommendation predates execution evidence; analyze again")
        live = self.apps.read_namespaced_deployment(
            approved.name, approved.namespace,
            _request_timeout=self.settings.prometheus_timeout_seconds,
        )
        current = deployment_spec(live)
        if (
            current.uid != approved.uid
            or current.spec_hash != approved.spec_hash
            or not same_configuration(current, approved)
        ):
            raise ExecutionError("The live Deployment changed since the recommendation; analyze again")
        if live.spec.paused or live.metadata.deletion_timestamp is not None:
            raise ExecutionError("The Deployment is paused or being deleted")
        if not live.metadata.resource_version:
            raise ExecutionError("The live Deployment has no resource version")
        return live

    def get_deployment(self, arguments: TargetArguments) -> dict:
        self.check_target(arguments.namespace, arguments.workload)
        live = self.read_unchanged()
        self.read_completed = True
        return deployment_spec(live).model_dump(mode="json")

    def apply_change(self, arguments: ApplyArguments) -> dict:
        self.check_target(arguments.namespace, arguments.workload)
        if not self.read_completed:
            raise ExecutionError("get_deployment must succeed before apply_change")
        if self.patch_attempted:
            raise ExecutionError("A real patch was already attempted; it will not be repeated")
        values = Allocation.model_validate(
            arguments.model_dump(exclude={"namespace", "workload"})
        )
        if values != self.recommendation.new_values:
            raise ExecutionError("The proposed values differ from the approved recommendation")
        live = self.read_unchanged()
        previous = deployment_spec(live)
        changed_resources = values.model_dump(exclude={"replicas"}) != previous.resources.model_dump()
        changed_replicas = values.replicas != previous.replicas
        if (
            (not changed_resources and not changed_replicas)
            or (self.recommendation.action == "resize" and changed_replicas)
            or (self.recommendation.action == "scale" and changed_resources)
        ):
            raise ExecutionError("The change does not match the approved action")

        # Patch only owned fields. resourceVersion closes the read/patch race:
        # a concurrent update causes a conflict instead of overwriting it.
        spec: dict = {"replicas": values.replicas}
        if self.recommendation.action == "resize":
            spec = {"template": {"spec": {"containers": [{
                "name": previous.container_name,
                "resources": {
                    "requests": {
                        "cpu": f"{values.cpu_request_millicores}m",
                        "memory": f"{values.memory_request_mib}Mi",
                    },
                    "limits": {
                        "cpu": f"{values.cpu_limit_millicores}m",
                        "memory": f"{values.memory_limit_mib}Mi",
                    },
                },
            }]}}}
        body = {
            "metadata": {"resourceVersion": live.metadata.resource_version},
            "spec": spec,
        }
        patch_options = {
            "_content_type": "application/strategic-merge-patch+json",
            "_request_timeout": self.settings.prometheus_timeout_seconds,
        }
        dry_run = self.apps.patch_namespaced_deployment(
            previous.name, previous.namespace, body, dry_run="All", **patch_options,
        )
        self.check_result(dry_run, previous)
        # Save the attempt before sending a write: a timeout can leave its result
        # unknown, and a restarted process must not automatically repeat it.
        self.on_patch(False)
        self.patch_attempted = True
        patched = self.apps.patch_namespaced_deployment(
            previous.name, previous.namespace, body, **patch_options,
        )
        self.applied = True
        self.on_patch(True)
        self.check_result(patched, previous)
        self.wait_for_rollout(patched)
        self.rollout_completed = True
        return {"status": "executed", "values": values.model_dump(), "completed_at": utc_now().isoformat()}

    def check_result(self, deployment: client.V1Deployment, previous: DeploymentSpec) -> None:
        """Admission webhooks must not silently replace the approved resource values."""
        result = deployment_spec(deployment)
        expected = self.recommendation.new_values
        if (
            result.uid != previous.uid
            or result.namespace != previous.namespace
            or result.name != previous.name
            or result.resources.model_dump() != expected.model_dump(exclude={"replicas"})
            or result.replicas != expected.replicas
        ):
            raise ExecutionError("The server returned values different from the approved change")

    def wait_for_rollout(self, patched: client.V1Deployment) -> None:
        """Require the patched generation to be fully rolled out, not merely ready."""
        expected = deployment_spec(patched)
        generation = patched.metadata.generation
        if generation is None:
            raise ExecutionError("The patch response has no Deployment generation")
        deadline = time.monotonic() + self.settings.rollout_timeout_seconds
        while time.monotonic() < deadline:
            live = self.apps.read_namespaced_deployment(
                expected.name, expected.namespace,
                _request_timeout=min(
                    self.settings.prometheus_timeout_seconds,
                    max(0.01, deadline - time.monotonic()),
                ),
            )
            current = deployment_spec(live)
            if current.uid != expected.uid or current.spec_hash != expected.spec_hash:
                raise ExecutionError("The Deployment changed again during rollout")
            status = live.status
            if status is not None:
                for condition in status.conditions or []:
                    if condition.type == "Progressing" and condition.status == "False":
                        raise ExecutionError(f"Rollout failed: {condition.reason}")
                if (
                    (status.observed_generation or 0) >= generation
                    and status.updated_replicas == expected.replicas
                    and status.replicas == expected.replicas
                    and status.ready_replicas == expected.replicas
                    and status.available_replicas == expected.replicas
                    and not status.unavailable_replicas
                ):
                    return
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise ExecutionError("Timed out waiting for rollout; the patch may already be applied")

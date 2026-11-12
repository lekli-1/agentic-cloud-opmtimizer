import logging

from optimizer.config import Settings
from optimizer.models import DeploymentSpec, Issue, Workload

logger = logging.getLogger(__name__)


def missing_detection_data(workload: Workload) -> list[str]:
    fields = {
        "cpu_p95_millicores": workload.metrics.cpu_p95_millicores,
        "cpu_throttling_percent": workload.metrics.cpu_throttling_percent,
        "memory_peak_mib": workload.metrics.memory_peak_mib,
        "cpu_request_millicores": workload.spec.resources.cpu_request_millicores,
        "memory_limit_mib": workload.spec.resources.memory_limit_mib,
    }
    return [name for name, value in fields.items() if value is None]


def same_configuration(first: DeploymentSpec, second: DeploymentSpec) -> bool:
    # Readiness changes and metric noise do not describe a new allocation.
    return first.model_dump(exclude={"ready_replicas"}) == second.model_dump(
        exclude={"ready_replicas"}
    )


def detect(workload: Workload, settings: Settings) -> list[Issue]:
    """Return independent issue flags only when all detection inputs are available."""
    missing = missing_detection_data(workload)
    if missing:
        logger.warning("%s: detection needs %s", workload.spec.name, ", ".join(missing))
        return []
    if workload.spec.replicas == 0:
        return []

    cpu_p95 = workload.metrics.cpu_p95_millicores
    throttling = workload.metrics.cpu_throttling_percent
    memory_peak = workload.metrics.memory_peak_mib
    cpu_request = workload.spec.resources.cpu_request_millicores
    memory_limit = workload.spec.resources.memory_limit_mib
    assert cpu_p95 is not None and throttling is not None and memory_peak is not None
    assert cpu_request is not None and memory_limit is not None

    # Metrics are Deployment totals, but Kubernetes resources are per replica.
    total_cpu_request = cpu_request * workload.spec.replicas
    total_memory_limit = memory_limit * workload.spec.replicas
    issues: list[Issue] = []
    # Low p95 and high throttling can coexist, so CPU flags are not exclusive.
    if cpu_p95 < settings.cpu_overprovisioned_ratio * total_cpu_request:
        issues.append("over_provisioned")
    if (
        cpu_p95 > settings.cpu_underprovisioned_ratio * total_cpu_request
        or throttling > settings.cpu_throttling_threshold_percent
    ):
        issues.append("under_provisioned")
    if memory_peak > settings.memory_risk_ratio * total_memory_limit:
        issues.append("memory_risk")
    return issues

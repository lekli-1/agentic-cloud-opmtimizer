import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx2
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity
from urllib3.exceptions import HTTPError as KubernetesTransportError

from optimizer.config import Settings
from optimizer.models import DeploymentSpec, Metrics, Observation, Resources, Workload

logger = logging.getLogger(__name__)


def prometheus_queries(settings: Settings) -> dict[str, str]:
    """Summarize Deployment totals, not the sum of individual pod peaks.

    Aggregate pods at each instant before calculating the window's p95 or peak.
    CPU is returned in millicores and memory in MiB.
    """
    namespace = settings.workload_namespace
    window = f"{settings.observe_window_seconds}s"
    containers = (
        f'namespace="{namespace}",job="kubelet",metrics_path="/metrics/cadvisor",'
        'container!="",container!="POD"'
    )
    # This recording rule supplies the Deployment label missing from cAdvisor.
    owners = (
        " * on (namespace, pod) group_left(workload) "
        "namespace_workload_pod:kube_pod_owner:relabel"
        f'{{namespace="{namespace}",workload_type="deployment"}}'
    )
    cpu = (
        f"sum by (workload) (rate(container_cpu_usage_seconds_total"
        f"{{{containers}}}[1m]){owners})"
    )
    memory = (
        f"sum by (workload) (container_memory_working_set_bytes"
        f"{{{containers}}}{owners})"
    )
    throttled = (
        "sum by (workload) (increase(container_cpu_cfs_throttled_periods_total"
        f"{{{containers}}}[{window}]){owners})"
    )
    periods = (
        "sum by (workload) (increase(container_cpu_cfs_periods_total"
        f"{{{containers}}}[{window}]){owners})"
    )
    return {
        "cpu_avg_millicores": f"1000 * avg_over_time(({cpu})[{window}:30s])",
        "cpu_p95_millicores": f"1000 * quantile_over_time(0.95, ({cpu})[{window}:30s])",
        "memory_peak_mib": f"max_over_time(({memory})[{window}:30s]) / 1048576",
        "cpu_throttling_percent": f"100 * {throttled} / {periods}",
    }


def parse_vector(payload: object) -> dict[str, float]:
    """Reject malformed/duplicate samples; leave unavailable values absent.

    Non-finite samples can occur when a counter has no usable history. Treating
    them as zero would incorrectly make an unobserved workload look idle.
    """
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError("Prometheus query did not succeed")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise ValueError("Prometheus query must return an instant vector")
    result = data.get("result")
    if not isinstance(result, list):
        raise ValueError("Prometheus result must be a list")
    values: dict[str, float] = {}
    for series in result:
        if not isinstance(series, dict):
            raise ValueError("Invalid Prometheus series")
        metric, value = series.get("metric"), series.get("value")
        if not isinstance(metric, dict) or not isinstance(metric.get("workload"), str):
            raise ValueError("Prometheus series has no workload label")
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("Invalid Prometheus sample")
        if not isinstance(value[1], (str, int, float)) or isinstance(value[1], bool):
            raise ValueError("Invalid Prometheus sample value")
        name = metric["workload"]
        number = float(value[1])
        if not math.isfinite(number):
            logger.warning("No finite Prometheus sample for %s", name)
            continue
        if number < 0 or name in values:
            raise ValueError(f"Invalid or duplicate Prometheus sample for {name}")
        values[name] = number
    return values


def resource_value(values: dict[str, str], name: str, multiplier: float) -> float | None:
    value = values.get(name)
    return None if value is None else float(parse_quantity(value)) * multiplier


def deployment_spec(deployment: client.V1Deployment) -> DeploymentSpec:
    """Normalize Kubernetes quantities into per-replica millicores and MiB."""
    containers = deployment.spec.template.spec.containers
    if len(containers) != 1:
        raise ValueError(f"{deployment.metadata.name}: the demo requires one container")
    container = containers[0]
    resources = container.resources or client.V1ResourceRequirements()
    requests, limits = resources.requests or {}, resources.limits or {}
    return DeploymentSpec(
        namespace=deployment.metadata.namespace,
        name=deployment.metadata.name,
        container_name=container.name,
        replicas=deployment.spec.replicas if deployment.spec.replicas is not None else 1,
        ready_replicas=(deployment.status.ready_replicas or 0) if deployment.status else 0,
        resources=Resources(
            cpu_request_millicores=resource_value(requests, "cpu", 1000),
            cpu_limit_millicores=resource_value(limits, "cpu", 1000),
            memory_request_mib=resource_value(requests, "memory", 1 / 1048576),
            memory_limit_mib=resource_value(limits, "memory", 1 / 1048576),
        ),
    )


class Observer:
    def __init__(
        self, settings: Settings, apps: client.AppsV1Api, prometheus: httpx2.Client
    ) -> None:
        self.settings = settings
        self.apps = apps
        self.prometheus = prometheus

    @classmethod
    def connect(cls, settings: Settings) -> "Observer":
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            config.load_incluster_config()
        else:
            config.load_kube_config()
        return cls(
            settings,
            client.AppsV1Api(),
            httpx2.Client(
                base_url=settings.prometheus_url.rstrip("/") + "/",
                timeout=settings.prometheus_timeout_seconds,
            ),
        )

    def collect(self, save_raw: Path | None = None) -> Observation:
        """Read live specs and evaluate all metrics at one shared timestamp."""
        deployments = self.apps.list_namespaced_deployment(
            self.settings.workload_namespace,
            _request_timeout=self.settings.prometheus_timeout_seconds,
        ).items
        observed_at = datetime.now(timezone.utc)
        samples: dict[str, dict[str, float]] = {}
        if save_raw is not None:
            save_raw.mkdir(parents=True, exist_ok=True)
        for name, query in prometheus_queries(self.settings).items():
            response = self.prometheus.get(
                "api/v1/query", params={"query": query, "time": observed_at.timestamp()}
            )
            response.raise_for_status()
            payload = response.json()
            samples[name] = parse_vector(payload)
            if save_raw is not None:
                (save_raw / f"{name}.json").write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
        workloads = []
        for deployment in sorted(deployments, key=lambda item: item.metadata.name):
            spec = deployment_spec(deployment)
            values = {name: samples[name].get(spec.name) for name in samples}
            missing = [name for name, value in values.items() if value is None]
            if missing:
                logger.warning("%s: missing metrics: %s", spec.name, ", ".join(missing))
            workloads.append(Workload(spec=spec, metrics=Metrics(**values)))
        return Observation(
            observed_at=observed_at,
            window_seconds=self.settings.observe_window_seconds,
            workloads=workloads,
        )

    def close(self) -> None:
        self.prometheus.close()
        self.apps.api_client.close()


@dataclass
class ObservationState:
    """Keep the last good snapshot even when a refresh fails.

    The interruption flag survives recovery until issue tracking consumes it.
    """

    snapshot: Observation | None = None
    error: str | None = None
    interrupted: bool = False


async def observe_loop(
    observer: Observer, state: ObservationState, stop: asyncio.Event,
    on_observation: Callable[[Observation], Awaitable[None]] | None = None,
) -> None:
    """Collect off the event loop, then notify the fast detection callback.

    The callback schedules analysis rather than waiting for the model. A failed
    refresh keeps the old snapshot and records a gap in issue continuity.
    """
    while not stop.is_set():
        started = time.monotonic()
        try:
            state.snapshot = await asyncio.to_thread(observer.collect)
            state.error = None
        except (ApiException, KubernetesTransportError, httpx2.HTTPError, ValueError) as exc:
            state.error = f"{type(exc).__name__}: {exc}"
            state.interrupted = True
            logger.exception("Workload observation failed")
        else:
            if on_observation is not None and not stop.is_set():
                await on_observation(state.snapshot)
        # Include collection time in the interval instead of drifting each cycle.
        delay = max(
            0.01,
            observer.settings.observe_interval_seconds - (time.monotonic() - started),
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass

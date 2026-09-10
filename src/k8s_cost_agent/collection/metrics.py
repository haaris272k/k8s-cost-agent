"""Build one normalized metrics record for each Kubernetes Deployment.

Kubernetes supplies workload configuration and incident information, while
Prometheus supplies the current CPU and memory measurements. The collector is
read-only and produces one JSON-compatible record per Deployment.

Kubernetes quantity strings are converted at this boundary. Downstream code
therefore always receives CPU as numeric cores and memory as numeric bytes.
"""

import re
import sys
from datetime import datetime
from typing import Any

from kubernetes import config
from kubernetes.utils.quantity import parse_quantity

from k8s_cost_agent.config import KubernetesSettings, PrometheusSettings


RESOURCE_NAMES = ("cpu", "memory")


def load_kubernetes_config(mode: str) -> None:
    """Load Kubernetes credentials for local or in-cluster execution.

    ``kubeconfig`` is intended for local development, while ``in_cluster``
    reads the service-account credentials mounted in a Kubernetes pod. In
    ``auto`` mode the local file is tried first, then in-cluster credentials.
    """
    if mode == "kubeconfig":
        config.load_kube_config()
        return
    if mode == "in_cluster":
        config.load_incluster_config()
        return
    if mode != "auto":
        raise ValueError(f"Unsupported Kubernetes config mode: {mode}")
    try:
        config.load_kube_config()
    except config.ConfigException:
        config.load_incluster_config()


def _sum_resource_quantities(values: list[str]) -> float | None:
    """Sum Kubernetes quantities and return one numeric base unit.

    Args:
        values: Quantity strings such as ``["250m", "500m"]``.

    Returns:
        CPU is returned as cores and memory as bytes. ``None`` is returned
        when no container requested the resource.
    """
    if not values:
        return None
    return float(sum((parse_quantity(value) for value in values), parse_quantity("0")))


def _deployment_resources(deployment: Any) -> dict[str, float | None]:
    """Extract total CPU and memory requests and limits.

    Kubernetes stores requests per container. Summing them gives the total
    request represented by the Deployment's pod, including multi-container
    pods, while preserving ``None`` for an unspecified resource. CPU is
    returned in cores and memory is returned in bytes, matching Prometheus.
    """
    # Collect strings first because a pod can contain several containers. The
    # result represents the resources requested by one complete pod replica.
    values = {
        f"{kind}_{resource}": []
        for kind in ("request", "limit")
        for resource in RESOURCE_NAMES
    }
    for container in deployment.spec.template.spec.containers:
        for kind in ("request", "limit"):
            quantities = getattr(container.resources, f"{kind}s", None) or {}
            for resource in RESOURCE_NAMES:
                if resource in quantities:
                    values[f"{kind}_{resource}"].append(str(quantities[resource]))
    return {
        key: _sum_resource_quantities(quantity_values)
        for key, quantity_values in values.items()
    }


def _latest_restart_was_oomkilled(pods: list[Any]) -> bool:
    """Determine whether the newest recorded termination was an OOMKill.

    A pod may expose several container statuses and a Deployment may have
    several pods. The timestamp on ``last_state.terminated`` is used to select
    the newest event; older OOMKills must not be reported as the latest event
    when a newer termination had another reason.
    """
    # A Deployment may temporarily have old and new pods during a rollout.
    # Compare timestamps across every container rather than trusting list order.
    terminations: list[tuple[datetime, bool]] = []
    for pod in pods:
        for status in pod.status.container_statuses or []:
            termination = status.last_state.terminated
            if termination and termination.finished_at:
                terminations.append(
                    (
                        termination.finished_at,
                        termination.reason == "OOMKilled",
                    )
                )
    return max(terminations, default=(datetime.min, False))[1]


def _query_value(
    prometheus: Any, query: str, debug: bool = False
) -> float:
    """Run an instant PromQL query and sum all returned sample values.

    Prometheus returns values as strings inside ``[timestamp, value]`` pairs.
    Empty result vectors are valid, for example when no pod currently exists,
    and naturally produce ``0.0``.
    """
    results = prometheus.custom_query(query=query)
    if debug:
        print(f"PROMQL: {query}", file=sys.stderr)
        print(f"PROMETHEUS RESPONSE: {results!r}", file=sys.stderr)
    # Prometheus may return several time series even for a summed query. Adding
    # them here gives one workload-level number and naturally maps no series to 0.
    return sum(float(result["value"][1]) for result in results)


def _label_selector(deployment: Any) -> str:
    """Build the selector used to find pods owned by a Deployment.

    The selector is derived from ``spec.selector.match_labels`` rather than
    from the Deployment name because Kubernetes ownership is label-based.
    """
    # Kubernetes uses all matchLabels entries together (logical AND).
    return ",".join(
        f"{key}={value}"
        for key, value in deployment.spec.selector.match_labels.items()
    )


def _promql_queries(
    namespace: str,
    pod_names: list[str],
    settings: PrometheusSettings,
) -> tuple[str, str]:
    """Build CPU and memory queries for a set of pods.

    Args:
        namespace: Kubernetes namespace containing the pods.
        pod_names: Pod names to include in the Prometheus regex matcher.

    Returns:
        A ``(cpu_query, memory_query)`` tuple.

    The cAdvisor series in this cluster reliably expose ``namespace`` and
    ``pod``. Container and image filters are intentionally omitted because
    those labels are absent from the observed series.
    """
    # Prometheus/RE2 accepts literal hyphens but rejects Python's ``\-`` form.
    pod_pattern = "|".join(
        re.escape(name).replace(r"\-", "-") for name in pod_names
    )
    match = (
        f'{settings.namespace_label}="{namespace}",'
        f'{settings.pod_label}=~"{pod_pattern}"'
    )
    return (
        f"sum(rate({settings.cpu_metric}{{{match}}}"
        f"[{settings.cpu_rate_window}]))",
        f"sum({settings.memory_metric}{{{match}}})",
    )


def _workload_record(
    deployment: Any,
    pods: list[Any],
    prometheus: Any,
    kubernetes_settings: KubernetesSettings,
    prometheus_settings: PrometheusSettings,
    debug_prometheus: bool,
) -> dict[str, Any]:
    """Combine Deployment configuration, pod status, and Prometheus usage.

    If no current pods match the Deployment selector, Kubernetes metadata is
    still returned and usage is represented as zero. Prometheus is not queried
    with an empty pod regex because that could accidentally match unrelated
    series.
    """
    namespace = kubernetes_settings.namespace
    pod_names = [pod.metadata.name for pod in pods]
    cpu_usage = memory_usage = 0.0
    if pod_names:
        # CPU is a counter and uses the configured rate; memory is a gauge.
        cpu_query, memory_query = _promql_queries(
            namespace, pod_names, prometheus_settings
        )
        cpu_usage = _query_value(prometheus, cpu_query, debug_prometheus)
        memory_usage = _query_value(prometheus, memory_query, debug_prometheus)

    # Resource requests come from the pod template; usage and incidents come
    # from the currently selected pods. Keeping both in one record lets later
    # stages compare requested capacity with observed use.
    resources = _deployment_resources(deployment)
    return {
        "name": deployment.metadata.name,
        "namespace": namespace,
        "requested_cpu_cores": resources["request_cpu"],
        "requested_memory_bytes": resources["request_memory"],
        "cpu_limit_cores": resources["limit_cpu"],
        "memory_limit_bytes": resources["limit_memory"],
        "current_cpu_usage_cores": cpu_usage,
        "current_memory_usage_bytes": memory_usage,
        "restart_count": sum(
            status.restart_count
            for pod in pods
            for status in pod.status.container_statuses or []
        ),
        "last_restart_oomkilled": _latest_restart_was_oomkilled(pods),
        "criticality": (
            deployment.metadata.annotations or {}
        ).get(kubernetes_settings.criticality_annotation, "unknown"),
    }


def collect_workloads(
    apps_api: Any,
    core_api: Any,
    prometheus: Any,
    kubernetes_settings: KubernetesSettings,
    prometheus_settings: PrometheusSettings,
    debug_prometheus: bool = False,
) -> list[dict[str, Any]]:
    """Collect one record for every Deployment in ``namespace``.

    Args:
        apps_api: Kubernetes AppsV1 API client used to list Deployments.
        core_api: Kubernetes CoreV1 API client used to list pods and statuses.
        prometheus: Client used to execute the CPU and memory PromQL queries.
        kubernetes_settings: Namespace, annotation, and access settings.
        prometheus_settings: Metric, label, and rate-window settings.
        debug_prometheus: Whether to print the first query and raw response to
            stderr for troubleshooting.

    Returns:
        Deployments sorted by name, each represented by a regular dictionary.

    Set ``debug_prometheus`` to print the first workload's PromQL and raw
    responses to stderr while leaving stdout as valid JSON.
    """
    workloads: list[dict[str, Any]] = []
    namespace = kubernetes_settings.namespace
    # Sort Deployments so repeated runs produce stable JSON and readable diffs.
    deployments = apps_api.list_namespaced_deployment(namespace).items

    for deployment in sorted(deployments, key=lambda item: item.metadata.name):
        # Query pods by the Deployment's selector. A name prefix would be
        # unreliable during rollouts and for custom naming conventions.
        selector = _label_selector(deployment)
        pods = core_api.list_namespaced_pod(namespace, label_selector=selector).items
        debug_queries = debug_prometheus and not workloads
        workloads.append(
            _workload_record(
                deployment,
                pods,
                prometheus,
                kubernetes_settings,
                prometheus_settings,
                debug_queries,
            )
        )
    return workloads

"""Collect a chronological window of workload metrics.

One snapshot is a list of Deployment records. Repeating snapshots creates the
history consumed by the statistics module; the final shape is therefore a
list of snapshots, not a flat list of workloads.
"""

import time
from typing import Any, Callable

from kubernetes import client
from prometheus_api_client import PrometheusConnect

from k8s_cost_agent.collection.metrics import collect_workloads, load_kubernetes_config
from k8s_cost_agent.config import Settings


def collect_history(
    settings: Settings,
    status_callback: Callable[[str], None] | None = None,
) -> list[list[dict[str, Any]]]:
    """Collect the configured number of snapshots and wait between them.

    Args:
        settings: Validated settings shared by the pipeline.
        status_callback: Optional progress sink.

    Returns:
        Snapshots in collection order. There are ``count - 1`` waits because
        the function does not sleep after the final sample.
    """
    sampling = settings.sampling
    # Create clients once and reuse them for every sample in the window.
    load_kubernetes_config(settings.kubernetes.config_mode)
    apps_api = client.AppsV1Api()
    core_api = client.CoreV1Api()
    prometheus = PrometheusConnect(
        url=settings.prometheus.url,
        disable_ssl=settings.prometheus.disable_ssl,
    )
    history = []

    for sample_number in range(sampling.count):
        history.append(
            collect_workloads(
                apps_api,
                core_api,
                prometheus,
                settings.kubernetes,
                settings.prometheus,
                debug_prometheus=(
                    sampling.debug_prometheus and sample_number == 0
                ),
            )
        )
        if status_callback:
            status_callback(
                f"Collected sample {sample_number + 1}/{sampling.count}"
            )
        # Sleeping only between samples avoids an unnecessary delay at the end.
        if sample_number < sampling.count - 1:
            time.sleep(sampling.interval_seconds)
    return history

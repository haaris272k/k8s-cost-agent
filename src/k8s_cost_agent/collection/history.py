"""Collect repeated Phase 2 snapshots for the Phase 3 stats engine."""

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
    """
    sampling = settings.sampling
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
        if sample_number < sampling.count - 1:
            time.sleep(sampling.interval_seconds)
    return history

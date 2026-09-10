"""Tests for configurable PromQL query construction."""

import unittest
from dataclasses import replace

from k8s_cost_agent.collection.metrics import _promql_queries
from tests.helpers import SETTINGS


class MetricsTests(unittest.TestCase):
    def test_promql_uses_configured_metrics_labels_and_rate_window(self):
        prometheus = replace(
            SETTINGS.prometheus,
            cpu_metric="custom_cpu_total",
            memory_metric="custom_memory_bytes",
            cpu_rate_window="2m",
            namespace_label="k8s_namespace",
            pod_label="k8s_pod",
        )

        cpu_query, memory_query = _promql_queries(
            "demo", ["pod-a"], prometheus
        )

        self.assertEqual(
            cpu_query,
            'sum(rate(custom_cpu_total{k8s_namespace="demo",'
            'k8s_pod=~"pod-a"}[2m]))',
        )
        self.assertEqual(
            memory_query,
            'sum(custom_memory_bytes{k8s_namespace="demo",'
            'k8s_pod=~"pod-a"})',
        )
        self.assertNotIn('container!=""', cpu_query)


if __name__ == "__main__":
    unittest.main()

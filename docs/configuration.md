# Configuration

The application has one non-secret configuration source:
`config/settings.toml`. Pass another file with `--config`. Relative paths are
resolved from `paths.base_dir`, which is itself relative to the TOML file.

## Settings reference

| Setting | Purpose |
| --- | --- |
| `paths.base_dir` | Base for all configured relative paths |
| `paths.environment` | Optional dotenv file containing secrets |
| `paths.samples` | Collector history output |
| `paths.statistics` | Statistics output and Gemini input |
| `paths.recommendations` | Guarded Gemini output |
| `paths.report` | HTML report output |
| `paths.recommendation_prompt` | External Gemini prompt template |
| `kubernetes.namespace` | Namespace whose Deployments are analyzed |
| `kubernetes.criticality_annotation` | Deployment annotation containing the tier |
| `kubernetes.config_mode` | `auto`, `kubeconfig`, or `in_cluster` |
| `prometheus.url` | Prometheus HTTP endpoint |
| `prometheus.disable_ssl` | Prometheus client SSL verification behavior |
| `prometheus.cpu_metric` | CPU counter metric |
| `prometheus.memory_metric` | Working-set gauge metric |
| `prometheus.cpu_rate_window` | PromQL CPU rate interval |
| `prometheus.namespace_label` | Namespace series label |
| `prometheus.pod_label` | Pod series label |
| `sampling.count` | Number of snapshots |
| `sampling.interval_seconds` | Wait between snapshots |
| `sampling.debug_prometheus` | Print first-sample PromQL responses to stderr |
| `statistics.percentile_rank` | Percentile used for safety evidence |
| `statistics.trend_change_threshold` | Relative half-window change threshold |
| `statistics.minimum_trend_samples` | Sample minimum for a trend classification |
| `guardrails.safety_margin` | Multiplier applied to percentile usage |
| `guardrails.high_variance_ratio` | Percentile-to-average low-confidence threshold |
| `guardrails.recent_incident_hours` | Restart/OOM lookback window |
| `guardrails.protected_criticalities` | Tiers that always require manual review |
| `gemini.api_key_environment_variable` | Name of the secret environment variable |
| `gemini.model` | Preferred Gemini model |
| `gemini.fallback_models` | Ordered availability fallbacks; may be empty |
| `gemini.thinking_level` | Configured reasoning level |
| `gemini.thinking_model_prefixes` | Model prefixes that accept thinking configuration; empty disables it |
| `gemini.max_rate_limit_retries` | Bounded retries for server-directed 429 waits |
| `gemini.retry_delay_buffer_seconds` | Buffer added to provider retry delays |
| `gemini.max_retry_delay_seconds` | Maximum delay for one retry |
| `gemini.max_tool_rounds` | Maximum model/tool conversation rounds |
| `gemini.max_output_tokens` | Structured response output limit |
| `report.title` | HTML report heading and document title |
| `report.subtitle` | HTML report safety description |

Invalid or missing settings fail before a pipeline stage starts. Structurally
unsafe policy values are also rejected—for example, a safety margin below
`1.0` or an empty protected-tier list. The committed configuration keeps
`tier-0` protected.

## Secrets

For initial setup, copy [the template](../.env.example) to `.env` and set the
configured Gemini variable. Preserve an existing `.env`:

```dotenv
GEMINI_API_KEY=your-local-key
```

`.env` and `key.txt` are ignored by Git. The loader does not override a value
already exported by the shell, which makes CI/CD secret injection predictable.
Never place credentials in TOML, command arguments, generated artifacts, or
logs. The application never reads `key.txt`.

## Sampling duration

The waits total `(sampling.count - 1) * sampling.interval_seconds`. Collection
and provider requests add to the overall run time. The checked-in values
(`10` samples, `5` seconds) give 45 seconds of waits for a quick check.

For the demo's periodic CPU bursts, prefer a ten-minute observation window:

```toml
[sampling]
count = 41
interval_seconds = 15
debug_prometheus = false
```

This changes the duration through configuration; no source changes are needed.

## Prompt template

The recommendation prompt is versioned separately from Python logic. It uses
`string.Template` placeholders:

- `$workload_names` — JSON list of workload names;
- `$tool_call_count` — expected number of read-only tool calls.

Unknown or missing placeholders fail before a provider request is completed.

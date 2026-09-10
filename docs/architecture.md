# Architecture

## Design boundary

The project is read-only and advisory. Gemini can propose values, but only the
deterministic analysis layer can approve them. The recommendation provider has
no Kubernetes write tools, and the pipeline contains no Kubernetes mutation
operation.

```text
Kubernetes API ----+
                   +--> collection --> statistics --> Gemini proposal
Prometheus --------+                         |               |
                                             +--> guardrails-+
                                                      |
                                                      v
                                                HTML report
```

The important dependency direction is inward toward deterministic data:

1. `collection` reads cluster configuration, usage, and incidents.
2. `analysis.statistics` calculates averages, percentiles, spread, and trends.
3. `recommendation.gemini` exposes three read-only data tools and requests
   structured proposals.
4. `analysis.guardrails` independently approves or rejects CPU and memory.
5. `reporting` displays raw proposals separately from approved values and
   calculates savings only from approvals.

`pipeline.py` coordinates those layers and owns artifact persistence. `cli.py`
is the only command-line boundary. Domain modules do not parse command-line
arguments or read environment variables.

## Package responsibilities

| Package | Responsibility | External side effects |
| --- | --- | --- |
| `collection` | Kubernetes metadata, incidents, PromQL, repeated samples | Read-only network calls |
| `analysis` | Statistics and deterministic safety decisions | None |
| `recommendation` | Gemini tool calling and structured proposals | Provider API calls only |
| `reporting` | Escaped, self-contained HTML | None |
| `pipeline` | Stage order and artifact persistence | Local file writes |
| `config` | TOML validation, path resolution, optional `.env` loading | Reads configuration only |

## Artifact flow

Configured paths default to:

1. `artifacts/samples.json` — chronological collector snapshots.
2. `artifacts/workload_stats.json` — one summarized record per workload.
3. `artifacts/recommendations.json` — raw proposals, decisions, model, and trace.
4. `artifacts/cost-optimization-report.html` — portable presentation output.

These files are generated locally and ignored by Git. The pipeline creates
their parent directory automatically, so a fresh checkout needs no artifacts.
Keep existing local results to resume a stage without repeating earlier work;
for example, `report` can be rerun without contacting the cluster or Gemini.

## Repository navigation

- `config/` contains the runtime settings and external prompt.
- `demo/` contains optional sample workloads, not a deployment of the agent.
- `src/k8s_cost_agent/` contains the application layers described above.
- `tests/` contains offline unit tests with in-code fixtures.
- `docs/` contains architecture and configuration details for deeper reading.

Python installers generate `*.egg-info/` metadata. It is ignored alongside
build output and caches; `pyproject.toml` remains the packaging source of truth.

## Safety properties

- Protected criticalities are always rejected; the included policy protects
  `tier-0`.
- Recent restarts or OOMKills fail closed when incident timestamps are absent.
- No value below the configured percentile safety margin is approved.
- Request growth is never approved.
- High-variance data lowers deterministic confidence.
- A rejected result carries the unchanged current request.
- The report independently treats every rejected value as unchanged, even if a
  manually edited artifact is internally inconsistent.
- Secrets are loaded by configured environment-variable name and never logged.

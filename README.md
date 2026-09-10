# Kubernetes Cost Optimization Agent

A read-only, explainable Kubernetes right-sizing pipeline built around one
safety rule: **the LLM proposes; deterministic code decides**.

The collector reads Kubernetes and Prometheus, the statistics layer summarizes
usage, Gemini proposes smaller CPU and memory requests, deterministic guardrails
approve or reject each proposal, and the reporting layer produces a portable
HTML report. The pipeline does not modify Kubernetes resources.

## Start here

To run the project, follow the setup below, edit
[config/settings.toml](config/settings.toml), provide your Gemini key, then run
`k8s-cost-agent run`. Open `artifacts/cost-optimization-report.html` when it
finishes.

| What you need | Where to look |
| --- | --- |
| Change the namespace, endpoints, sampling, or policy | [config/settings.toml](config/settings.toml) |
| Provide credentials | Local `.env`, copied from [.env.example](.env.example), or an exported environment variable |
| Inspect results | Local `artifacts/` directory, created by the pipeline |
| Try demonstration workloads | Optional [demo/](demo/) manifests |
| Understand or change the implementation | [src/k8s_cost_agent/](src/k8s_cost_agent/), [tests/](tests/), and [architecture](docs/architecture.md) |

## Setup

Prerequisites:

- Python 3.11 or newer.
- A running Kubernetes cluster and a configured kubeconfig.
- Prometheus collecting the cluster's CPU and memory metrics.
- A valid Gemini API key with model access and available quota.
- `kubectl` for the local port-forward and optional demo setup.

Run all commands from the repository root. The local example assumes minikube
with the Docker driver and an existing Prometheus installation.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --editable .
cp .env.example .env
```

Copy the template only when creating your local `.env`; keep an existing file.
Set `GEMINI_API_KEY` there. The file is ignored by Git and must never be printed
or committed. An already exported value takes precedence over `.env`.

Edit `config/settings.toml` for your namespace and Prometheus endpoint. The
included settings use namespace `cost-agent-demo` and
`http://localhost:9090`. For the existing minikube Prometheus installation,
keep this running in a separate terminal:

```bash
kubectl port-forward -n monitoring \
  svc/kube-prometheus-kube-prome-prometheus 9090:9090
```

### Optional demo workloads

The files in `demo/` create example workloads in `cost-agent-demo`; they are
not an agent service deployment. To create or update these demonstration
resources in your selected cluster:

```bash
kubectl apply -f demo/
```

Skip this if you already have workloads to analyze, and set
`kubernetes.namespace` to their namespace. The demo includes steady workloads,
`spiky-worker` with periodic CPU bursts, and `payment-service` with deliberate
OOMKills to demonstrate the guardrails.

## Run end to end

Validate configuration without contacting Kubernetes, Prometheus, or Gemini,
then run the complete pipeline:

```bash
k8s-cost-agent check-config
k8s-cost-agent run
```

The same commands are available through `python -m k8s_cost_agent`.

The checked-in sampling settings use 10 samples with a 5-second interval:
nine waits total approximately 45 seconds, plus collection and recommendation
time. This is a quick check and can miss periodic spikes. For the demo's
five-minute burst cycle, use at least a five-minute observation window,
preferably ten minutes: set `sampling.count = 41` and
`sampling.interval_seconds = 15`.

Progress is printed after each sample and stage. On success, open
`artifacts/cost-optimization-report.html`. All JSON and HTML outputs stay local
and are ignored by Git. They are created automatically; no example output files
are required before the first run.

To use another configuration file, put the global option before the command:

```bash
k8s-cost-agent --config config/settings.toml run
```

## Run one stage

Stage commands support diagnosis or reuse of existing local artifacts:

```bash
k8s-cost-agent collect
k8s-cost-agent analyze
k8s-cost-agent recommend
k8s-cost-agent report
```

| Command | Reads | Writes |
| --- | --- | --- |
| `collect` | Kubernetes and Prometheus | `artifacts/samples.json` |
| `analyze` | Saved samples | `artifacts/workload_stats.json` |
| `recommend` | Saved statistics and Gemini | `artifacts/recommendations.json` |
| `report` | Saved statistics and recommendations | `artifacts/cost-optimization-report.html` |

Paths come from the TOML file. Keep your local artifacts if you want to resume
at a later stage; deleting them requires regenerating the relevant inputs.

Gemini normally handles all workloads in one conversation: one request for
parallel read-only tool calls and one for the final structured proposals. Every
CPU and memory proposal passes independently through deterministic guardrails.

## Configuration

`config/settings.toml` contains runtime configuration.
`pyproject.toml` defines Python dependencies, packaging, and the command entry
point. See the [configuration reference](docs/configuration.md) for supported
settings.

The prompt is versioned separately in
[config/prompts/recommend_workloads.txt](config/prompts/recommend_workloads.txt).
CPU values remain numeric cores and memory values remain numeric bytes in every
generated JSON artifact.

## Project layout

```text
README.md                  Setup and normal usage
.env.example               Credential template with no real key
pyproject.toml             Python packaging and dependencies
config/                    Runtime settings and recommendation prompt
demo/                      Optional Kubernetes demonstration workloads
src/k8s_cost_agent/         Application implementation
tests/                     Offline unit tests
docs/                      Architecture and configuration references
artifacts/                 Generated local results, ignored by Git
```

The source package follows the processing flow: `collection/`, `analysis/`,
`recommendation/`, and `reporting/`. Shared modules handle configuration,
orchestration, file operations, and the CLI.

## Developer validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
k8s-cost-agent --help
k8s-cost-agent check-config
```

Tests use in-code fixtures and mocked provider calls; they need no saved
artifacts or live services. Full collection requires Kubernetes and Prometheus;
recommendation additionally requires Gemini access.

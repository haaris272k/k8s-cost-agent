"""Turn guarded recommendations into a portable HTML report.

The report is presentation only. It reads workload statistics and guarded
recommendations, and never connects to or modifies Kubernetes.

The renderer treats saved JSON as untrusted input: numeric fields are checked,
model text is HTML-escaped, and savings use only guardrail-approved values.
"""

import html
import json
from datetime import datetime, timezone
from typing import Any

from k8s_cost_agent.config import ReportSettings


def _index_records(
    records: list[dict[str, Any]], key: str, label: str
) -> dict[str, dict[str, Any]]:
    """Index records by a required unique string field."""
    # Duplicate names would make the join ambiguous, so fail instead of silently
    # replacing one record with another.
    indexed = {}
    for record in records:
        name = record.get(key)
        if not isinstance(name, str) or not name:
            raise ValueError(f"Every {label} entry must have a non-empty {key}")
        if name in indexed:
            raise ValueError(f"Duplicate {label} entry: {name}")
        indexed[name] = record
    return indexed


def _number(value: Any, field: str) -> float:
    """Return a finite non-negative number for a report resource field."""
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if number < 0 or number == float("inf") or number != number:
        raise ValueError(f"{field} must be a finite non-negative number")
    return number


def _savings_percent(current: float, approved: float) -> float:
    """Calculate request reduction using only guardrail-approved values."""
    if current == 0:
        return 0.0
    return (current - approved) / current * 100


def _status(cpu_approved: bool, memory_approved: bool) -> str:
    """Return a workload-level status from both resource decisions."""
    if cpu_approved and memory_approved:
        return "approved"
    if cpu_approved or memory_approved:
        return "partial"
    return "rejected"


def _effective_confidence(
    status: str,
    model_confidence: str,
    cpu_confidence: str,
    memory_confidence: str,
) -> str:
    """Combine advisory and deterministic confidence without hiding either."""
    if status == "rejected":
        return "manual review"
    if "low" in {model_confidence, cpu_confidence, memory_confidence}:
        return "low"
    return model_confidence


def build_report_rows(
    stats_records: list[dict[str, Any]],
    recommendation_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join statistics and recommendations, sorting by approved savings."""
    stats_by_name = _index_records(stats_records, "name", "stats")
    recommendations_by_name = _index_records(
        recommendation_records, "workload", "recommendation"
    )
    if set(stats_by_name) != set(recommendations_by_name):
        missing = sorted(set(stats_by_name) - set(recommendations_by_name))
        unexpected = sorted(set(recommendations_by_name) - set(stats_by_name))
        raise ValueError(
            "Stats and recommendations contain different workloads "
            f"(missing={missing}, unexpected={unexpected})"
        )

    rows = []
    for name, stats in stats_by_name.items():
        result = recommendations_by_name[name]
        proposal = result.get("recommendation", {})
        guardrails = result.get("guardrails", {})
        cpu_decision = guardrails.get("cpu", {})
        memory_decision = guardrails.get("memory", {})

        current_cpu = _number(stats.get("requested_cpu_cores"), "current CPU")
        current_memory = _number(
            stats.get("requested_memory_bytes"), "current memory"
        )
        proposed_cpu = _number(proposal.get("recommended_cpu"), "proposed CPU")
        proposed_memory = _number(
            proposal.get("recommended_memory"), "proposed memory"
        )
        decision_cpu = _number(
            cpu_decision.get("approved_value"), "approved CPU"
        )
        decision_memory = _number(
            memory_decision.get("approved_value"), "approved memory"
        )
        cpu_approved = cpu_decision.get("approved") is True
        memory_approved = memory_decision.get("approved") is True
        # A rejected decision always displays the current request. This second
        # enforcement protects the report even if saved JSON was manually edited.
        approved_cpu = decision_cpu if cpu_approved else current_cpu
        approved_memory = decision_memory if memory_approved else current_memory
        if approved_cpu > current_cpu or approved_memory > current_memory:
            raise ValueError(
                f"Guardrail-approved values cannot grow requests for {name}"
            )
        status = _status(cpu_approved, memory_approved)
        cpu_savings = _savings_percent(current_cpu, approved_cpu)
        memory_savings = _savings_percent(current_memory, approved_memory)
        model_confidence = str(proposal.get("confidence", "unknown"))
        cpu_confidence = str(cpu_decision.get("confidence", "unknown"))
        memory_confidence = str(memory_decision.get("confidence", "unknown"))

        rows.append(
            {
                "name": name,
                "namespace": stats.get("namespace", "unknown"),
                "criticality": stats.get("criticality", "unknown"),
                "status": status,
                "current_cpu": current_cpu,
                "proposed_cpu": proposed_cpu,
                "approved_cpu": approved_cpu,
                "cpu_savings_percent": cpu_savings,
                "current_memory": current_memory,
                "proposed_memory": proposed_memory,
                "approved_memory": approved_memory,
                "memory_savings_percent": memory_savings,
                "savings_score": (cpu_savings + memory_savings) / 2,
                "model_confidence": model_confidence,
                "cpu_confidence": cpu_confidence,
                "memory_confidence": memory_confidence,
                "effective_confidence": _effective_confidence(
                    status,
                    model_confidence,
                    cpu_confidence,
                    memory_confidence,
                ),
                "reasoning": proposal.get("reasoning", ""),
                "cpu_reason": cpu_decision.get("reason", "missing decision"),
                "memory_reason": memory_decision.get(
                    "reason", "missing decision"
                ),
                "sample_count": stats.get("sample_count", 0),
                "restart_count": stats.get("restart_count", 0),
                "last_restart_oomkilled": stats.get(
                    "last_restart_oomkilled", False
                ),
                "model": result.get("model", "unknown"),
                "tool_trace": result.get("tool_trace", []),
            }
        )

    # Show the largest approved opportunity first; names break equal-score ties
    # so repeated renders have stable ordering.
    return sorted(rows, key=lambda row: (-row["savings_score"], row["name"]))


def _format_cpu(value: float) -> str:
    """Format a numeric CPU value as cores for human-readable output."""
    return f"{value:.3f} cores"


def _format_memory(value: float) -> str:
    """Format byte values with Kubernetes-friendly binary memory units."""
    gibibyte = 1024**3
    mebibyte = 1024**2
    if value >= gibibyte:
        return f"{value / gibibyte:.2f} GiB"
    return f"{value / mebibyte:.1f} MiB"


def _format_percent(value: float) -> str:
    """Format a percentage consistently throughout the report."""
    return f"{value:.1f}%"


def _escape(value: Any) -> str:
    """Escape provider and workload text before inserting it into HTML."""
    return html.escape(str(value), quote=True)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate portfolio-level totals from approved values."""
    # Aggregate raw values before calculating percentages; averaging individual
    # percentages would give tiny and large workloads equal weight.
    current_cpu = sum(row["current_cpu"] for row in rows)
    approved_cpu = sum(row["approved_cpu"] for row in rows)
    current_memory = sum(row["current_memory"] for row in rows)
    approved_memory = sum(row["approved_memory"] for row in rows)
    return {
        "workloads": len(rows),
        "approved": sum(row["status"] == "approved" for row in rows),
        "review": sum(row["status"] != "approved" for row in rows),
        "cpu_savings": _savings_percent(current_cpu, approved_cpu),
        "memory_savings": _savings_percent(current_memory, approved_memory),
    }


def render_html(
    rows: list[dict[str, Any]],
    settings: ReportSettings,
    generated_at: datetime | None = None,
) -> str:
    """Render escaped report rows into one portable HTML document."""
    generated_at = generated_at or datetime.now(timezone.utc)
    summary = _summary(rows)
    generated_label = generated_at.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    # The summary table gives a quick scan. Detail cards retain the proposal,
    # deterministic reasons, and tool trace needed to explain each result.
    table_rows = []
    detail_cards = []
    for row in rows:
        status_label = row["status"].replace("partial", "partial approval").title()
        table_rows.append(
            f"""
            <tr>
              <td><strong>{_escape(row['name'])}</strong><br>
                <span class="muted">{_escape(row['criticality'])}</span></td>
              <td><span class="badge {row['status']}">{status_label}</span></td>
              <td>{_format_cpu(row['current_cpu'])}<br>
                <span class="arrow">→</span> {_format_cpu(row['approved_cpu'])}</td>
              <td>{_format_memory(row['current_memory'])}<br>
                <span class="arrow">→</span> {_format_memory(row['approved_memory'])}</td>
              <td><strong>{_format_percent(row['cpu_savings_percent'])}</strong> CPU<br>
                <strong>{_format_percent(row['memory_savings_percent'])}</strong> memory</td>
              <td>{_escape(row['effective_confidence'])}</td>
            </tr>"""
        )

        incident_text = (
            f"{row['restart_count']} restarts"
            + (", latest OOMKilled" if row["last_restart_oomkilled"] else "")
        )
        trace = _escape(json.dumps(row["tool_trace"], indent=2))
        detail_cards.append(
            f"""
            <article class="workload-card">
              <div class="card-heading">
                <div><h3>{_escape(row['name'])}</h3>
                  <p>{_escape(row['namespace'])} · {_escape(row['criticality'])} ·
                    {_escape(row['model'])}</p></div>
                <span class="badge {row['status']}">{status_label}</span>
              </div>
              <div class="proposal-grid">
                <div><span>CPU</span><strong>{_format_cpu(row['current_cpu'])} →
                  {_format_cpu(row['approved_cpu'])}</strong>
                  <small>LLM proposed {_format_cpu(row['proposed_cpu'])}</small></div>
                <div><span>Memory</span><strong>{_format_memory(row['current_memory'])} →
                  {_format_memory(row['approved_memory'])}</strong>
                  <small>LLM proposed {_format_memory(row['proposed_memory'])}</small></div>
                <div><span>Confidence</span><strong>{_escape(row['effective_confidence'])}</strong>
                  <small>LLM {_escape(row['model_confidence'])}; guardrails CPU
                    {_escape(row['cpu_confidence'])}, memory
                    {_escape(row['memory_confidence'])}</small></div>
                <div><span>Evidence</span><strong>{_escape(row['sample_count'])} samples</strong>
                  <small>{_escape(incident_text)}</small></div>
              </div>
              <div class="reasoning"><h4>LLM reasoning</h4>
                <p>{_escape(row['reasoning'])}</p></div>
              <div class="decisions"><p><strong>CPU:</strong>
                {_escape(row['cpu_reason'])}</p><p><strong>Memory:</strong>
                {_escape(row['memory_reason'])}</p></div>
              <details><summary>Read-only tool trace ({len(row['tool_trace'])} calls)</summary>
                <pre>{trace}</pre></details>
            </article>"""
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(settings.title)}</title>
  <style>
    :root {{ --ink:#172033; --muted:#687086; --line:#dfe3eb; --panel:#fff;
      --bg:#f4f6fa; --accent:#2257d6; --good:#176b45; --warn:#946200;
      --bad:#9e2f2f; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:var(--bg);
      font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(1180px,calc(100% - 32px)); margin:32px auto 64px; }}
    header {{ color:#fff; padding:34px; border-radius:18px;
      background:linear-gradient(135deg,#172554,#2257d6); box-shadow:0 14px 35px #17255426; }}
    h1,h2,h3,h4,p {{ margin-top:0; }}
    header p {{ max-width:760px; margin-bottom:0; color:#dce7ff; }}
    .generated {{ margin-top:16px; font-size:13px; }}
    .summary {{ display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin:20px 0; }}
    .metric,.table-wrap,.workload-card {{ background:var(--panel); border:1px solid var(--line);
      border-radius:14px; box-shadow:0 5px 18px #1720330a; }}
    .metric {{ padding:18px; }} .metric span,.proposal-grid span {{ color:var(--muted);
      display:block; font-size:12px; letter-spacing:.04em; text-transform:uppercase; }}
    .metric strong {{ display:block; font-size:26px; margin-top:4px; }}
    .table-wrap {{ overflow:auto; margin-bottom:28px; }}
    table {{ width:100%; border-collapse:collapse; min-width:850px; }}
    th,td {{ padding:14px 16px; text-align:left; border-bottom:1px solid var(--line); }}
    th {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
    tr:last-child td {{ border-bottom:0; }} .muted,.arrow,small {{ color:var(--muted); }}
    .badge {{ display:inline-block; border-radius:999px; padding:4px 9px;
      font-size:12px; font-weight:700; white-space:nowrap; }}
    .badge.approved {{ color:var(--good); background:#e8f5ee; }}
    .badge.partial {{ color:var(--warn); background:#fff4d8; }}
    .badge.rejected {{ color:var(--bad); background:#fdecec; }}
    .workload-card {{ padding:22px; margin:14px 0; }}
    .card-heading {{ display:flex; justify-content:space-between; gap:16px; }}
    .card-heading h3 {{ margin-bottom:2px; }} .card-heading p {{ color:var(--muted); }}
    .proposal-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }}
    .proposal-grid > div {{ padding:13px; border-radius:10px; background:#f7f8fb; }}
    .proposal-grid strong,.proposal-grid small {{ display:block; margin-top:5px; }}
    .reasoning,.decisions {{ margin-top:18px; }} .reasoning h4 {{ margin-bottom:5px; }}
    .decisions {{ padding:12px 14px; border-left:4px solid var(--accent); background:#f4f7ff; }}
    .decisions p:last-child {{ margin-bottom:0; }}
    details {{ margin-top:14px; }} summary {{ color:var(--accent); cursor:pointer; font-weight:650; }}
    pre {{ overflow:auto; padding:14px; border-radius:9px; background:#111827; color:#dbeafe;
      font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }}
    footer {{ margin-top:28px; color:var(--muted); text-align:center; }}
    @media (max-width:850px) {{ .summary {{ grid-template-columns:repeat(2,1fr); }}
      .proposal-grid {{ grid-template-columns:repeat(2,1fr); }} }}
    @media (max-width:520px) {{ main {{ width:min(100% - 18px,1180px); margin-top:9px; }}
      header {{ padding:24px; }} .summary,.proposal-grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body><main>
  <header><h1>{_escape(settings.title)}</h1>
    <p>{_escape(settings.subtitle)}</p>
    <p class="generated">Generated {_escape(generated_label)}</p></header>
  <section class="summary" aria-label="Summary">
    <div class="metric"><span>Workloads</span><strong>{summary['workloads']}</strong></div>
    <div class="metric"><span>Fully approved</span><strong>{summary['approved']}</strong></div>
    <div class="metric"><span>Need review</span><strong>{summary['review']}</strong></div>
    <div class="metric"><span>CPU request saving</span><strong>{_format_percent(summary['cpu_savings'])}</strong></div>
    <div class="metric"><span>Memory request saving</span><strong>{_format_percent(summary['memory_savings'])}</strong></div>
  </section>
  <h2>Recommendations by approved savings potential</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Workload</th><th>Decision</th><th>CPU request</th>
      <th>Memory request</th><th>Savings</th><th>Confidence</th></tr></thead>
    <tbody>{''.join(table_rows)}</tbody>
  </table></div>
  <h2>Decision details</h2>
  {''.join(detail_cards)}
  <footer>All approved values come from deterministic guardrails. No cluster changes were made.</footer>
</main></body></html>
"""

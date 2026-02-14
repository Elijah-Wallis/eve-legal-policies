from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any


_TYPE_RE = re.compile(r"^#\s*TYPE\s+([a-zA-Z_:][a-zA-Z0-9_:]*)\s+(counter|gauge|histogram)\s*$")
_SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+]?[0-9]+(?:\.[0-9]+)?)$")
_LE_RE = re.compile(r'le="([^"]+)"')


def parse_prometheus_text(text: str) -> tuple[dict[str, float], dict[str, float], dict[str, dict[str, float]]]:
    types: dict[str, str] = {}
    counters: dict[str, float] = {}
    gauges: dict[str, float] = {}
    hist_buckets: dict[str, dict[str, float]] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m_type = _TYPE_RE.match(line)
        if m_type:
            types[m_type.group(1)] = m_type.group(2)
            continue
        if line.startswith("#"):
            continue

        m_sample = _SAMPLE_RE.match(line)
        if not m_sample:
            continue
        name = m_sample.group(1)
        labels = m_sample.group(2) or ""
        value = float(m_sample.group(3))

        if name.endswith("_bucket"):
            base = name[: -len("_bucket")]
            m_le = _LE_RE.search(labels)
            if m_le is None:
                continue
            le = m_le.group(1)
            hist_buckets.setdefault(base, {})[le] = value
            continue

        t = types.get(name, "")
        if t == "counter":
            counters[name] = value
        elif t == "gauge":
            gauges[name] = value

    return counters, gauges, hist_buckets


def histogram_quantile_from_buckets(buckets: dict[str, float], q: float) -> float | None:
    if not buckets:
        return None
    items: list[tuple[float, float]] = []
    inf_count: float | None = None
    for le_str, count in buckets.items():
        if le_str == "+Inf":
            inf_count = float(count)
            continue
        try:
            items.append((float(le_str), float(count)))
        except Exception:
            continue
    items.sort(key=lambda x: x[0])
    if inf_count is None:
        if not items:
            return None
        inf_count = items[-1][1]
    if inf_count <= 0:
        return None

    target = max(1.0, math.ceil(float(q) * float(inf_count)))
    for le, cumulative in items:
        if cumulative >= target:
            return le
    if items:
        return items[-1][0]
    return None


def _state_for_threshold(value: float | None, *, target: float, op: str) -> str:
    if value is None:
        return "unknown"
    if op == "lte":
        return "pass" if value <= target else "fail"
    if op == "eq":
        return "pass" if value == target else "fail"
    return "unknown"


def build_dashboard_summary(metrics_text: str) -> dict[str, Any]:
    counters, gauges, hists = parse_prometheus_text(metrics_text)

    ack_p95 = histogram_quantile_from_buckets(hists.get("vic_turn_final_to_ack_segment_ms", {}), 0.95)
    first_p95 = histogram_quantile_from_buckets(hists.get("vic_turn_final_to_first_segment_ms", {}), 0.95)
    cancel_p95 = histogram_quantile_from_buckets(hists.get("vic_barge_in_cancel_latency_ms", {}), 0.95)

    checks = [
        {
            "id": "ack_p95",
            "title": "ACK latency p95",
            "target": "<=300ms",
            "value": ack_p95,
            "state": _state_for_threshold(ack_p95, target=300, op="lte"),
            "laymen": "How fast Eve acknowledges users.",
            "technical": "vic_turn_final_to_ack_segment_ms p95",
            "fix": "Inspect queue pressure and writer backpressure timeout metrics.",
        },
        {
            "id": "first_content_p95",
            "title": "First response p95",
            "target": "<=700ms",
            "value": first_p95,
            "state": _state_for_threshold(first_p95, target=700, op="lte"),
            "laymen": "How fast Eve starts giving real content.",
            "technical": "vic_turn_final_to_first_segment_ms p95",
            "fix": "Reduce tool latency and model timeout/filler thresholds.",
        },
        {
            "id": "barge_cancel_p95",
            "title": "Barge-in cancel p95",
            "target": "<=250ms",
            "value": cancel_p95,
            "state": _state_for_threshold(cancel_p95, target=250, op="lte"),
            "laymen": "How fast Eve stops talking when user interrupts.",
            "technical": "vic_barge_in_cancel_latency_ms p95",
            "fix": "Tune interruption sensitivity and cancel path latency.",
        },
        {
            "id": "reasoning_leak",
            "title": "Reasoning leakage",
            "target": "==0",
            "value": int(counters.get("voice_reasoning_leak_total", 0)),
            "state": _state_for_threshold(float(counters.get("voice_reasoning_leak_total", 0)), target=0, op="eq"),
            "laymen": "Internal chain-of-thought is not exposed to users.",
            "technical": "voice_reasoning_leak_total",
            "fix": "Keep plain-language policy and guardrail transforms enabled.",
        },
        {
            "id": "jargon_violation",
            "title": "Jargon violations",
            "target": "==0",
            "value": int(counters.get("voice_jargon_violation_total", 0)),
            "state": _state_for_threshold(float(counters.get("voice_jargon_violation_total", 0)), target=0, op="eq"),
            "laymen": "Eve responses stay understandable.",
            "technical": "voice_jargon_violation_total",
            "fix": "Adjust readability filters and phrasing templates.",
        },
    ]

    failing = sum(1 for c in checks if c["state"] == "fail")
    passing = sum(1 for c in checks if c["state"] == "pass")
    unknown = sum(1 for c in checks if c["state"] == "unknown")

    status = "green"
    if failing > 0:
        status = "red"
    elif passing == 0:
        status = "gray"

    skills_inv = int(counters.get("skills_invocations_total", 0))
    skills_hit = int(counters.get("skills_hit_total", 0))
    skills_hit_rate_pct = round((skills_hit / skills_inv) * 100.0, 1) if skills_inv > 0 else None

    return {
        "status": status,
        "checks": checks,
        "totals": {
            "passing": passing,
            "failing": failing,
            "unknown": unknown,
        },
        "memory": {
            "transcript_chars_current": int(gauges.get("memory_transcript_chars_current", 0)),
            "transcript_utterances_current": int(gauges.get("memory_transcript_utterances_current", 0)),
        },
        "skills": {
            "invocations_total": skills_inv,
            "hit_total": skills_hit,
            "hit_rate_pct": skills_hit_rate_pct,
            "error_total": int(counters.get("skills_error_total", 0)),
        },
        "shell": {
            "exec_total": int(counters.get("shell_exec_total", 0)),
            "exec_denied_total": int(counters.get("shell_exec_denied_total", 0)),
            "exec_timeout_total": int(counters.get("shell_exec_timeout_total", 0)),
        },
        "self_improve": {
            "cycles_total": int(counters.get("self_improve_cycles_total", 0)),
            "proposals_total": int(counters.get("self_improve_proposals_total", 0)),
            "applies_total": int(counters.get("self_improve_applies_total", 0)),
            "blocked_on_gates_total": int(counters.get("self_improve_blocked_on_gates_total", 0)),
        },
        "context": {
            "compactions_total": int(counters.get("context_compactions_total", 0)),
            "compaction_tokens_saved_total": int(counters.get("context_compaction_tokens_saved_total", 0)),
        },
    }


def build_repo_map(repo_root: Path) -> dict[str, Any]:
    components = [
        {
            "id": "runtime_core",
            "title": "Runtime Core",
            "path": "app/",
            "laymen": "The live brain that takes calls and responds.",
            "technical": "FastAPI server, orchestrator, policy, tool routing, metrics.",
        },
        {
            "id": "automation_scripts",
            "title": "Automation Scripts",
            "path": "scripts/",
            "laymen": "Operational commands that keep Eve healthy.",
            "technical": "Acceptance runners, scorecards, self-improve cycle, metrics tools.",
        },
        {
            "id": "tests_contracts",
            "title": "Tests and Contracts",
            "path": "tests/",
            "laymen": "Proof that behavior is stable and safe.",
            "technical": "Unit, contract, replay, latency, policy, and regression tests.",
        },
        {
            "id": "skills_library",
            "title": "Skills Library",
            "path": "skills/",
            "laymen": "Reusable methods Eve can apply to solve tasks faster.",
            "technical": "Markdown skill artifacts loaded and injected by retriever.",
        },
        {
            "id": "knowledge_docs",
            "title": "Knowledge and SOP",
            "path": "docs/",
            "laymen": "How the system is operated and improved safely.",
            "technical": "Runbooks, self-improve SOP, and operational references.",
        },
    ]

    for c in components:
        p = repo_root / c["path"]
        c["exists"] = p.exists()
        c["files"] = sum(1 for _ in p.rglob("*") if _.is_file()) if p.exists() else 0

    top_level = []
    for p in sorted(repo_root.iterdir(), key=lambda x: x.name.lower()):
        if p.name.startswith("."):
            continue
        if p.name in {".venv", "retell_ws_brain.egg-info", "__pycache__"}:
            continue
        top_level.append({
            "name": p.name,
            "type": "dir" if p.is_dir() else "file",
        })

    sop_docs = [
        "docs/self_improve_sop.md",
        "README.md",
        "soul.md",
    ]

    return {
        "repo_root": str(repo_root),
        "components": components,
        "top_level": top_level,
        "sop_docs": sop_docs,
    }

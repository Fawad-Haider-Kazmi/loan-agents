"""Loan-officer dashboard (Flask). The human-in-the-loop surface of the system.

Shows every case, the agents' recommendation with the evidence behind it, and
lets an officer approve / reject / reduce the amount. Also lets operations retry
failed cases and record repayment outcomes (the feedback loop's input).

Prototype notes: no authentication or CSRF protection. Put it behind your
organisation's SSO and add CSRF tokens before using it for anything real.
"""
from __future__ import annotations

import os
import random
import re
from typing import Optional

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from jinja2 import DictLoader

from .ml.feedback_learning import RetrainResult
from .models import CreditReport, Document, DocumentType, LoanApplication
from .samples import Scenario
from .simulation import next_sim_index, simulate_applications, simulate_outcomes
from .state import ApplicationStatus as S
from .system import LoanProcessingSystem
from .utils import money

BASE = """<!doctype html><html><head><meta charset="utf-8"><title>Loan Officer Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2433;--mute:#667085;--line:#e4e7ec;--brand:#2459d3}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}
header{background:#101828;color:#fff;padding:0 28px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap}
header a{color:#fff;text-decoration:none;font-weight:600}
nav{display:flex;gap:4px}nav a{padding:16px 14px;font-weight:500;color:#cbd5e1;border-bottom:3px solid transparent}
nav a.on{color:#fff;border-bottom-color:#5b8def}nav a:hover{color:#fff}
.agentgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.agent{border:1px solid var(--line);border-radius:9px;padding:12px 14px;background:#fbfbfd}.agent b{display:block}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}.three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
@media(max-width:800px){.two,.three{grid-template-columns:1fr}}
pre.mermaid{background:#fff;text-align:center;overflow-x:auto}
textarea.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}main{max-width:1100px;margin:24px auto;padding:0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:18px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}.mute{color:var(--mute)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600}
.s-awaiting_approval{background:#fff4d6;color:#8a5a00}.s-completed{background:#dcf5e4;color:#14633a}
.s-failed,.s-reconciliation_required{background:#fde2e1;color:#a11d17}.s-needs_info{background:#e7ecf5;color:#33415c}
.s-rejected{background:#fde2e1;color:#a11d17}.s-approved,.s-disbursed,.s-verified{background:#dcf5e4;color:#14633a}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}.stat b{font-size:24px;display:block}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:800px){.grid{grid-template-columns:1fr}}
button{background:var(--brand);color:#fff;border:0;border-radius:7px;padding:8px 14px;font-weight:600;cursor:pointer}
button.alt{background:#fff;color:var(--ink);border:1px solid var(--line)}button.danger{background:#b42318}
input,select,textarea{width:100%;padding:8px;border:1px solid var(--line);border-radius:7px;font:inherit;margin:4px 0 10px}
pre{background:#f2f4f7;padding:10px;border-radius:7px;white-space:pre-wrap;margin:0}
.flash{background:#e8f0fe;border:1px solid #c5d6fa;padding:10px 14px;border-radius:8px;margin-bottom:16px}
.pill-block{background:#ffe3e1;color:#a11d17}.pill-warn{background:#fff4d6;color:#8a5a00}
a{color:var(--brand)}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
</style></head><body>
<header><a href="{{ url_for('index') }}" style="padding:16px 0">Multi-Agent Loan Processing</a>
<nav>{% for ep, label in [('index','Cases'),('submit','New application'),('workflow','Workflow &amp; agents'),('learning','Learning')] %}
<a href="{{ url_for(ep) }}" class="{{ 'on' if request.endpoint == ep or (ep == 'index' and request.endpoint == 'case') else '' }}">{{ label|safe }}</a>{% endfor %}</nav></header>
<main>{% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}
{% block body %}{% endblock %}</main>{% block scripts %}{% endblock %}</body></html>"""

INDEX = """{% extends 'base.html' %}{% block body %}
<div class="row" style="justify-content:space-between;margin-bottom:14px">
  <div><h1>Cases</h1><span class="mute">Agents prepare the case; you make the call where policy requires a human.</span></div>
  {% if can_load_demo %}<form method="post" action="{{ url_for('load_demo') }}"><button class="alt">Load demo applications</button></form>{% endif %}
</div>
<div class="stats">
  <div class="stat"><b>{{ counts.pending }}</b>awaiting your approval</div>
  <div class="stat"><b>{{ counts.completed }}</b>completed</div>
  <div class="stat"><b>{{ counts.attention }}</b>need attention (failed / reconcile / info)</div>
  <div class="stat"><b>{{ counts.total }}</b>total</div>
</div>
<div class="card"><h2>Awaiting approval</h2>
{% if pending %}<table><tr><th>Case</th><th>Applicant</th><th>Amount</th><th>Agent recommendation</th><th></th></tr>
{% for c in pending %}<tr><td>{{ c.application_id }}</td><td>{{ c.application.applicant_name }}</td>
<td>{{ money(c.application.requested_amount) }}</td>
<td><b>{{ c.decision.recommendation.value|upper }}</b><div class="mute">{{ c.decision.reasons[0] }}</div></td>
<td><a href="{{ url_for('case', application_id=c.application_id) }}"><button>Review</button></a></td></tr>{% endfor %}</table>
{% else %}<span class="mute">Nothing waiting. </span>{% endif %}</div>
<div class="card"><h2>All cases</h2>
{% if cases %}<table><tr><th>Case</th><th>Applicant</th><th>Amount</th><th>Status</th><th>Outcome</th></tr>
{% for c in cases %}<tr><td><a href="{{ url_for('case', application_id=c.application_id) }}">{{ c.application_id }}</a></td>
<td>{{ c.application.applicant_name }}</td><td>{{ money(c.application.requested_amount) }}</td>
<td><span class="badge s-{{ c.status.value }}">{{ c.status.value.replace('_',' ') }}</span></td>
<td class="mute">{{ c.final_report.outcome if c.final_report else '' }}</td></tr>{% endfor %}</table>
{% else %}<span class="mute">No cases yet.</span>{% endif %}</div>{% endblock %}"""

CASE = """{% extends 'base.html' %}{% block body %}
<div class="row" style="justify-content:space-between"><div>
<h1>{{ c.application_id }} &middot; {{ c.application.applicant_name }}</h1>
<span class="badge s-{{ c.status.value }}">{{ c.status.value.replace('_',' ') }}</span>
<span class="mute">&nbsp;{{ money(c.application.requested_amount) }} over {{ c.application.tenure_months }} months</span></div>
<a href="{{ url_for('index') }}">&larr; all cases</a></div><br>

{% if c.decision %}<div class="card"><h2>Agent recommendation</h2>
<p><b>{{ c.decision.recommendation.value|upper }}</b> &middot; human sign-off {{ 'required' if c.decision.requires_human else 'not required' }}
&middot; <span class="mute">{{ c.decision.policy_version }}</span></p>
<ul>{% for r in c.decision.reasons %}<li>{{ r }}</li>{% endfor %}</ul>
<pre>{{ c.decision.explanation }}</pre></div>{% endif %}

{% if c.status.value == 'awaiting_approval' %}<div class="card" style="border-color:#f0c36d"><h2>Your decision</h2>
<form method="post" action="{{ url_for('review', application_id=c.application_id) }}">
<label>Your name (recorded in the audit trail)</label><input name="reviewer" required>
<label>Approved amount (you may reduce it, never raise it)</label>
<input name="amount" type="number" step="1000" value="{{ c.decision.approved_amount|int }}" max="{{ c.application.requested_amount|int }}">
<label>Notes</label><textarea name="notes" rows="2"></textarea>
<div class="row"><button name="decision" value="approve">Approve &amp; disburse</button>
<button name="decision" value="reject" class="danger">Reject</button></div></form></div>{% endif %}

{% if c.status.value == 'failed' %}<div class="card" style="border-color:#f1a29b"><h2>Case failed</h2>
<p>Node <b>{{ c.failed_node }}</b>: {{ c.error }}</p>
<form method="post" action="{{ url_for('retry', application_id=c.application_id) }}"><button>Retry from {{ c.failed_node }}</button></form></div>{% endif %}

{% if c.receipt and c.status.value == 'completed' and not outcome_recorded %}<div class="card"><h2>Record repayment outcome (feeds model learning)</h2>
<form method="post" action="{{ url_for('outcome', application_id=c.application_id) }}" class="row">
<button name="result" value="repaid" class="alt">Loan repaid</button><button name="result" value="defaulted" class="danger">Loan defaulted</button></form></div>{% endif %}

<div class="grid">
{% if c.risk %}<div class="card"><h2>Credit risk</h2><table>
<tr><td>Credit score</td><td>{{ c.risk.credit_report.score }}</td></tr>
<tr><td>Verified income</td><td>{{ money(c.risk.verified_income) }} / month</td></tr>
<tr><td>New instalment (EMI)</td><td>{{ money(c.risk.requested_emi) }}</td></tr>
<tr><td>Debt-to-income</td><td>{{ '%.0f'|format(c.risk.dti*100) }}%</td></tr>
<tr><td>Default probability</td><td>{{ '%.1f'|format(c.risk.default_probability*100) }}% ({{ c.risk.risk_band.value }}) &middot; <span class="mute">model {{ c.risk.model_version }}</span></td></tr>
</table></div>{% endif %}
{% if c.fraud %}<div class="card"><h2>Fraud screening: {{ c.fraud.level.value }}</h2>
{% if c.fraud.signals %}<table>{% for s in c.fraud.signals %}<tr><td><b>{{ s.code }}</b></td><td>{{ s.message }}</td></tr>{% endfor %}</table>
{% else %}<span class="mute">No signals.</span>{% endif %}</div>{% endif %}
{% if c.validation %}<div class="card"><h2>Document validation</h2>
{% if c.validation.missing_documents %}<p><b>Missing:</b> {{ c.validation.missing_documents|join(', ') }}</p>{% endif %}
{% for i in c.validation.issues %}<p><span class="badge {{ 'pill-block' if i.severity.value=='blocking' else 'pill-warn' }}">{{ i.severity.value }}</span> {{ i.message }}</p>
{% else %}{% if not c.validation.missing_documents %}<span class="mute">All checks passed.</span>{% endif %}{% endfor %}</div>{% endif %}
{% if c.receipt %}<div class="card"><h2>Disbursement</h2><table>
<tr><td>Transaction</td><td>{{ c.receipt.transaction_id }}</td></tr><tr><td>Amount</td><td>{{ money(c.receipt.amount) }}</td></tr>
<tr><td>Account</td><td>{{ c.receipt.account_number }}</td></tr>
<tr><td>Verification</td><td>{% if c.verification %}{{ 'ledger matches' if c.verification.matched else 'MISMATCH: ' + c.verification.discrepancies|join('; ') }}{% endif %}</td></tr></table></div>{% endif %}
</div>

{% if c.human_review %}<div class="card"><h2>Human review</h2>
<p>{{ c.human_review.reviewer }} {{ 'approved' if c.human_review.approved else 'rejected' }}
{% if c.human_review.approved %}{{ money(c.human_review.approved_amount) }}{% endif %}. <span class="mute">{{ c.human_review.notes }}</span></p></div>{% endif %}
{% if c.final_report %}<div class="card"><h2>Applicant notice</h2><pre>{{ c.final_report.applicant_notice }}</pre></div>{% endif %}

<div class="grid"><div class="card"><h2>Agent conversation</h2><table>
{% for m in messages %}<tr><td class="mute">{{ m.kind }}</td><td><b>{{ m.sender }}</b> &rarr; {{ m.recipient }}<div class="mute">{{ m.summary }}</div></td></tr>{% endfor %}</table></div>
<div class="card"><h2>Audit trail</h2><table>
{% for a in audit %}<tr><td class="mute">{{ a.ts[11:19] }}</td><td><b>{{ a.actor }}</b> {{ a.event.replace('_',' ') }}<div class="mute">{{ a.detail }}</div></td></tr>{% endfor %}</table></div></div>
{% endblock %}"""

SUBMIT = """{% extends 'base.html' %}{% block body %}
<h1>New application</h1>
<span class="mute">Submit a loan application and watch the agents process it. Documents are the text an OCR step would return.</span>
<div class="row" style="margin:12px 0"><span class="mute">Prefill from a demo scenario:</span>
{% for sc in scenarios %}<a href="{{ url_for('submit', sample=sc.application.application_id) }}"><button type="button" class="alt">{{ sc.application.applicant_name }}</button></a>{% endfor %}</div>
{% if sample %}<div class="flash"><b>{{ sample.expectation }}</b></div>{% endif %}
<form method="post" action="{{ url_for('submit') }}">
<div class="card"><h2>Applicant</h2><div class="three">
<div><label>Full name</label><input name="name" required value="{{ f.name }}"></div>
<div><label>National ID</label><input name="national_id" required value="{{ f.national_id }}"></div>
<div><label>Disbursement account</label><input name="account" required value="{{ f.account }}"></div>
<div><label>Employer</label><input name="employer" required value="{{ f.employer }}"></div>
<div><label>Stated monthly income (PKR)</label><input name="income" type="number" required value="{{ f.income }}"></div>
<div><label>Loan amount (PKR)</label><input name="amount" type="number" required value="{{ f.amount }}"></div>
<div><label>Tenure (months)</label><input name="tenure" type="number" required value="{{ f.tenure }}"></div></div></div>
<div class="card"><h2>Simulated credit bureau profile</h2>
<span class="mute">Stands in for the real bureau API. Tick "no history" to see the thin-file path.</span>
<div class="three" style="margin-top:8px">
<div><label>Credit score</label><input name="score" type="number" value="{{ f.score }}"></div>
<div><label>Existing monthly obligations</label><input name="obligations" type="number" value="{{ f.obligations }}"></div>
<div><label>Delinquencies</label><input name="delinquencies" type="number" value="{{ f.delinquencies }}"></div>
<div><label>Utilisation (0 to 1)</label><input name="utilization" type="number" step="0.05" value="{{ f.utilization }}"></div>
<div><label><input type="checkbox" name="thin" style="width:auto" {{ 'checked' if f.thin }}> No bureau history</label></div></div></div>
<div class="card"><h2>Documents</h2><span class="mute">Leave one empty to see the missing-document path.</span>
<div class="three" style="margin-top:8px">
<div><label>National ID</label><textarea class="mono" name="doc_id" rows="6">{{ f.doc_id }}</textarea></div>
<div><label>Salary slip</label><textarea class="mono" name="doc_salary" rows="6">{{ f.doc_salary }}</textarea></div>
<div><label>Bank statement</label><textarea class="mono" name="doc_bank" rows="6">{{ f.doc_bank }}</textarea></div></div></div>
<button>Submit to the agents</button></form>{% endblock %}"""

WORKFLOW = """{% extends 'base.html' %}{% block body %}
<h1>Workflow &amp; agents</h1><span class="mute">How the system is built. Nothing here is mocked up: the agents listed are the live objects, and the state list is read from the code.</span><br><br>
<div class="card"><h2>The eight agents</h2><div class="agentgrid">
{% for key, a in agents %}<div class="agent"><b>{{ loop.index }}. {{ a.name }}</b><span class="mute">{{ a.purpose }}</span></div>{% endfor %}</div></div>
<div class="card"><h2>Current policy</h2><div class="two"><table>
<tr><td>Straight-through limit</td><td>{{ money(policy.auto_approve_limit) }}</td></tr>
<tr><td>Max debt-to-income</td><td>{{ '%.0f'|format(policy.max_dti*100) }}%</td></tr>
<tr><td>Auto-reject at default probability</td><td>{{ '%.0f'|format(policy.reject_pd*100) }}%</td></tr>
<tr><td>Escalate from default probability</td><td>{{ '%.0f'|format(policy.approve_pd*100) }}%</td></tr></table>
<table><tr><td>Policy version</td><td>{{ policy.version }}</td></tr><tr><td>Live risk model</td><td>{{ model.version }} (trained on {{ model.trained_on }} rows)</td></tr>
<tr><td>Retry policy</td><td>{{ retries }} attempts, exponential back-off</td></tr></table></div></div>
{% for title, code, note in diagrams %}<div class="card"><h2>{{ title }}</h2><pre class="mermaid">{{ code }}</pre><span class="mute">{{ note }}</span></div>
{% else %}<div class="card">Diagram source docs/ARCHITECTURE.md was not found.</div>{% endfor %}{% endblock %}
{% block scripts %}<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";
mermaid.initialize({startOnLoad:true, securityLevel:"loose"});
</script>{% endblock %}"""

LEARNING = """{% extends 'base.html' %}{% block body %}
<h1>Learning from outcomes</h1>
<span class="mute">The Reporting/Feedback agent proposes changes from real repayment results. Nothing goes live until a named person approves it.</span><br><br>
<div class="stats">
<div class="stat"><b>{{ n_feedback }}</b>repayment outcomes recorded</div>
<div class="stat"><b>{{ model.version }}</b>live risk model</div>
<div class="stat"><b>{{ '%.0f'|format(policy.reject_pd*100) }}%</b>auto-reject threshold</div>
<div class="stat"><b>{{ min_needed }}</b>outcomes needed to retrain</div></div>

<div class="card"><h2>1. Get some data</h2>
<p class="mute">Real outcomes take months. To try the loop now, simulate traffic: random applications go through all eight agents, then repayment results arrive from a "real world" that has drifted from what the bootstrap model learned. Around 800 applications gives the model enough to learn from.</p>
<form method="post" action="{{ url_for('learning_simulate') }}" class="row">
<input name="n" type="number" value="800" min="10" max="2000" style="width:110px;margin:0"><button>Simulate applications and outcomes</button>
<span class="mute">takes roughly 10 to 20 seconds for 800; the cases appear on the Cases tab</span></form>
<p class="mute">Outcomes for real cases can also be recorded one by one on each completed case page.</p></div>

<div class="card"><h2>2. Retrain the risk model</h2>
<form method="post" action="{{ url_for('learning_retrain') }}"><button class="alt">Propose retraining</button></form>
{% if retrain == 'not_enough' %}<p>Not enough data yet: {{ n_feedback }} of {{ min_needed }} outcomes.</p>
{% elif retrain %}<table style="margin-top:12px"><tr><th></th><th>Hold-out log-loss (lower is better)</th></tr>
<tr><td>Current model {{ model.version }}</td><td>{{ '%.3f'|format(retrain.current_metrics.log_loss) }}</td></tr>
<tr><td>Candidate {{ retrain.candidate.version }}</td><td>{{ '%.3f'|format(retrain.candidate_metrics.log_loss) }}</td></tr></table>
<p>Evaluated on {{ retrain.holdout_size }} outcomes the candidate never trained on. <b>{{ 'The candidate is better.' if retrain.improved else 'The candidate is not better, so promoting is not recommended.' }}</b></p>
<form method="post" action="{{ url_for('learning_promote') }}" class="row">
<input name="approver" placeholder="Approver name (required)" required style="max-width:260px;margin:0"><button {{ '' if retrain.improved else 'class=danger' }}>Promote to live</button></form>{% endif %}</div>

<div class="card"><h2>3. Tune the reject threshold</h2>
<form method="post" action="{{ url_for('learning_threshold') }}"><button class="alt">Propose threshold</button></form>
{% if threshold == 'none' %}<p>No outcomes recorded yet.</p>
{% elif threshold %}<p>Auto-reject threshold {{ '%.2f'|format(threshold.current) }} &rarr; <b>{{ '%.2f'|format(threshold.suggested) }}</b>
(expected cost {{ '{:,.0f}'.format(threshold.cost_at_current) }} &rarr; {{ '{:,.0f}'.format(threshold.cost_at_suggested) }} on {{ threshold.samples }} outcomes; {{ threshold.note }}).</p>
{% if threshold.suggested != threshold.current %}<form method="post" action="{{ url_for('learning_apply_threshold') }}" class="row">
<input name="approver" placeholder="Approver name (required)" required style="max-width:260px;margin:0"><button>Apply to policy</button></form>
{% else %}<p class="mute">No cheaper threshold within the allowed range, so no change is proposed.</p>{% endif %}{% endif %}
<p class="mute">Caveat: outcomes only exist for loans that were approved, so this can make policy stricter but never more lenient.</p></div>

<div class="card"><h2>Change history</h2>
{% if changes %}<table>{% for a in changes %}<tr><td class="mute">{{ a.ts[:19] }}</td><td><b>{{ a.actor }}</b> {{ a.event.replace('_',' ') }}</td><td>{{ a.detail }}</td></tr>{% endfor %}</table>
{% else %}<span class="mute">No model or policy changes yet.</span>{% endif %}</div>{% endblock %}"""


def create_app(system: LoanProcessingSystem, demo_scenarios: Optional[list[Scenario]] = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = "dev-only-change-me"
    app.jinja_loader = DictLoader({"base.html": BASE, "index.html": INDEX, "case.html": CASE,
                                   "submit.html": SUBMIT, "workflow.html": WORKFLOW, "learning.html": LEARNING})
    proposals: dict = {}
    sim_rng = random.Random(2026)
    app.jinja_env.globals["money"] = money

    @app.get("/")
    def index():
        cases = system.cases()
        attention = {S.FAILED, S.RECONCILIATION_REQUIRED, S.NEEDS_INFO}
        counts = {"pending": sum(c.status == S.AWAITING_APPROVAL for c in cases),
                  "completed": sum(c.status == S.COMPLETED for c in cases),
                  "attention": sum(c.status in attention for c in cases), "total": len(cases)}
        return render_template("index.html", cases=cases, pending=system.pending_approvals(),
                               counts=type("C", (), counts), can_load_demo=bool(demo_scenarios))

    @app.get("/case/<application_id>")
    def case(application_id: str):
        try:
            ctx = system.get(application_id)
        except KeyError:
            abort(404)
        recorded = any(f["application_id"] == application_id for f in system.repo.get_feedback())
        return render_template("case.html", c=ctx, messages=system.bus.conversation(application_id),
                               audit=system.repo.get_audit(application_id), outcome_recorded=recorded)

    @app.post("/case/<application_id>/review")
    def review(application_id: str):
        approved = request.form.get("decision") == "approve"
        try:
            amount = float(request.form["amount"]) if approved and request.form.get("amount") else None
            ctx = system.review(application_id, request.form.get("reviewer", ""), approved,
                                request.form.get("notes", ""), amount)
            flash(f"{application_id}: {'approved' if approved else 'rejected'} -> {ctx.status.value}")
        except (ValueError, KeyError) as exc:
            flash(f"Could not record decision: {exc}")
        return redirect(url_for("case", application_id=application_id))

    @app.post("/case/<application_id>/retry")
    def retry(application_id: str):
        try:
            ctx = system.retry(application_id)
            flash(f"{application_id}: retried -> {ctx.status.value}")
        except Exception as exc:  # noqa: BLE001
            flash(f"Retry not possible: {exc}")
        return redirect(url_for("case", application_id=application_id))

    @app.post("/case/<application_id>/outcome")
    def outcome(application_id: str):
        try:
            system.record_outcome(application_id, request.form.get("result") == "defaulted")
            flash("Repayment outcome recorded for model learning.")
        except Exception as exc:  # noqa: BLE001
            flash(f"Could not record outcome: {exc}")
        return redirect(url_for("case", application_id=application_id))

    @app.post("/demo/load")
    def load_demo():
        added = 0
        for s in demo_scenarios or []:
            if not system.repo.exists(s.application.application_id):
                system.submit(s.application)
                added += 1
        flash(f"Submitted {added} demo application(s).")
        return redirect(url_for("index"))

    # ------------------------------------------------------------ new application
    blank_docs = {
        "doc_id": "Name: \nID Number: \nDate of Birth: 1990-01-01\nExpiry: 2031-01-01",
        "doc_salary": "Employer: \nEmployee: \nMonthly Salary: \nMonth: 2026-09",
        "doc_bank": "Account Holder: \nAccount Number: \nAverage Monthly Balance: \nAverage Monthly Credit: ",
    }

    def form_from_scenario(sc: Scenario) -> dict:
        a, texts = sc.application, {d.doc_type: d.raw_text for d in sc.application.documents}
        cr = sc.credit
        return dict(name=a.applicant_name, national_id=a.national_id, account=a.account_number,
                    employer=a.employer, income=int(a.stated_monthly_income), amount=int(a.requested_amount),
                    tenure=a.tenure_months, score=cr.score if cr else 600,
                    obligations=int(cr.monthly_obligations) if cr else 0,
                    delinquencies=cr.delinquencies if cr else 0, utilization=cr.utilization if cr else 0.0,
                    thin=cr is None, doc_id=texts.get(DocumentType.NATIONAL_ID, ""),
                    doc_salary=texts.get(DocumentType.SALARY_SLIP, ""), doc_bank=texts.get(DocumentType.BANK_STATEMENT, ""))

    def blank_form() -> dict:
        return dict(name="", national_id="", account="", employer="", income="", amount="", tenure=24,
                    score=680, obligations=0, delinquencies=0, utilization=0.3, thin=False, **blank_docs)

    @app.route("/submit", methods=["GET", "POST"])
    def submit():
        scenarios = {s.application.application_id: s for s in (demo_scenarios or [])}
        if request.method == "POST":
            form = request.form
            try:
                name, nid = form["name"].strip(), form["national_id"].strip()
                amount, tenure, income = float(form["amount"]), int(form["tenure"]), float(form["income"])
                if not name or not nid or amount <= 0 or income <= 0 or not (1 <= tenure <= 120):
                    raise ValueError("name, national ID, amount, income and a tenure of 1-120 months are required")
                n = sum(1 for c in system.cases() if c.application_id.startswith("WEB-")) + 1
                while system.repo.exists(f"WEB-{n:04d}"):
                    n += 1
                docs = [Document(t, fn, form.get(key, "").strip())
                        for key, t, fn in (("doc_id", DocumentType.NATIONAL_ID, "national_id.txt"),
                                           ("doc_salary", DocumentType.SALARY_SLIP, "salary_slip.txt"),
                                           ("doc_bank", DocumentType.BANK_STATEMENT, "bank_statement.txt"))
                        if form.get(key, "").strip()]
                application = LoanApplication(f"WEB-{n:04d}", name, nid, amount, tenure, income,
                                              form["employer"].strip(), form["account"].strip(), docs)
                if form.get("thin") and hasattr(system.bureau, "unregister"):
                    system.bureau.unregister(nid)               # make 'no history' actually true
                elif hasattr(system.bureau, "register"):
                    obligations = float(form.get("obligations") or 0)
                    system.bureau.register(nid, CreditReport(
                        nid, int(form.get("score") or 600), 1 if obligations else 0, obligations,
                        int(form.get("delinquencies") or 0), float(form.get("utilization") or 0)))
                ctx = system.submit(application)
                flash(f"{ctx.application_id} processed: {ctx.status.value.replace('_', ' ')}")
                return redirect(url_for("case", application_id=ctx.application_id))
            except (ValueError, KeyError) as exc:
                flash(f"Could not submit: {exc}")
                values = {**blank_form(), **form.to_dict(), "thin": bool(form.get("thin"))}
                return render_template("submit.html", scenarios=list(scenarios.values()), sample=None, f=values)
        sample = scenarios.get(request.args.get("sample", ""))
        values = form_from_scenario(sample) if sample else (
            form_from_scenario(next(iter(scenarios.values()))) if scenarios else blank_form())
        return render_template("submit.html", scenarios=list(scenarios.values()), sample=sample, f=values)

    # ----------------------------------------------------------- workflow & agents
    def load_diagrams() -> list[tuple[str, str, str]]:
        path = os.path.join(os.path.dirname(__file__), "..", "docs", "ARCHITECTURE.md")
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for sec in re.split(r"\n(?=## )", text)[1:]:
            m = re.search(r"```mermaid\n(.*?)```", sec, re.S)
            if m:
                title = sec.splitlines()[0].lstrip("# ").strip()
                note = " ".join(l.strip() for l in re.sub(r"```mermaid.*?```", "", sec, flags=re.S).splitlines()[1:]
                                if l.strip() and l.strip() != "---")
                out.append((title, m.group(1), note))
        return out

    @app.get("/workflow")
    def workflow():
        return render_template("workflow.html", agents=list(system.workflow.agents.items()),
                               policy=system.config.policy, model=system.models.current,
                               retries=system.config.retry_attempts, diagrams=load_diagrams())

    # ------------------------------------------------------------------- learning
    def fresh(key: str, n_feedback: int):
        item = proposals.get(key)
        return item[1] if item and item[0] == n_feedback else None   # stale once new outcomes arrive

    @app.get("/learning")
    def learning():
        n = len(system.repo.get_feedback())
        return render_template("learning.html", n_feedback=n, min_needed=system.config.min_feedback_samples,
                               model=system.models.current, policy=system.config.policy,
                               retrain=fresh("retrain", n), threshold=fresh("threshold", n),
                               changes=system.repo.get_audit("SYSTEM"))

    @app.post("/learning/simulate")
    def learning_simulate():
        try:
            n = max(10, min(2000, int(request.form.get("n", 800))))
            counts = simulate_applications(system, n, sim_rng, start=next_sim_index(system))
            recorded, defaults = simulate_outcomes(system, sim_rng)
            flash(f"Simulated {n} applications ({counts['disbursed']} disbursed in total). "
                  f"Recorded {recorded} new repayment outcomes, {defaults} defaults.")
        except (ValueError, RuntimeError) as exc:
            flash(f"Simulation failed: {exc}")
        return redirect(url_for("learning"))

    @app.post("/learning/retrain")
    def learning_retrain():
        result = system.propose_retraining()
        proposals["retrain"] = (len(system.repo.get_feedback()), result or "not_enough")
        return redirect(url_for("learning"))

    @app.post("/learning/promote")
    def learning_promote():
        result = fresh("retrain", len(system.repo.get_feedback()))
        if not isinstance(result, RetrainResult):
            flash("Propose retraining first.")
        else:
            try:
                system.promote_model(result, request.form.get("approver", ""))
                proposals.pop("retrain", None)
                flash(f"Model {result.candidate.version} is now live.")
            except ValueError as exc:
                flash(str(exc))
        return redirect(url_for("learning"))

    @app.post("/learning/threshold")
    def learning_threshold():
        suggestion = system.propose_threshold()
        proposals["threshold"] = (len(system.repo.get_feedback()), suggestion or "none")
        return redirect(url_for("learning"))

    @app.post("/learning/apply-threshold")
    def learning_apply_threshold():
        suggestion = fresh("threshold", len(system.repo.get_feedback()))
        if suggestion in (None, "none"):
            flash("Propose a threshold first.")
        else:
            try:
                system.apply_threshold(suggestion, request.form.get("approver", ""))
                proposals.pop("threshold", None)
                flash(f"Policy updated: auto-reject threshold is now {suggestion.suggested:.2f}.")
            except ValueError as exc:
                flash(str(exc))
        return redirect(url_for("learning"))

    return app

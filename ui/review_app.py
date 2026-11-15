"""
Streamlit Interactive UI for the Corporate Travel Policy Booking Auditor.

Full interactive dashboard:
  - Audit a travel request (natural language input)
  - Run full 10-case evaluation with live progress
  - Human review queue with verdict submission
  - Audit trail viewer
  - Employee & budget explorer
  - System architecture diagram

Run:  streamlit run ui/review_app.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

# Add project root to path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

import config
from core import hitl
from core.memory import MemoryStore
from core.rag import PolicyRAG
from core.schemas import Decision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_memory() -> MemoryStore:
    """Singleton MemoryStore in session state."""
    if "memory" not in st.session_state:
        mem = MemoryStore(config.SQLITE_DB_PATH)
        row = mem.conn.execute("SELECT COUNT(*) as cnt FROM employees").fetchone()
        if row["cnt"] == 0:
            mem.seed_from_json(
                config.EMPLOYEES_PATH,
                config.BUDGETS_PATH,
                config.TRIP_HISTORY_SEED_PATH,
            )
        st.session_state.memory = mem
    return st.session_state.memory


def _get_rag() -> PolicyRAG:
    if "rag" not in st.session_state:
        rag = PolicyRAG(
            persist_dir=config.CHROMA_PERSIST_DIR,
            embedding_model=config.EMBEDDING_MODEL_NAME,
            top_k=config.RAG_TOP_K,
        )
        rag.ingest(config.POLICY_PATH)
        st.session_state.rag = rag
    return st.session_state.rag


def _run_async(coro):
    """Run an async coroutine from sync Streamlit code."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_DECISION_COLORS = {
    "APPROVE": "#28a745",
    "MODIFY": "#ffc107",
    "ESCALATE": "#dc3545",
}

_DECISION_EMOJI = {
    "APPROVE": "✅",
    "MODIFY": "🔄",
    "ESCALATE": "🚨",
}


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="Travel Auditor — CSE476 CA2",
        page_icon="✈️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # --- Sidebar navigation ---
    with st.sidebar:
        st.image("https://img.icons8.com/fluency/96/airplane-take-off.png", width=64)
        st.title("✈️ Travel Auditor")
        st.caption("CSE476 CA2 · Group 15 · Q15")
        st.caption(f"Yaswanth Kumar Mallela · 12315067")
        st.divider()

        page = st.radio(
            "Navigate",
            [
                "🏠 Dashboard",
                "📝 Audit Request",
                "📊 Run Evaluation",
                "👤 Human Review",
                "📋 Audit Trail",
                "👥 Employees & Budgets",
                "📜 Travel Policy",
                "🏗️ Architecture",
            ],
            label_visibility="collapsed",
        )

        st.divider()
        use_llm = st.toggle("Enable LLM (Gemini)", value=bool(config.GOOGLE_API_KEY))
        if use_llm and not config.GOOGLE_API_KEY:
            st.warning("⚠️ Set GOOGLE_API_KEY in .env to enable LLM")
            use_llm = False
        st.session_state["use_llm"] = use_llm

        st.divider()
        st.caption(f"📅 Today: {config.TODAY}")
        st.caption(f"🤖 Model: {config.GEMINI_MODEL_NAME}")
        st.caption(f"💾 DB: {Path(config.SQLITE_DB_PATH).name}")

    # --- Route to page ---
    if page.startswith("🏠"):
        _page_dashboard()
    elif page.startswith("📝"):
        _page_audit()
    elif page.startswith("📊"):
        _page_evaluation()
    elif page.startswith("👤"):
        _page_review()
    elif page.startswith("📋"):
        _page_audit_trail()
    elif page.startswith("👥"):
        _page_employees()
    elif page.startswith("📜"):
        _page_policy()
    elif page.startswith("🏗️"):
        _page_architecture()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _page_dashboard():
    st.title("🏠 Dashboard")
    st.markdown("### Corporate Travel Policy Booking Auditor")

    memory = _get_memory()

    # Metrics row
    col1, col2, col3, col4 = st.columns(4)

    # Count employees
    emp_count = memory.conn.execute("SELECT COUNT(*) as cnt FROM employees").fetchone()["cnt"]
    col1.metric("👥 Employees", emp_count)

    # Count trips
    trip_count = memory.conn.execute("SELECT COUNT(*) as cnt FROM trip_history").fetchone()["cnt"]
    col2.metric("✈️ Total Trips", trip_count)

    # Pending reviews
    pending = memory.conn.execute("SELECT COUNT(*) as cnt FROM review_queue WHERE status='PENDING'").fetchone()["cnt"]
    col3.metric("⏳ Pending Reviews", pending)

    # Audit log entries
    log_count = memory.conn.execute("SELECT COUNT(*) as cnt FROM audit_log").fetchone()["cnt"]
    col4.metric("📋 Audit Events", log_count)

    st.divider()

    # Budget overview
    st.subheader("💰 Department Budgets")
    budgets = memory.conn.execute("SELECT * FROM dept_budget_ledger ORDER BY dept").fetchall()
    if budgets:
        budget_cols = st.columns(len(budgets))
        for i, b in enumerate(budgets):
            with budget_cols[i]:
                remaining = b["remaining_inr"]
                color = "normal" if remaining > 50000 else ("off" if remaining > 10000 else "inverse")
                st.metric(f"🏢 {b['dept']}", f"₹{remaining:,.0f}", delta=None)
    else:
        st.info("No budget data loaded.")

    st.divider()

    # Recent audit log
    st.subheader("📋 Recent Activity")
    recent = memory.conn.execute(
        "SELECT * FROM audit_log ORDER BY ts DESC LIMIT 10"
    ).fetchall()
    if recent:
        for entry in recent:
            ts = entry["ts"] or ""
            agent = entry["agent"]
            action = entry["action"]
            req_id = entry["request_id"]
            emoji = "🔵" if "approve" in action.lower() else ("🟡" if "escalat" in action.lower() else "⚪")
            st.text(f"{emoji} [{ts}] {req_id} · {agent} → {action}")
    else:
        st.info("No audit activity yet. Try auditing a request!")


# ---------------------------------------------------------------------------
# Audit Request
# ---------------------------------------------------------------------------

def _page_audit():
    st.title("📝 Audit a Travel Request")
    st.markdown("Enter a natural-language travel request below and the system will process it through all five agents.")

    # Example requests
    with st.expander("💡 Example Requests (click to copy)"):
        examples = [
            "I need to fly Delhi to Mumbai on 2 Dec 2026, returning 3 Dec 2026, for a client meeting. Economy class. Employee E101.",
            "Book me a Business class flight from Delhi to Bangalore, departing 27 Nov 2026, returning 28 Nov. Client meeting. Employee E101.",
            "Urgent travel needed. Delhi to Goa on 15 Nov 2026, returning 16 Nov. Client meeting. Employee E101. Economy around INR 7500.",
            "Delhi to London, departing 7 Dec 2026, returning 12 Dec 2026. Client meeting. Business class. Employee E103.",
            "Delhi to Goa on 5 Dec 2026, return 8 Dec 2026. Family vacation. Economy. Employee E101.",
            "Mumbai to Delhi on 30 Nov 2026, return 1 Dec 2026. Project delivery work. Economy. Employee E104.",
            "Book Business class Delhi to New York JFK, departing 10 Dec 2026, returning 17 Dec 2026. Conference. Employee E103. Fare around INR 145,000.",
        ]
        for i, ex in enumerate(examples):
            if st.button(f"📋 Case {i+1}", key=f"ex_{i}", use_container_width=True):
                st.session_state["audit_input"] = ex

    # Input area
    request_text = st.text_area(
        "Travel Request",
        value=st.session_state.get("audit_input", ""),
        height=100,
        placeholder="E.g., I need to fly Delhi to Mumbai on 2 Dec 2026, returning 3 Dec...",
    )

    col_btn, col_reset = st.columns([1, 4])
    with col_btn:
        run_audit = st.button("🚀 Audit Request", type="primary", use_container_width=True)
    with col_reset:
        if st.button("🗑️ Clear", use_container_width=False):
            st.session_state.pop("audit_input", None)
            st.session_state.pop("audit_result", None)
            st.rerun()

    if run_audit and request_text.strip():
        from agents.approval_manager import ApprovalManager

        memory = _get_memory()
        rag = _get_rag()
        use_llm = st.session_state.get("use_llm", False)

        with st.spinner("🔄 Processing through 5 agents..."):
            manager = ApprovalManager(memory, rag, use_llm=use_llm)
            start = time.time()
            result = _run_async(manager.process_request(request_text.strip()))
            elapsed = time.time() - start

        st.session_state["audit_result"] = result
        st.session_state["audit_elapsed"] = elapsed

    # Display result
    if "audit_result" in st.session_state:
        result = st.session_state["audit_result"]
        elapsed = st.session_state.get("audit_elapsed", 0)
        decision = result.decision.value
        color = _DECISION_COLORS.get(decision, "#888")
        emoji = _DECISION_EMOJI.get(decision, "❓")

        st.divider()

        # Decision banner
        st.markdown(
            f"""
            <div style="background-color:{color}22; border-left: 5px solid {color};
                        padding: 20px; border-radius: 8px; margin-bottom: 16px;">
                <h2 style="margin:0; color:{color};">{emoji} {decision}</h2>
                <p style="margin:4px 0 0; font-size:14px; color:#666;">
                    Request {result.request_id} · Processed in {elapsed:.2f}s
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Metrics
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Decision", result.decision.value)
        m2.metric("Primary Clause", result.primary_clause_id)
        m3.metric("Secondary", ", ".join(result.secondary_clause_ids) or "None")
        m4.metric("Status", result.status.value)

        # Best fare
        if result.best_compliant_fare:
            fare = result.best_compliant_fare
            st.success(
                f"✈️ **Best Compliant Fare:** {fare.airline} · "
                f"₹{fare.price_inr:,.0f} · {fare.cabin.value.title()} · "
                f"{fare.duration_hours:.1f}h · "
                f"{'Contracted ✅' if fare.contracted else 'Non-contracted'} · "
                f"{'Refundable' if fare.refundable else 'Non-refundable'}"
            )
        else:
            st.warning("No compliant fare available.")

        # Justification
        st.info(f"📝 **Justification:** {result.justification}")

        # Human routing
        if result.routed_to_human:
            st.error("🚨 This request has been escalated to the **Human Review Queue**. Go to 👤 Human Review to submit a verdict.")

        # Audit trail for this request
        with st.expander("🔍 Agent Audit Trail"):
            memory = _get_memory()
            logs = memory.get_audit_log(result.request_id)
            if logs:
                for entry in logs:
                    payload = ""
                    if entry.payload_json:
                        try:
                            p = json.loads(entry.payload_json)
                            payload = f" → {json.dumps(p, indent=None)[:120]}"
                        except json.JSONDecodeError:
                            pass
                    st.text(f"[{entry.ts}] {entry.agent}: {entry.action}{payload}")
            else:
                st.info("No audit entries found.")


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _page_evaluation():
    st.title("📊 Run 10-Case Evaluation")
    st.markdown("Run the full benchmark suite of 10 travel requests with known ground truth.")

    # Load eval cases for preview
    with open(config.EVAL_CASES_PATH) as f:
        cases = json.load(f)

    with st.expander("📋 Preview Eval Cases"):
        for c in cases:
            dec_color = {"APPROVE": "green", "MODIFY": "orange", "ESCALATE": "red"}.get(c["expected_decision"], "gray")
            st.markdown(
                f"**Case {c['case_id']}** · :{dec_color}[{c['expected_decision']}] · `{c['expected_primary_clause']}` · "
                f"Expected fare: {'₹' + str(c['expected_best_fare_inr']) if c.get('expected_best_fare_inr') else 'null'}"
            )
            st.caption(c["request_text"])

    st.divider()

    if st.button("🚀 Run Full Evaluation", type="primary", use_container_width=True):
        from agents.approval_manager import ApprovalManager

        use_llm = st.session_state.get("use_llm", False)
        progress_bar = st.progress(0, text="Initializing...")
        results_container = st.empty()
        results = []

        for i, case in enumerate(cases):
            progress_bar.progress((i) / len(cases), text=f"Running case {case['case_id']}...")

            # Reset memory for isolation
            memory = MemoryStore(config.SQLITE_DB_PATH)
            memory.reset_to_seed(config.EMPLOYEES_PATH, config.BUDGETS_PATH, config.TRIP_HISTORY_SEED_PATH)
            rag = PolicyRAG(
                persist_dir=config.CHROMA_PERSIST_DIR,
                embedding_model=config.EMBEDDING_MODEL_NAME,
                top_k=config.RAG_TOP_K,
            )
            rag.ingest(config.POLICY_PATH)

            manager = ApprovalManager(memory, rag, use_llm=use_llm)
            start = time.time()
            result = _run_async(manager.process_request(case["request_text"]))
            latency = time.time() - start

            actual_fare = result.best_compliant_fare.price_inr if result.best_compliant_fare else None
            expected_fare = case.get("expected_best_fare_inr")

            decision_ok = result.decision.value == case["expected_decision"]
            clause_ok = result.primary_clause_id == case["expected_primary_clause"]

            # Fare correctness
            if expected_fare is None and case["expected_decision"] == "ESCALATE":
                fare_ok = True
            else:
                fare_ok = (actual_fare == expected_fare) or (actual_fare is None and expected_fare is None)

            results.append({
                "case_id": case["case_id"],
                "expected_decision": case["expected_decision"],
                "actual_decision": result.decision.value,
                "decision_ok": decision_ok,
                "expected_clause": case["expected_primary_clause"],
                "actual_clause": result.primary_clause_id,
                "clause_ok": clause_ok,
                "expected_fare": expected_fare,
                "actual_fare": actual_fare,
                "fare_ok": fare_ok,
                "latency": round(latency, 2),
                "justification": result.justification,
            })
            memory.close()

        progress_bar.progress(1.0, text="✅ Evaluation complete!")

        # Store results in session state
        st.session_state["eval_results"] = results

        # Refresh the session-state memory since we closed it
        st.session_state.pop("memory", None)

    # Display results
    if "eval_results" in st.session_state:
        results = st.session_state["eval_results"]
        total = len(results)
        dec_acc = sum(1 for r in results if r["decision_ok"]) / total
        cls_acc = sum(1 for r in results if r["clause_ok"]) / total
        fare_acc = sum(1 for r in results if r["fare_ok"]) / total
        avg_lat = sum(r["latency"] for r in results) / total

        st.divider()
        st.subheader("📈 Metrics Summary")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Decision Accuracy", f"{dec_acc:.0%} ({sum(1 for r in results if r['decision_ok'])}/{total})")
        m2.metric("Clause Correctness", f"{cls_acc:.0%} ({sum(1 for r in results if r['clause_ok'])}/{total})")
        m3.metric("Fare Accuracy", f"{fare_acc:.0%} ({sum(1 for r in results if r['fare_ok'])}/{total})")
        m4.metric("Avg Latency", f"{avg_lat:.2f}s")

        # Per-case table
        st.subheader("📋 Per-Case Results")
        table_data = []
        for r in results:
            dec_icon = "✅" if r["decision_ok"] else "❌"
            cls_icon = "✅" if r["clause_ok"] else "❌"
            fare_icon = "✅" if r["fare_ok"] else "❌"
            fare_str = f"₹{r['actual_fare']:,.0f}" if r["actual_fare"] else "null"
            exp_fare_str = f"₹{r['expected_fare']:,.0f}" if r["expected_fare"] else "null"
            table_data.append({
                "Case": r["case_id"],
                "Expected": f"{r['expected_decision']} / {r['expected_clause']}",
                "Actual": f"{r['actual_decision']} / {r['actual_clause']}",
                "Decision": dec_icon,
                "Clause": cls_icon,
                "Fare": f"{fare_icon} ({exp_fare_str} → {fare_str})",
                "Latency": f"{r['latency']:.2f}s",
            })
        st.dataframe(table_data, use_container_width=True, hide_index=True)

        # Confusion matrix
        st.subheader("🔢 Confusion Matrix")
        decisions = ["APPROVE", "MODIFY", "ESCALATE"]
        confusion = {(e, a): 0 for e in decisions for a in decisions}
        for r in results:
            confusion[(r["expected_decision"], r["actual_decision"])] += 1

        cm_data = []
        for e in decisions:
            row = {"Expected \\ Actual": e}
            for a in decisions:
                row[a] = confusion[(e, a)]
            cm_data.append(row)
        st.dataframe(cm_data, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Human Review
# ---------------------------------------------------------------------------

def _page_review():
    st.title("👤 Human Review Queue")
    st.markdown("Review escalated travel requests and submit verdicts.")

    memory = _get_memory()

    # Tab layout
    tab_pending, tab_all = st.tabs(["⏳ Pending Reviews", "📁 All Reviews"])

    with tab_pending:
        items = hitl.get_pending_reviews(memory)
        if not items:
            st.success("🎉 No pending reviews — all caught up!")
        else:
            st.warning(f"**{len(items)}** request(s) awaiting your review")

            for item in items:
                with st.container(border=True):
                    # Header
                    st.markdown(f"### 📋 {item.request_id}")

                    if item.audit_result_json:
                        audit = json.loads(item.audit_result_json)

                        # Metrics row
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Agent Decision", audit.get("decision", "N/A"))
                        c2.metric("Primary Clause", audit.get("primary_clause_id", "N/A"))
                        c3.metric("Status", item.status)

                        # Justification
                        st.info(f"📝 {audit.get('justification', 'N/A')}")

                        # Secondary clauses
                        sec = audit.get("secondary_clause_ids", [])
                        if sec:
                            st.caption(f"Secondary clauses: {', '.join(sec)}")

                        # Best fare
                        bf = audit.get("best_compliant_fare")
                        if bf:
                            st.success(f"✈️ Best fare: {bf.get('airline', '?')} · ₹{bf.get('price_inr', 0):,.0f}")

                    # Audit trail
                    with st.expander("🔍 Agent Audit Trail"):
                        logs = memory.get_audit_log(item.request_id)
                        for entry in logs:
                            st.text(f"[{entry.ts}] {entry.agent}: {entry.action}")

                    # Verdict form
                    st.divider()
                    st.markdown("**Submit Your Verdict:**")

                    fc1, fc2, fc3 = st.columns([1, 2, 1])
                    with fc1:
                        verdict = st.selectbox(
                            "Verdict",
                            ["approve", "reject", "modify"],
                            key=f"v_{item.request_id}",
                        )
                    with fc2:
                        note = st.text_input(
                            "Reviewer Note",
                            key=f"n_{item.request_id}",
                            placeholder="Reason for your decision...",
                        )
                    with fc3:
                        reviewer = st.text_input(
                            "Reviewer",
                            value="Manager",
                            key=f"r_{item.request_id}",
                        )

                    # Get fare for budget deduction
                    fare_inr = None
                    if item.audit_result_json:
                        aud = json.loads(item.audit_result_json)
                        bf = aud.get("best_compliant_fare")
                        if bf:
                            fare_inr = bf.get("price_inr")

                    if st.button(
                        f"✅ Submit Verdict for {item.request_id}",
                        key=f"s_{item.request_id}",
                        type="primary",
                        use_container_width=True,
                    ):
                        try:
                            result = hitl.submit_verdict(
                                item.request_id, verdict, note, reviewer, memory, fare_inr,
                            )
                            st.success(f"✅ Verdict recorded: **{result['final_status']}**")
                            st.balloons()
                            time.sleep(1)
                            st.rerun()
                        except ValueError as e:
                            st.error(f"Error: {e}")

    with tab_all:
        all_items = hitl.get_all_reviews(memory)
        if not all_items:
            st.info("No review items yet.")
        else:
            for item in all_items:
                status_emoji = {
                    "PENDING": "🟡",
                    "HUMAN_APPROVED": "🟢",
                    "HUMAN_REJECTED": "🔴",
                    "HUMAN_MODIFIED": "🔵",
                }.get(item.status, "⚪")

                with st.expander(f"{status_emoji} {item.request_id} — **{item.status}**"):
                    if item.audit_result_json:
                        aud = json.loads(item.audit_result_json)
                        st.json(aud)
                    if item.human_verdict:
                        st.markdown(f"**Verdict:** {item.human_verdict}")
                        st.markdown(f"**Note:** {item.human_note}")
                        st.markdown(f"**Reviewed At:** {item.reviewed_at}")


# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------

def _page_audit_trail():
    st.title("📋 Audit Trail")
    st.markdown("Complete log of all agent interactions and decisions.")

    memory = _get_memory()
    logs = memory.get_all_audit_log()

    if not logs:
        st.info("No audit log entries yet.")
        return

    st.metric("Total Events", len(logs))

    # Filter controls
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        agents = sorted(set(e.agent for e in logs))
        selected_agent = st.selectbox("Filter by Agent", ["All"] + agents)
    with col_f2:
        request_ids = sorted(set(e.request_id for e in logs))
        selected_req = st.selectbox("Filter by Request", ["All"] + request_ids)

    filtered = logs
    if selected_agent != "All":
        filtered = [e for e in filtered if e.agent == selected_agent]
    if selected_req != "All":
        filtered = [e for e in filtered if e.request_id == selected_req]

    st.caption(f"Showing {len(filtered)} of {len(logs)} entries")

    # Display as table
    table_data = []
    for entry in filtered:
        payload_preview = ""
        if entry.payload_json:
            try:
                p = json.loads(entry.payload_json)
                payload_preview = json.dumps(p, indent=None)[:100]
            except json.JSONDecodeError:
                payload_preview = entry.payload_json[:100]
        table_data.append({
            "Time": str(entry.ts or ""),
            "Request": entry.request_id,
            "Agent": entry.agent,
            "Action": entry.action,
            "Details": payload_preview,
        })

    st.dataframe(table_data, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Employees & Budgets
# ---------------------------------------------------------------------------

def _page_employees():
    st.title("👥 Employees & Budgets")

    memory = _get_memory()

    tab_emp, tab_budget, tab_trips = st.tabs(["👤 Employees", "💰 Budgets", "✈️ Trip History"])

    with tab_emp:
        rows = memory.conn.execute("SELECT * FROM employees ORDER BY employee_id").fetchall()
        if rows:
            emp_data = []
            for r in rows:
                emp_data.append({
                    "ID": r["employee_id"],
                    "Name": r["name"],
                    "Grade": r["grade"],
                    "Department": r["department"],
                    "Email": r["email"],
                })
            st.dataframe(emp_data, use_container_width=True, hide_index=True)
        else:
            st.info("No employees loaded.")

    with tab_budget:
        budgets = memory.conn.execute("SELECT * FROM dept_budget_ledger ORDER BY dept").fetchall()
        if budgets:
            bdata = []
            for b in budgets:
                bdata.append({
                    "Department": b["dept"],
                    "Remaining (₹)": f"₹{b['remaining_inr']:,.0f}",
                    "Last Updated": b["updated_at"] or "—",
                })
            st.dataframe(bdata, use_container_width=True, hide_index=True)

            # Visual bar chart
            import pandas as pd
            df = pd.DataFrame([{"Department": b["dept"], "Remaining (INR)": b["remaining_inr"]} for b in budgets])
            st.bar_chart(df, x="Department", y="Remaining (INR)")
        else:
            st.info("No budget data loaded.")

    with tab_trips:
        trips = memory.conn.execute("SELECT * FROM trip_history ORDER BY depart_date DESC").fetchall()
        if trips:
            tdata = []
            for t in trips:
                status_emoji = {"approved": "🟢", "completed": "✅", "pending": "🟡", "rejected": "🔴"}.get(t["status"], "⚪")
                tdata.append({
                    "Trip ID": t["trip_id"],
                    "Employee": t["employee_id"],
                    "Route": f"{t['origin']} → {t['destination']}",
                    "Date": t["depart_date"],
                    "Fare (₹)": f"₹{t['fare_inr']:,.0f}" if t["fare_inr"] else "—",
                    "Status": f"{status_emoji} {t['status']}",
                    "Clause": t["clause_id"] or "—",
                    "Decided By": t["decided_by"],
                })
            st.dataframe(tdata, use_container_width=True, hide_index=True)
        else:
            st.info("No trip history.")


# ---------------------------------------------------------------------------
# Travel Policy
# ---------------------------------------------------------------------------

def _page_policy():
    st.title("📜 Travel Policy")
    st.markdown("The 8 policy clauses that govern travel request auditing.")

    policy_path = config.POLICY_PATH
    if policy_path.exists():
        content = policy_path.read_text(encoding="utf-8")
        st.markdown(content)
    else:
        st.error(f"Policy file not found at {policy_path}")

    st.divider()
    st.subheader("⚖️ Clause Precedence Order")
    st.markdown("""
    When multiple clauses are violated, the **primary clause** is determined by precedence (highest first):

    | Priority | Clause | Rule |
    |:---:|:---|:---|
    | 1 | **TP-6.1** | Business Purpose — ineligible purpose |
    | 2 | **TP-1.1** | Advance Booking — insufficient notice |
    | 3 | **TP-7.1** | Trip Frequency — quarterly limit exceeded |
    | 4 | **TP-9.1** | High Value — fare > ₹100,000 |
    | 5 | **TP-4.1** | Department Budget — insufficient funds |
    | 6 | **TP-2.1** | Travel Class — unauthorized cabin class |
    | 7 | **TP-2.2** | Fare Caps — fare exceeds category cap |
    """)


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------

def _page_architecture():
    st.title("🏗️ System Architecture")

    st.markdown("""
    ### Manager-Worker Delegation with Human Routing

    The **Approval Manager** (orchestrator) delegates to four specialised worker agents,
    collects their structured outputs, applies deterministic decision rules, and routes
    escalated requests to a human review queue.
    """)

    # Mermaid diagram via st.markdown (Streamlit renders mermaid natively in newer versions)
    st.markdown("""
    ```mermaid
    graph TD
        User["👤 User / CLI / Streamlit"] -->|Raw text request| AM["🎯 Approval Manager<br/>(Orchestrator)"]

        AM -->|Step 1: Parse| RP["📝 Request Parser<br/>Agent"]
        AM -->|Step 2a: Retrieve| PR["📚 Policy Retrieval<br/>Agent (RAG)"]
        AM -->|Step 2b: Search| FS["✈️ Fare Search<br/>Agent (SK Plugin)"]
        AM -->|Step 3: Check| BC["💰 Budget Checker<br/>Agent"]

        RP <-->|R/W| DB[("💾 Shared SQLite<br/>MemoryStore")]
        PR <-->|R/W| DB
        FS <-->|R/W| DB
        BC <-->|R/W| DB
        AM <-->|R/W| DB

        PR <-->|Embed & Search| VDB[("🔍 ChromaDB<br/>Policy Vector Store")]

        AM -->|Step 4| DR["⚖️ Deterministic<br/>Decision Rules"]
        DR -->|APPROVE| OUT1["✅ Approved"]
        DR -->|MODIFY| OUT2["🔄 Modified"]
        DR -->|ESCALATE| HQ["🚨 Human Review<br/>Queue"]

        HQ -->|Verdict| DB
    ```
    """)

    st.divider()

    st.markdown("""
    ### Agent Communication Flow

    | Step | Agent | Input | Output | Memory |
    |:---:|:---|:---|:---|:---|
    | 1 | **Request Parser** | Raw text | `ParsedRequest` | Reads employee profile, writes parsed fields |
    | 2a | **Policy Retrieval** | `ParsedRequest` | `RetrievedPolicy` | Reads prior trips, writes clause results |
    | 2b | **Fare Search** | `ParsedRequest` | `FareOptions` | Reads cabin prefs, writes fares found |
    | 3 | **Budget Checker** | `ParsedRequest` + `FareOptions` + `RetrievedPolicy` | `BudgetCheck` | Reads budget & history, writes compliance |
    | 4 | **Approval Manager** | All outputs | `AuditResult` | Reads quarter count, writes decision + audit + trip |

    Steps 2a and 2b run **concurrently** (asyncio.gather) through the shared rate limiter.

    ### Key Design Points
    - **LLM is never used for decisions** — only for parsing, re-ranking, and justification text
    - **Deterministic rule engine** in `decision_rules.py` with strict clause precedence
    - **Graceful degradation** — if LLM is unavailable, regex parser and template justifications kick in
    - **Shared memory** — all 5 agents read from and write to the same SQLite store via `MemoryStore`
    """)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()

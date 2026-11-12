"""
Streamlit Human Review UI for the Corporate Travel Policy Booking Auditor.

Run: streamlit run ui/review_app.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

import config
from core import hitl
from core.memory import MemoryStore


def get_memory() -> MemoryStore:
    """Get or create a MemoryStore (cached in session state)."""
    if "memory" not in st.session_state:
        memory = MemoryStore(config.SQLITE_DB_PATH)
        row = memory.conn.execute("SELECT COUNT(*) as cnt FROM employees").fetchone()
        if row["cnt"] == 0:
            memory.seed_from_json(config.EMPLOYEES_PATH, config.BUDGETS_PATH, config.TRIP_HISTORY_SEED_PATH)
        st.session_state.memory = memory
    return st.session_state.memory


def main():
    st.set_page_config(
        page_title="Travel Auditor — Human Review",
        page_icon="✈️",
        layout="wide",
    )

    st.title("✈️ Corporate Travel Policy Booking Auditor")
    st.subheader("Human Review Queue")

    memory = get_memory()

    # Sidebar: navigation
    page = st.sidebar.selectbox("Page", ["Pending Reviews", "All Reviews", "Audit Trail"])

    if page == "Pending Reviews":
        _show_pending(memory)
    elif page == "All Reviews":
        _show_all(memory)
    elif page == "Audit Trail":
        _show_audit_trail(memory)


def _show_pending(memory: MemoryStore):
    """Show pending reviews with verdict form."""
    items = hitl.get_pending_reviews(memory)

    if not items:
        st.info("🎉 No pending reviews. All caught up!")
        return

    st.write(f"**{len(items)}** pending review(s)")

    for item in items:
        with st.expander(f"📋 {item.request_id}", expanded=True):
            # Show audit result details
            if item.audit_result_json:
                audit = json.loads(item.audit_result_json)
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Decision", audit.get("decision", "N/A"))
                with col2:
                    st.metric("Primary Clause", audit.get("primary_clause_id", "N/A"))
                with col3:
                    st.metric("Status", item.status)

                st.write("**Justification:**", audit.get("justification", "N/A"))

                if audit.get("secondary_clause_ids"):
                    st.write("**Secondary Clauses:**", ", ".join(audit["secondary_clause_ids"]))

                if audit.get("best_compliant_fare"):
                    fare = audit["best_compliant_fare"]
                    st.write(f"**Best Fare:** {fare.get('airline', 'N/A')} INR {fare.get('price_inr', 0):,.0f}")

            # Show audit trail for this request
            logs = memory.get_audit_log(item.request_id)
            if logs:
                with st.container():
                    st.write("**Agent Audit Trail:**")
                    for log in logs:
                        st.text(f"  [{log.ts}] {log.agent}: {log.action}")

            # Verdict form
            st.divider()
            st.write("**Submit Verdict:**")
            col_v, col_n = st.columns([1, 3])
            with col_v:
                verdict = st.selectbox(
                    "Verdict",
                    ["approve", "reject", "modify"],
                    key=f"verdict_{item.request_id}",
                )
            with col_n:
                note = st.text_input(
                    "Note",
                    key=f"note_{item.request_id}",
                    placeholder="Add a reviewer note...",
                )

            reviewer = st.text_input(
                "Reviewer Name",
                value="admin",
                key=f"reviewer_{item.request_id}",
            )

            # Get fare for budget deduction
            fare_inr = None
            if item.audit_result_json:
                audit = json.loads(item.audit_result_json)
                if audit.get("best_compliant_fare"):
                    fare_inr = audit["best_compliant_fare"].get("price_inr")

            if st.button("Submit Verdict", key=f"submit_{item.request_id}", type="primary"):
                try:
                    result = hitl.submit_verdict(
                        item.request_id, verdict, note, reviewer, memory, fare_inr,
                    )
                    st.success(f"✓ Verdict recorded: {result['final_status']}")
                    st.rerun()
                except ValueError as e:
                    st.error(f"Error: {e}")


def _show_all(memory: MemoryStore):
    """Show all review items."""
    items = hitl.get_all_reviews(memory)

    if not items:
        st.info("No review items found.")
        return

    for item in items:
        status_color = {
            "PENDING": "🟡",
            "HUMAN_APPROVED": "🟢",
            "HUMAN_REJECTED": "🔴",
            "HUMAN_MODIFIED": "🔵",
        }.get(item.status, "⚪")

        with st.expander(f"{status_color} {item.request_id} — {item.status}"):
            if item.audit_result_json:
                audit = json.loads(item.audit_result_json)
                st.json(audit)
            if item.human_verdict:
                st.write(f"**Verdict:** {item.human_verdict}")
                st.write(f"**Note:** {item.human_note}")
                st.write(f"**Reviewed At:** {item.reviewed_at}")


def _show_audit_trail(memory: MemoryStore):
    """Show the full audit trail."""
    logs = memory.get_all_audit_log()

    if not logs:
        st.info("No audit log entries.")
        return

    st.write(f"**{len(logs)}** audit log entries")

    # Group by request_id
    by_request: dict = {}
    for log in logs:
        by_request.setdefault(log.request_id, []).append(log)

    for req_id, entries in by_request.items():
        with st.expander(f"🔍 {req_id} ({len(entries)} entries)"):
            for entry in entries:
                st.text(f"[{entry.ts}] {entry.agent}: {entry.action}")
                if entry.payload_json:
                    try:
                        st.json(json.loads(entry.payload_json))
                    except json.JSONDecodeError:
                        st.text(entry.payload_json)


if __name__ == "__main__":
    main()

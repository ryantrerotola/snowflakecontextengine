"""
Review & Edit Page
==================
Browse, search, and edit all captured context facts.
View by category, by entity, or in timeline order.
Each fact is editable, deletable, and traceable to its source.
"""

import streamlit as st

from lib.context_store import (
    list_sessions,
    get_all_facts,
    get_facts,
    update_fact,
    delete_fact,
    get_responses,
    get_documents,
)

st.set_page_config(page_title="Review & Edit", page_icon="📝", layout="wide")
st.title("📝 Review & Edit Context")

# ---------------------------------------------------------------------------
# Session Check
# ---------------------------------------------------------------------------
if "interview_session_id" not in st.session_state:
    st.info("Please select a session first.")
    try:
        sessions = list_sessions()
        for s in sessions:
            if st.button(f"Open: {s['SESSION_NAME']}", key=s["SESSION_ID"]):
                st.session_state.interview_session_id = s["SESSION_ID"]
                st.rerun()
    except Exception:
        pass
    st.stop()

session_id = st.session_state.interview_session_id

# ---------------------------------------------------------------------------
# View Mode Selection
# ---------------------------------------------------------------------------
view_mode = st.radio(
    "View Mode",
    ["By Category", "By Entity", "Timeline", "All Facts"],
    horizontal=True,
)

CATEGORY_LABELS = {
    "terminology": ("📖", "Business Terminology"),
    "metric_definition": ("📊", "Metrics & KPIs"),
    "business_rule": ("📋", "Business Rules"),
    "table_description": ("🗃️", "Table Descriptions"),
    "relationship": ("🔗", "Relationships"),
    "caveat": ("⚠️", "Caveats & Gotchas"),
    "access_pattern": ("🔒", "Access Patterns"),
}

try:
    all_facts = get_all_facts(session_id)
except Exception as e:
    st.error(f"Could not load facts: {e}")
    st.stop()

if not all_facts:
    st.info("No context facts captured yet. Start an interview or upload documents to get started.")
    st.stop()

st.markdown(f"**{len(all_facts)} total facts** captured in this session.")

# ---------------------------------------------------------------------------
# By Category View
# ---------------------------------------------------------------------------
if view_mode == "By Category":
    # Group by category
    by_category = {}
    for fact in all_facts:
        cat = fact.get("CATEGORY", "other")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(fact)

    for cat_id, (icon, label) in CATEGORY_LABELS.items():
        facts_in_cat = by_category.get(cat_id, [])
        count = len(facts_in_cat)

        with st.expander(f"{icon} {label} ({count})", expanded=count > 0):
            if not facts_in_cat:
                st.markdown("*No facts in this category yet.*")
                continue

            for fact in facts_in_cat:
                _render_fact_card(fact)

    # Show uncategorized facts
    other_facts = [f for f in all_facts if f.get("CATEGORY") not in CATEGORY_LABELS]
    if other_facts:
        with st.expander(f"❓ Other ({len(other_facts)})"):
            for fact in other_facts:
                _render_fact_card(fact)

# ---------------------------------------------------------------------------
# By Entity View
# ---------------------------------------------------------------------------
elif view_mode == "By Entity":
    # Group by entity (table)
    by_entity = {}
    unlinked = []
    for fact in all_facts:
        table = fact.get("ENTITY_TABLE")
        if table:
            key = table
            if fact.get("ENTITY_SCHEMA"):
                key = f"{fact['ENTITY_SCHEMA']}.{table}"
            if fact.get("ENTITY_DATABASE"):
                key = f"{fact['ENTITY_DATABASE']}.{key}"
            if key not in by_entity:
                by_entity[key] = []
            by_entity[key].append(fact)
        else:
            unlinked.append(fact)

    if by_entity:
        for entity_name in sorted(by_entity.keys()):
            facts = by_entity[entity_name]
            with st.expander(f"🗃️ {entity_name} ({len(facts)} facts)"):
                for fact in facts:
                    _render_fact_card(fact)

    if unlinked:
        with st.expander(f"📌 General (not linked to a table) ({len(unlinked)})"):
            for fact in unlinked:
                _render_fact_card(fact)

# ---------------------------------------------------------------------------
# Timeline View
# ---------------------------------------------------------------------------
elif view_mode == "Timeline":
    # Show in chronological order
    sorted_facts = sorted(all_facts, key=lambda f: f.get("CREATED_AT", ""))
    for fact in sorted_facts:
        _render_fact_card(fact, show_timestamp=True)

# ---------------------------------------------------------------------------
# All Facts View (table)
# ---------------------------------------------------------------------------
elif view_mode == "All Facts":
    import pandas as pd

    df = pd.DataFrame([
        {
            "Category": CATEGORY_LABELS.get(f["CATEGORY"], ("", f["CATEGORY"]))[1],
            "Fact": f["FACT_TEXT"][:200],
            "Table": f.get("ENTITY_TABLE", ""),
            "Column": f.get("ENTITY_COLUMN", ""),
            "Source": f.get("SOURCE_TYPE", ""),
            "Verified": "✅" if f.get("IS_VERIFIED") else "",
            "ID": f["FACT_ID"],
        }
        for f in all_facts
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Fact Card Renderer
# ---------------------------------------------------------------------------
def _render_fact_card(fact: dict, show_timestamp: bool = False):
    """Render an individual fact with edit/delete controls."""
    fact_id = fact["FACT_ID"]
    cat = fact.get("CATEGORY", "")
    cat_label = CATEGORY_LABELS.get(cat, ("", cat))[1]

    col1, col2, col3 = st.columns([0.7, 0.15, 0.15])

    with col1:
        # Show fact text
        if fact.get("IS_VERIFIED"):
            st.markdown(f"✅ {fact['FACT_TEXT']}")
        else:
            st.markdown(fact["FACT_TEXT"])

        # Metadata line
        meta_parts = []
        if cat_label:
            meta_parts.append(f"`{cat_label}`")
        if fact.get("ENTITY_TABLE"):
            entity = fact["ENTITY_TABLE"]
            if fact.get("ENTITY_COLUMN"):
                entity += f".{fact['ENTITY_COLUMN']}"
            meta_parts.append(f"→ `{entity}`")
        meta_parts.append(f"Source: {fact.get('SOURCE_TYPE', 'Unknown')}")
        if show_timestamp and fact.get("CREATED_AT"):
            meta_parts.append(str(fact["CREATED_AT"]))
        st.caption(" | ".join(meta_parts))

    with col2:
        if st.button("✏️ Edit", key=f"edit_{fact_id}"):
            st.session_state[f"editing_{fact_id}"] = True

    with col3:
        if st.button("🗑️", key=f"del_{fact_id}"):
            try:
                delete_fact(fact_id)
                st.success("Deleted")
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")

    # Edit form
    if st.session_state.get(f"editing_{fact_id}"):
        with st.form(f"edit_form_{fact_id}"):
            new_text = st.text_area("Fact Text", value=fact["FACT_TEXT"], key=f"text_{fact_id}")
            new_cat = st.selectbox(
                "Category",
                options=list(CATEGORY_LABELS.keys()),
                index=list(CATEGORY_LABELS.keys()).index(cat) if cat in CATEGORY_LABELS else 0,
                format_func=lambda x: CATEGORY_LABELS[x][1],
                key=f"cat_{fact_id}",
            )
            verified = st.checkbox("Mark as Verified", value=fact.get("IS_VERIFIED", False),
                                   key=f"ver_{fact_id}")

            col_save, col_cancel = st.columns(2)
            with col_save:
                if st.form_submit_button("Save"):
                    try:
                        update_fact(fact_id, fact_text=new_text, category=new_cat,
                                    is_verified=verified)
                        del st.session_state[f"editing_{fact_id}"]
                        st.success("Updated!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed: {e}")
            with col_cancel:
                if st.form_submit_button("Cancel"):
                    del st.session_state[f"editing_{fact_id}"]
                    st.rerun()

    st.divider()

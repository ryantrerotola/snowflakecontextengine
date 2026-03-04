"""
Context Interview Page
======================
Adaptive conversational interview that progressively builds context about
the user's Snowflake data environment. Chat-style UI with sidebar progress.
"""

import streamlit as st

from lib.interview_engine import (
    TOPIC_AREAS,
    get_current_topic_index,
    get_next_question,
    get_starter_question,
    get_coverage_scores,
)
from lib.answer_processor import process_answer
from lib.context_store import (
    list_sessions,
    get_responses,
    get_all_facts,
)
from lib.snowflake_connection import (
    get_databases,
    introspect_schema,
    get_schemas,
)
from lib.context_store import cache_schema

st.set_page_config(page_title="Context Interview", page_icon="💬", layout="wide")
st.title("💬 Context Interview")

# ---------------------------------------------------------------------------
# Session Check
# ---------------------------------------------------------------------------
if "interview_session_id" not in st.session_state:
    st.info("Please create or select a session from the Upload Documents page first.")

    try:
        sessions = list_sessions()
        if sessions:
            st.markdown("### Existing Sessions")
            for s in sessions:
                if st.button(f"Resume: {s['SESSION_NAME']}", key=s["SESSION_ID"]):
                    st.session_state.interview_session_id = s["SESSION_ID"]
                    st.rerun()
    except Exception:
        pass
    st.stop()

session_id = st.session_state.interview_session_id

# ---------------------------------------------------------------------------
# Initialize Chat State
# ---------------------------------------------------------------------------
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []
if "current_topic_id" not in st.session_state:
    st.session_state.current_topic_id = TOPIC_AREAS[0]["id"]
if "sequence_number" not in st.session_state:
    st.session_state.sequence_number = 0
if "interview_started" not in st.session_state:
    st.session_state.interview_started = False
if "free_mode" not in st.session_state:
    st.session_state.free_mode = False

# ---------------------------------------------------------------------------
# Sidebar: Schema Discovery + Progress
# ---------------------------------------------------------------------------
st.sidebar.header("Schema Discovery")

if "schema_discovered" not in st.session_state:
    st.session_state.schema_discovered = False

if not st.session_state.schema_discovered:
    st.sidebar.markdown("Connect to your Snowflake environment to auto-discover tables.")
    if st.sidebar.button("Discover Schema"):
        with st.sidebar.status("Discovering schema...", expanded=True):
            try:
                databases = get_databases()
                st.sidebar.write(f"Found {len(databases)} databases")

                all_columns = []
                for db in databases[:10]:  # Limit to prevent timeout
                    try:
                        schemas = get_schemas(db)
                        for schema in schemas[:20]:
                            cols = introspect_schema(db, schema)
                            all_columns.extend(cols)
                    except Exception:
                        continue

                if all_columns:
                    cache_schema(session_id, all_columns)
                    st.session_state.schema_discovered = True
                    st.sidebar.success(f"Discovered {len(all_columns)} columns")
                    st.rerun()
                else:
                    st.sidebar.warning("No columns discovered.")
            except Exception as e:
                st.sidebar.error(f"Discovery failed: {e}")
                st.sidebar.info("You can proceed without schema discovery.")
else:
    st.sidebar.success("Schema discovered")

st.sidebar.markdown("---")

# Progress through topics
st.sidebar.header("Interview Progress")

try:
    scores = get_coverage_scores(session_id)
except Exception:
    scores = {}

for topic in TOPIC_AREAS:
    score = scores.get(topic["id"], 0.0)
    if score >= 0.8:
        icon = "✅"
    elif score > 0:
        icon = "🔄"
    else:
        icon = "⬜"

    label = f"{icon} {topic['icon']} {topic['name']}"
    if st.sidebar.button(label, key=f"topic_{topic['id']}",
                         use_container_width=True):
        st.session_state.current_topic_id = topic["id"]
        st.session_state.free_mode = False
        # Generate the starter question for this topic
        question = get_starter_question(session_id, topic["id"])
        st.session_state.chat_messages.append({
            "role": "assistant",
            "content": question,
            "topic_id": topic["id"],
        })
        st.rerun()

st.sidebar.markdown("---")
if st.sidebar.button("🗣️ Free-form Mode", use_container_width=True):
    st.session_state.free_mode = True
    st.session_state.chat_messages.append({
        "role": "assistant",
        "content": (
            "Free-form mode! Just type whatever comes to mind about your data "
            "environment. Business rules, metrics definitions, table descriptions, "
            "caveats — anything that would help an AI agent understand your data better. "
            "I'll extract the relevant context from whatever you share."
        ),
        "topic_id": "free_form",
    })
    st.rerun()

# ---------------------------------------------------------------------------
# Start Interview
# ---------------------------------------------------------------------------
if not st.session_state.interview_started:
    st.markdown(
        """
        ### Ready to start the context interview?

        I'll ask you a series of open-ended questions about your data environment.
        Each question builds on your previous answers to go deeper.

        **Tips:**
        - Don't worry about being perfectly structured — stream of consciousness is great
        - You can skip topics, jump to any topic from the sidebar, or switch to free-form mode
        - Every answer is summarized and broken into individual facts you can review later
        - You can pause and resume the interview any time
        """
    )

    if st.button("Start Interview", type="primary"):
        st.session_state.interview_started = True

        # First question
        topic = TOPIC_AREAS[0]
        question = get_starter_question(session_id, topic["id"])
        st.session_state.chat_messages.append({
            "role": "assistant",
            "content": question,
            "topic_id": topic["id"],
        })
        st.rerun()
    st.stop()

# ---------------------------------------------------------------------------
# Chat Display
# ---------------------------------------------------------------------------
for msg in st.session_state.chat_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

        # Show summary for processed answers
        if msg["role"] == "user" and msg.get("summary"):
            with st.expander("📝 AI Summary", expanded=False):
                st.markdown(msg["summary"])
            if msg.get("facts"):
                with st.expander(f"📌 Extracted Facts ({len(msg['facts'])})", expanded=False):
                    for fact in msg["facts"]:
                        cat = fact.get("category", "").replace("_", " ").title()
                        st.markdown(f"- **[{cat}]** {fact.get('fact', '')}")

# ---------------------------------------------------------------------------
# Chat Input
# ---------------------------------------------------------------------------
if user_input := st.chat_input("Type your answer here..."):
    # Display user message
    st.session_state.chat_messages.append({
        "role": "user",
        "content": user_input,
    })
    with st.chat_message("user"):
        st.markdown(user_input)

    # Process the answer
    current_topic = st.session_state.current_topic_id
    st.session_state.sequence_number += 1

    with st.spinner("Processing your answer..."):
        result = process_answer(
            session_id=session_id,
            topic_area=current_topic,
            question=st.session_state.chat_messages[-2]["content"]
            if len(st.session_state.chat_messages) >= 2
            else "",
            raw_answer=user_input,
            sequence_number=st.session_state.sequence_number,
        )

    # Update the user message with processing results
    st.session_state.chat_messages[-1]["summary"] = result["summary"]
    st.session_state.chat_messages[-1]["facts"] = result["facts"]

    # Generate next question
    if st.session_state.free_mode:
        # In free-form mode, just acknowledge and invite more
        next_q = {
            "question": (
                f"Got it — I extracted {len(result['facts'])} facts from that. "
                "Keep going! What else should I know about your data?"
            ),
            "topic_id": "free_form",
            "is_followup": False,
            "is_review": False,
        }
    else:
        next_q = get_next_question(
            session_id=session_id,
            current_topic_id=current_topic,
            user_answer=user_input,
        )
        st.session_state.current_topic_id = next_q["topic_id"]

    # Display assistant response
    st.session_state.chat_messages.append({
        "role": "assistant",
        "content": next_q["question"],
        "topic_id": next_q["topic_id"],
    })

    st.rerun()

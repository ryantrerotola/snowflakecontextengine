"""
Snowflake Context Engine
========================
A Streamlit-in-Snowflake application that helps teams upload and generate
rich context for Snowflake Intelligence agents (Cortex Analyst).

Combines document uploads with an adaptive conversational interview to
capture business rules, metrics, terminology, and data relationships.
Outputs Semantic View YAML, Verified Query Repository, and Custom Instructions.
"""

import streamlit as st

st.set_page_config(
    page_title="Snowflake Context Engine",
    page_icon="❄️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.sidebar.title("❄️ Context Engine")
st.sidebar.markdown("Build context for Snowflake Intelligence agents.")

st.title("Snowflake Context Engine")
st.markdown(
    """
    Welcome to the **Snowflake Context Engine**. This tool helps you build rich context
    that improves the accuracy of Snowflake Intelligence agents (Cortex Analyst).

    ### How it works

    1. **Upload Documents** — Add PDFs, PowerPoints, Word docs, and text files
       containing business rules, data dictionaries, and documentation.

    2. **Context Interview** — An adaptive conversation that asks you about your
       databases, schemas, business terminology, metrics, and data caveats.
       Each question builds on your previous answers.

    3. **Review & Edit** — Browse, search, and edit all captured context.
       Every fact is traceable back to its source.

    4. **Export & Deploy** — Generate Semantic View YAML, Verified Query Repository,
       and Custom Instructions, then deploy directly to Snowflake.

    ---

    Use the sidebar to navigate between pages.
    """
)

# Show quick stats if we have a session
if "interview_session_id" in st.session_state:
    st.sidebar.markdown("---")
    st.sidebar.markdown("### Current Session")
    st.sidebar.markdown(f"Session: `{st.session_state.interview_session_id[:8]}...`")

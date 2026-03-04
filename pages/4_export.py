"""
Export & Deploy Page
====================
Generate, preview, and deploy Snowflake-native artifacts:
- Semantic View YAML
- Verified Query Repository (VQR)
- Custom Instructions
- Human-readable Markdown documentation
"""

import streamlit as st

from lib.yaml_generator import generate_semantic_yaml
from lib.vqr_generator import generate_vqr
from lib.instructions_generator import generate_custom_instructions, generate_markdown_doc
from lib.context_store import (
    list_sessions,
    save_artifact,
    get_latest_artifact,
    get_artifact_history,
    get_all_facts,
)
from lib.snowflake_connection import get_session, run_ddl

st.set_page_config(page_title="Export & Deploy", page_icon="🚀", layout="wide")
st.title("🚀 Export & Deploy")

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

# Quick stats
try:
    facts = get_all_facts(session_id)
    st.markdown(f"**Session has {len(facts)} context facts** to generate from.")
except Exception:
    facts = []

if not facts:
    st.warning("No context captured yet. Run an interview or upload documents first.")
    st.stop()

# ---------------------------------------------------------------------------
# Generate Artifacts
# ---------------------------------------------------------------------------
tab_yaml, tab_vqr, tab_instructions, tab_markdown = st.tabs([
    "📐 Semantic View YAML",
    "✅ Verified Queries (VQR)",
    "📋 Custom Instructions",
    "📄 Markdown Doc",
])

# --- Semantic YAML ---
with tab_yaml:
    st.markdown("### Semantic View YAML")
    st.markdown("Generates a Cortex Analyst semantic model from your captured context.")

    model_name = st.text_input("Model Name", value="my_analytics",
                               help="Name for the semantic model")

    if st.button("Generate YAML", type="primary", key="gen_yaml"):
        with st.spinner("Generating semantic model YAML..."):
            yaml_content = generate_semantic_yaml(session_id, model_name)

        st.session_state.generated_yaml = yaml_content
        save_artifact(session_id, "SEMANTIC_YAML", f"{model_name}.yaml", yaml_content)
        st.success("YAML generated and saved!")

    # Display latest
    if "generated_yaml" in st.session_state:
        st.code(st.session_state.generated_yaml, language="yaml")
        st.download_button(
            "Download YAML",
            data=st.session_state.generated_yaml,
            file_name=f"{model_name}.yaml",
            mime="text/yaml",
        )
    else:
        latest = get_latest_artifact(session_id, "SEMANTIC_YAML")
        if latest:
            st.code(latest["CONTENT"], language="yaml")
            st.download_button(
                "Download YAML",
                data=latest["CONTENT"],
                file_name=f"{latest.get('ARTIFACT_NAME', 'semantic_model.yaml')}",
                mime="text/yaml",
            )

    # Deploy options
    st.markdown("---")
    st.markdown("#### Deploy to Snowflake")

    col1, col2 = st.columns(2)
    with col1:
        stage_name = st.text_input("Stage Name", value="@CONTEXT_ENGINE.UPLOADS",
                                    key="yaml_stage")
        if st.button("Upload YAML to Stage"):
            yaml_to_deploy = st.session_state.get("generated_yaml") or ""
            if yaml_to_deploy:
                try:
                    # Write YAML to stage via Snowpark
                    session = get_session()
                    import tempfile, os
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                                     delete=False) as tmp:
                        tmp.write(yaml_to_deploy)
                        tmp_path = tmp.name
                    try:
                        session.file.put(tmp_path, stage_name, auto_compress=False,
                                         overwrite=True)
                        st.success(f"YAML uploaded to {stage_name}")
                    finally:
                        os.unlink(tmp_path)
                except Exception as e:
                    st.error(f"Failed to upload: {e}")
            else:
                st.warning("Generate YAML first.")

    with col2:
        sv_name = st.text_input("Semantic View Name", value=model_name,
                                key="sv_name")
        if st.button("Create Semantic View"):
            yaml_to_deploy = st.session_state.get("generated_yaml") or ""
            if yaml_to_deploy:
                try:
                    escaped_yaml = yaml_to_deploy.replace("'", "''")
                    run_ddl(
                        f"SELECT SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML('{escaped_yaml}')"
                    )
                    st.success(f"Semantic View '{sv_name}' created!")
                except Exception as e:
                    st.error(f"Failed to create semantic view: {e}")
            else:
                st.warning("Generate YAML first.")

# --- VQR ---
with tab_vqr:
    st.markdown("### Verified Query Repository")
    st.markdown(
        "Auto-generates example questions and SQL queries from your metrics and "
        "common query patterns. **All queries are marked as DRAFT** — review and "
        "verify each one before deploying."
    )

    if st.button("Generate VQR", type="primary", key="gen_vqr"):
        with st.spinner("Generating verified queries..."):
            vqr_content = generate_vqr(session_id)

        st.session_state.generated_vqr = vqr_content
        save_artifact(session_id, "VQR_YAML", "verified_queries.yaml", vqr_content)
        st.success("VQR generated and saved!")

    if "generated_vqr" in st.session_state:
        st.code(st.session_state.generated_vqr, language="yaml")
        st.download_button(
            "Download VQR YAML",
            data=st.session_state.generated_vqr,
            file_name="verified_queries.yaml",
            mime="text/yaml",
        )
    else:
        latest = get_latest_artifact(session_id, "VQR_YAML")
        if latest:
            st.code(latest["CONTENT"], language="yaml")

# --- Custom Instructions ---
with tab_instructions:
    st.markdown("### Custom Instructions")
    st.markdown(
        "Plain text directives that guide Cortex Analyst's SQL generation behavior. "
        "These encode your business rules, data caveats, and terminology."
    )

    if st.button("Generate Instructions", type="primary", key="gen_inst"):
        with st.spinner("Generating custom instructions..."):
            instructions = generate_custom_instructions(session_id)

        st.session_state.generated_instructions = instructions
        save_artifact(session_id, "CUSTOM_INSTRUCTIONS", "custom_instructions.txt",
                      instructions)
        st.success("Instructions generated and saved!")

    if "generated_instructions" in st.session_state:
        # Editable text area
        edited = st.text_area(
            "Custom Instructions (editable)",
            value=st.session_state.generated_instructions,
            height=400,
        )
        if edited != st.session_state.generated_instructions:
            if st.button("Save Edits"):
                st.session_state.generated_instructions = edited
                save_artifact(session_id, "CUSTOM_INSTRUCTIONS",
                              "custom_instructions.txt", edited)
                st.success("Saved!")

        st.download_button(
            "Download Instructions",
            data=st.session_state.generated_instructions,
            file_name="custom_instructions.txt",
            mime="text/plain",
        )
    else:
        latest = get_latest_artifact(session_id, "CUSTOM_INSTRUCTIONS")
        if latest:
            st.text_area("Custom Instructions", value=latest["CONTENT"], height=400,
                         disabled=True)

# --- Markdown Doc ---
with tab_markdown:
    st.markdown("### Human-Readable Context Document")
    st.markdown(
        "A comprehensive Markdown document with all captured context, organized "
        "for easy reading and editing by your team."
    )

    if st.button("Generate Document", type="primary", key="gen_md"):
        with st.spinner("Generating document..."):
            md_content = generate_markdown_doc(session_id)

        st.session_state.generated_markdown = md_content
        save_artifact(session_id, "MARKDOWN_DOC", "context_document.md", md_content)
        st.success("Document generated and saved!")

    if "generated_markdown" in st.session_state:
        st.markdown(st.session_state.generated_markdown)
        st.download_button(
            "Download Markdown",
            data=st.session_state.generated_markdown,
            file_name="context_document.md",
            mime="text/markdown",
        )
    else:
        latest = get_latest_artifact(session_id, "MARKDOWN_DOC")
        if latest:
            st.markdown(latest["CONTENT"])

# ---------------------------------------------------------------------------
# Version History
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### Artifact Version History")

for artifact_type, label in [
    ("SEMANTIC_YAML", "Semantic YAML"),
    ("VQR_YAML", "Verified Queries"),
    ("CUSTOM_INSTRUCTIONS", "Custom Instructions"),
    ("MARKDOWN_DOC", "Markdown Doc"),
]:
    try:
        history = get_artifact_history(session_id, artifact_type)
        if history:
            with st.expander(f"{label} — {len(history)} version(s)"):
                for h in history:
                    st.markdown(
                        f"**v{h['VERSION']}** — {h['CREATED_AT']} — "
                        f"{'🚀 Deployed' if h.get('IS_DEPLOYED') else 'Draft'}"
                    )
    except Exception:
        pass

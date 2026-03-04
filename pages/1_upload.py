"""
Upload Documents Page
=====================
Allows users to upload PDFs, PowerPoints, Word docs, and text files.
Files are staged in Snowflake, parsed, summarized, and context is extracted.
"""

import streamlit as st

from lib.snowflake_connection import get_session
from lib.document_processor import process_file, stage_file
from lib.context_extractor import extract_facts_from_text
from lib.context_store import (
    save_document,
    update_document,
    get_documents,
    create_session,
    list_sessions,
)

st.set_page_config(page_title="Upload Documents", page_icon="📄", layout="wide")
st.title("📄 Upload Documents")
st.markdown("Upload files containing business context, data dictionaries, or documentation.")

# ---------------------------------------------------------------------------
# Session Selection / Creation
# ---------------------------------------------------------------------------
st.sidebar.header("Session")

sessions = []
try:
    sessions = list_sessions()
except Exception:
    st.sidebar.warning("Could not load sessions. Ensure database objects are created.")

session_options = ["+ Create New Session"] + [
    f"{s['SESSION_NAME']} ({s['SESSION_ID'][:8]}...)" for s in sessions
]
selected = st.sidebar.selectbox("Select Session", session_options)

if selected == "+ Create New Session":
    new_name = st.sidebar.text_input("Session Name", value="My Context Session")
    if st.sidebar.button("Create Session"):
        try:
            sid = create_session(new_name)
            st.session_state.interview_session_id = sid
            st.sidebar.success(f"Created session: {sid[:8]}...")
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"Failed to create session: {e}")
elif sessions:
    idx = session_options.index(selected) - 1
    st.session_state.interview_session_id = sessions[idx]["SESSION_ID"]

# ---------------------------------------------------------------------------
# File Upload
# ---------------------------------------------------------------------------
if "interview_session_id" not in st.session_state:
    st.info("Please create or select a session from the sidebar to begin uploading.")
    st.stop()

session_id = st.session_state.interview_session_id

st.markdown("### Upload Files")
uploaded_files = st.file_uploader(
    "Drag and drop files here",
    type=["pdf", "pptx", "docx", "txt", "csv", "md"],
    accept_multiple_files=True,
    help="Supported formats: PDF, PowerPoint, Word, plain text, CSV, Markdown",
)

if uploaded_files:
    st.markdown(f"**{len(uploaded_files)} file(s) selected**")

    if st.button("Process Files", type="primary"):
        progress = st.progress(0, text="Processing files...")

        for i, uploaded_file in enumerate(uploaded_files):
            file_type = uploaded_file.name.rsplit(".", 1)[-1].lower()
            progress.progress(
                (i) / len(uploaded_files),
                text=f"Processing {uploaded_file.name}...",
            )

            with st.status(f"Processing: {uploaded_file.name}", expanded=True) as status:
                # Stage the file
                st.write("Staging file to Snowflake...")
                try:
                    stage_path = stage_file(uploaded_file, session_id)
                except Exception as e:
                    st.error(f"Failed to stage file: {e}")
                    status.update(label=f"Failed: {uploaded_file.name}", state="error")
                    continue

                # Save document metadata
                doc_id = save_document(
                    session_id=session_id,
                    filename=uploaded_file.name,
                    file_type=file_type,
                    file_size_bytes=uploaded_file.size,
                    stage_path=stage_path,
                )

                # Process the file
                st.write("Extracting text content...")
                result = process_file(uploaded_file, stage_path)

                # Update document with results
                update_document(
                    doc_id=doc_id,
                    extracted_text=result["extracted_text"],
                    summary=result["summary"],
                    status=result["status"],
                    error_message=result["error"],
                )

                if result["status"] == "COMPLETED":
                    st.write("Text extracted successfully.")

                    if result["summary"]:
                        st.write("**Summary:**")
                        st.info(result["summary"])

                    # Extract context facts
                    st.write("Extracting context facts...")
                    facts = extract_facts_from_text(
                        text=result["extracted_text"],
                        session_id=session_id,
                        source_type="DOCUMENT",
                        source_id=doc_id,
                    )
                    st.write(f"Extracted **{len(facts)}** context facts.")

                    status.update(
                        label=f"Completed: {uploaded_file.name} ({len(facts)} facts)",
                        state="complete",
                    )
                else:
                    st.error(f"Processing failed: {result['error']}")
                    status.update(label=f"Failed: {uploaded_file.name}", state="error")

        progress.progress(1.0, text="All files processed!")

# ---------------------------------------------------------------------------
# Previously Uploaded Documents
# ---------------------------------------------------------------------------
st.markdown("---")
st.markdown("### Previously Uploaded Documents")

try:
    docs = get_documents(session_id)
    if docs:
        for doc in docs:
            icon = {
                "pdf": "📕",
                "pptx": "📊",
                "docx": "📝",
                "txt": "📄",
                "csv": "📈",
                "md": "📋",
            }.get(doc["FILE_TYPE"], "📎")
            status_icon = "✅" if doc["PROCESSING_STATUS"] == "COMPLETED" else "⏳" if doc["PROCESSING_STATUS"] == "PENDING" else "❌"

            with st.expander(f"{icon} {doc['FILENAME']} {status_icon}"):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**Type:** {doc['FILE_TYPE'].upper()}")
                    st.markdown(f"**Size:** {doc['FILE_SIZE_BYTES']:,} bytes")
                with col2:
                    st.markdown(f"**Status:** {doc['PROCESSING_STATUS']}")
                    st.markdown(f"**Uploaded:** {doc['UPLOADED_AT']}")

                if doc.get("SUMMARY"):
                    st.markdown("**Summary:**")
                    st.info(doc["SUMMARY"])

                if doc.get("ERROR_MESSAGE"):
                    st.error(doc["ERROR_MESSAGE"])
    else:
        st.info("No documents uploaded yet for this session.")
except Exception as e:
    st.warning(f"Could not load documents: {e}")

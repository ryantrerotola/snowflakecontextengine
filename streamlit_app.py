"""
Snowflake Context Engine — Single-File Streamlit App
=====================================================
A Streamlit-in-Snowflake application that helps teams upload and generate
rich context for Snowflake Intelligence agents (Cortex Analyst).

Combines document uploads with an adaptive conversational interview to
capture business rules, metrics, terminology, and data relationships.
Outputs Semantic View YAML, Verified Query Repository, and Custom Instructions.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import uuid
from datetime import date, datetime
from typing import Dict, List, Optional

import streamlit as st
import yaml
from snowflake.snowpark import Session

try:
    from snowflake.snowpark.context import get_active_session as _get_active_session
except ImportError:
    _get_active_session = None

try:
    from pptx import Presentation as _Presentation
except ImportError:
    _Presentation = None

try:
    from docx import Document as _Document
except ImportError:
    _Document = None

try:
    from openai import OpenAI as _OpenAI
except ImportError:
    _OpenAI = None


# =============================================================================
# SNOWFLAKE CONNECTION
# =============================================================================

def get_session() -> Session:
    """Get or create a Snowpark session."""
    if "snowpark_session" not in st.session_state:
        try:
            if _get_active_session is None:
                raise RuntimeError("Not running in Snowflake")
            session = _get_active_session()
        except Exception:
            session = Session.builder.configs({
                "account": st.secrets["snowflake"]["account"],
                "user": st.secrets["snowflake"]["user"],
                "password": st.secrets["snowflake"]["password"],
                "warehouse": st.secrets["snowflake"]["warehouse"],
                "database": st.secrets["snowflake"]["database"],
                "schema": st.secrets["snowflake"].get("schema", "CONTEXT_ENGINE"),
                "role": st.secrets["snowflake"].get("role", "SYSADMIN"),
            }).create()
        st.session_state.snowpark_session = session
    return st.session_state.snowpark_session


def run_query(sql: str, params: list | None = None) -> list[dict]:
    """Execute a SQL query and return results as a list of dicts."""
    session = get_session()
    if params:
        result = session.sql(sql, params=params).collect()
    else:
        result = session.sql(sql).collect()
    return [row.as_dict() for row in result]


def run_ddl(sql: str) -> None:
    """Execute a DDL/DML statement (no results expected)."""
    session = get_session()
    session.sql(sql).collect()


def call_cortex_complete(prompt: str, model: str = "mistral-large2") -> str:
    """Call SNOWFLAKE.CORTEX.COMPLETE() with the given prompt."""
    session = get_session()
    escaped_prompt = prompt.replace("'", "''")
    result = session.sql(
        f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{model}', '{escaped_prompt}') AS response"
    ).collect()
    return result[0]["RESPONSE"] if result else ""


def call_cortex_summarize(text: str) -> str:
    """Call SNOWFLAKE.CORTEX.SUMMARIZE() on the given text."""
    session = get_session()
    escaped_text = text.replace("'", "''")
    result = session.sql(
        f"SELECT SNOWFLAKE.CORTEX.SUMMARIZE('{escaped_text}') AS summary"
    ).collect()
    return result[0]["SUMMARY"] if result else ""


def introspect_schema(database: str, schema: str | None = None) -> list[dict]:
    """Query INFORMATION_SCHEMA to discover tables and columns."""
    session = get_session()
    if schema:
        sql = f"""
            SELECT TABLE_CATALOG AS DATABASE_NAME,
                   TABLE_SCHEMA AS SCHEMA_NAME,
                   TABLE_NAME,
                   COLUMN_NAME,
                   DATA_TYPE,
                   IS_NULLABLE,
                   ORDINAL_POSITION,
                   COMMENT
            FROM {database}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = '{schema}'
              AND TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')
            ORDER BY TABLE_NAME, ORDINAL_POSITION
        """
    else:
        sql = f"""
            SELECT TABLE_CATALOG AS DATABASE_NAME,
                   TABLE_SCHEMA AS SCHEMA_NAME,
                   TABLE_NAME,
                   COLUMN_NAME,
                   DATA_TYPE,
                   IS_NULLABLE,
                   ORDINAL_POSITION,
                   COMMENT
            FROM {database}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA')
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """
    result = session.sql(sql).collect()
    return [row.as_dict() for row in result]


def get_databases() -> list[str]:
    """List available databases."""
    session = get_session()
    result = session.sql("SHOW DATABASES").collect()
    return [row["name"] for row in result]


def get_schemas(database: str) -> list[str]:
    """List schemas in a database."""
    session = get_session()
    result = session.sql(f"SHOW SCHEMAS IN DATABASE {database}").collect()
    return [row["name"] for row in result if row["name"] != "INFORMATION_SCHEMA"]


# =============================================================================
# SQL HELPERS
# =============================================================================

def _esc(value: str) -> str:
    """Escape single quotes for SQL strings."""
    if value is None:
        return ""
    return str(value).replace("'", "''")


def _sql_str(value: str | None) -> str:
    """Format a value as a SQL string or NULL."""
    if value is None:
        return "NULL"
    return f"'{_esc(value)}'"


# =============================================================================
# CONTEXT STORE — Sessions
# =============================================================================

def _ensure_created_by_column() -> None:
    """Add CREATED_BY column to INTERVIEW_SESSIONS if it doesn't exist."""
    try:
        run_ddl(
            "ALTER TABLE CONTEXT_ENGINE.INTERVIEW_SESSIONS "
            "ADD COLUMN IF NOT EXISTS CREATED_BY VARCHAR(255) DEFAULT CURRENT_USER()"
        )
    except Exception:
        pass  # Column may already exist or ALTER not supported


def create_session(session_name: str, target_database: str = None,
                   target_schema: str = None) -> str:
    """Create a new interview session. Returns the session ID."""
    _ensure_created_by_column()
    session_id = str(uuid.uuid4())
    session = get_session()
    session.sql(
        f"""INSERT INTO CONTEXT_ENGINE.INTERVIEW_SESSIONS
            (SESSION_ID, SESSION_NAME, TARGET_DATABASE, TARGET_SCHEMA, CREATED_BY)
            VALUES ('{session_id}', '{_esc(session_name)}',
                    {_sql_str(target_database)}, {_sql_str(target_schema)},
                    CURRENT_USER())"""
    ).collect()
    return session_id


def get_session_by_id(session_id: str) -> dict | None:
    """Retrieve a session by ID."""
    rows = run_query(
        f"SELECT * FROM CONTEXT_ENGINE.INTERVIEW_SESSIONS WHERE SESSION_ID = '{_esc(session_id)}'"
    )
    return rows[0] if rows else None


def list_sessions() -> list[dict]:
    """List interview sessions for the current user, most recent first."""
    _ensure_created_by_column()
    try:
        rows = run_query(
            "SELECT * FROM CONTEXT_ENGINE.INTERVIEW_SESSIONS "
            "WHERE CREATED_BY = CURRENT_USER() "
            "ORDER BY CREATED_AT DESC"
        )
        if rows is not None:
            return rows
    except Exception:
        pass
    # Fallback if CREATED_BY column doesn't exist or query fails
    return run_query(
        "SELECT * FROM CONTEXT_ENGINE.INTERVIEW_SESSIONS ORDER BY CREATED_AT DESC"
    )


def update_session_status(session_id: str, status: str) -> None:
    """Update session status (IN_PROGRESS, COMPLETED, ARCHIVED)."""
    run_ddl(
        f"""UPDATE CONTEXT_ENGINE.INTERVIEW_SESSIONS
            SET STATUS = '{_esc(status)}', UPDATED_AT = CURRENT_TIMESTAMP()
            WHERE SESSION_ID = '{_esc(session_id)}'"""
    )


# =============================================================================
# CONTEXT STORE — Interview Responses
# =============================================================================

def save_response(session_id: str, topic_area: str, question: str,
                  raw_answer: str, summarized_answer: str = None,
                  sequence_number: int = None) -> str:
    """Save an interview Q&A pair. Returns the response ID."""
    response_id = str(uuid.uuid4())
    session = get_session()
    session.sql(
        f"""INSERT INTO CONTEXT_ENGINE.INTERVIEW_RESPONSES
            (RESPONSE_ID, SESSION_ID, TOPIC_AREA, QUESTION_TEXT,
             RAW_ANSWER, SUMMARIZED_ANSWER, SEQUENCE_NUMBER)
            VALUES ('{response_id}', '{_esc(session_id)}', '{_esc(topic_area)}',
                    '{_esc(question)}', '{_esc(raw_answer)}',
                    {_sql_str(summarized_answer)}, {sequence_number or 'NULL'})"""
    ).collect()
    return response_id


def get_responses(session_id: str) -> list[dict]:
    """Get all responses for a session, ordered by sequence."""
    return run_query(
        f"""SELECT * FROM CONTEXT_ENGINE.INTERVIEW_RESPONSES
            WHERE SESSION_ID = '{_esc(session_id)}'
            ORDER BY SEQUENCE_NUMBER, CREATED_AT"""
    )


# =============================================================================
# CONTEXT STORE — Context Facts
# =============================================================================

def save_fact(session_id: str, source_type: str, source_id: str,
              category: str, fact_text: str,
              entity_database: str = None, entity_schema: str = None,
              entity_table: str = None, entity_column: str = None,
              confidence: float = 1.0) -> str:
    """Save an individual context fact. Returns the fact ID."""
    fact_id = str(uuid.uuid4())
    session = get_session()
    session.sql(
        f"""INSERT INTO CONTEXT_ENGINE.CONTEXT_FACTS
            (FACT_ID, SESSION_ID, SOURCE_TYPE, SOURCE_ID, CATEGORY, FACT_TEXT,
             ENTITY_DATABASE, ENTITY_SCHEMA, ENTITY_TABLE, ENTITY_COLUMN, CONFIDENCE)
            VALUES ('{fact_id}', '{_esc(session_id)}', '{_esc(source_type)}',
                    '{_esc(source_id)}', '{_esc(category)}', '{_esc(fact_text)}',
                    {_sql_str(entity_database)}, {_sql_str(entity_schema)},
                    {_sql_str(entity_table)}, {_sql_str(entity_column)}, {confidence})"""
    ).collect()
    return fact_id


def get_facts(session_id: str, category: str = None) -> list[dict]:
    """Get context facts for a session, optionally filtered by category."""
    where = f"WHERE SESSION_ID = '{_esc(session_id)}'"
    if category:
        where += f" AND CATEGORY = '{_esc(category)}'"
    return run_query(
        f"SELECT * FROM CONTEXT_ENGINE.CONTEXT_FACTS {where} ORDER BY CREATED_AT"
    )


def get_all_facts(session_id: str) -> list[dict]:
    """Get all facts for a session grouped by category."""
    return run_query(
        f"""SELECT * FROM CONTEXT_ENGINE.CONTEXT_FACTS
            WHERE SESSION_ID = '{_esc(session_id)}'
            ORDER BY CATEGORY, CREATED_AT"""
    )


def update_fact(fact_id: str, fact_text: str = None, category: str = None,
                is_verified: bool = None) -> None:
    """Update a context fact."""
    updates = []
    if fact_text is not None:
        updates.append(f"FACT_TEXT = '{_esc(fact_text)}'")
    if category is not None:
        updates.append(f"CATEGORY = '{_esc(category)}'")
    if is_verified is not None:
        updates.append(f"IS_VERIFIED = {is_verified}")
    if updates:
        updates.append("UPDATED_AT = CURRENT_TIMESTAMP()")
        run_ddl(
            f"""UPDATE CONTEXT_ENGINE.CONTEXT_FACTS
                SET {', '.join(updates)}
                WHERE FACT_ID = '{_esc(fact_id)}'"""
        )


def delete_fact(fact_id: str) -> None:
    """Delete a context fact."""
    run_ddl(f"DELETE FROM CONTEXT_ENGINE.CONTEXT_FACTS WHERE FACT_ID = '{_esc(fact_id)}'")


# =============================================================================
# CONTEXT STORE — Documents
# =============================================================================

def save_document(session_id: str, filename: str, file_type: str,
                  file_size_bytes: int, stage_path: str) -> str:
    """Save document metadata. Returns the document ID."""
    doc_id = str(uuid.uuid4())
    session = get_session()
    session.sql(
        f"""INSERT INTO CONTEXT_ENGINE.UPLOADED_DOCUMENTS
            (DOCUMENT_ID, SESSION_ID, FILENAME, FILE_TYPE, FILE_SIZE_BYTES, STAGE_PATH)
            VALUES ('{doc_id}', '{_esc(session_id)}', '{_esc(filename)}',
                    '{_esc(file_type)}', {file_size_bytes}, '{_esc(stage_path)}')"""
    ).collect()
    return doc_id


def update_document(doc_id: str, extracted_text: str = None,
                    summary: str = None, status: str = None,
                    error_message: str = None) -> None:
    """Update document processing results."""
    updates = []
    if extracted_text is not None:
        updates.append(f"EXTRACTED_TEXT = '{_esc(extracted_text)}'")
    if summary is not None:
        updates.append(f"SUMMARY = '{_esc(summary)}'")
    if status is not None:
        updates.append(f"PROCESSING_STATUS = '{_esc(status)}'")
    if error_message is not None:
        updates.append(f"ERROR_MESSAGE = '{_esc(error_message)}'")
    if status == "COMPLETED":
        updates.append("PROCESSED_AT = CURRENT_TIMESTAMP()")
    if updates:
        run_ddl(
            f"""UPDATE CONTEXT_ENGINE.UPLOADED_DOCUMENTS
                SET {', '.join(updates)}
                WHERE DOCUMENT_ID = '{_esc(doc_id)}'"""
        )


def get_documents(session_id: str) -> list[dict]:
    """Get all documents for a session."""
    return run_query(
        f"""SELECT * FROM CONTEXT_ENGINE.UPLOADED_DOCUMENTS
            WHERE SESSION_ID = '{_esc(session_id)}'
            ORDER BY UPLOADED_AT DESC"""
    )


# =============================================================================
# CONTEXT STORE — Artifacts
# =============================================================================

def save_artifact(session_id: str, artifact_type: str,
                  artifact_name: str, content: str) -> str:
    """Save a generated artifact. Returns the artifact ID."""
    artifact_id = str(uuid.uuid4())
    rows = run_query(
        f"""SELECT COALESCE(MAX(VERSION), 0) + 1 AS NEXT_VERSION
            FROM CONTEXT_ENGINE.CONTEXT_ARTIFACTS
            WHERE SESSION_ID = '{_esc(session_id)}'
              AND ARTIFACT_TYPE = '{_esc(artifact_type)}'"""
    )
    version = rows[0]["NEXT_VERSION"] if rows else 1
    session = get_session()
    session.sql(
        f"""INSERT INTO CONTEXT_ENGINE.CONTEXT_ARTIFACTS
            (ARTIFACT_ID, SESSION_ID, ARTIFACT_TYPE, ARTIFACT_NAME, CONTENT, VERSION)
            VALUES ('{artifact_id}', '{_esc(session_id)}', '{_esc(artifact_type)}',
                    '{_esc(artifact_name)}', '{_esc(content)}', {version})"""
    ).collect()
    return artifact_id


def get_latest_artifact(session_id: str, artifact_type: str) -> dict | None:
    """Get the latest version of an artifact."""
    rows = run_query(
        f"""SELECT * FROM CONTEXT_ENGINE.CONTEXT_ARTIFACTS
            WHERE SESSION_ID = '{_esc(session_id)}'
              AND ARTIFACT_TYPE = '{_esc(artifact_type)}'
            ORDER BY VERSION DESC LIMIT 1"""
    )
    return rows[0] if rows else None


def get_artifact_history(session_id: str, artifact_type: str) -> list[dict]:
    """Get all versions of an artifact."""
    return run_query(
        f"""SELECT * FROM CONTEXT_ENGINE.CONTEXT_ARTIFACTS
            WHERE SESSION_ID = '{_esc(session_id)}'
              AND ARTIFACT_TYPE = '{_esc(artifact_type)}'
            ORDER BY VERSION DESC"""
    )


# =============================================================================
# CONTEXT STORE — Schema Cache
# =============================================================================

def cache_schema(session_id: str, columns: list[dict]) -> None:
    """Cache introspected schema metadata."""
    session = get_session()
    run_ddl(
        f"DELETE FROM CONTEXT_ENGINE.SCHEMA_CACHE WHERE SESSION_ID = '{_esc(session_id)}'"
    )
    for col in columns:
        cache_id = str(uuid.uuid4())
        session.sql(
            f"""INSERT INTO CONTEXT_ENGINE.SCHEMA_CACHE
                (CACHE_ID, SESSION_ID, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME,
                 COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION, COMMENT)
                VALUES ('{cache_id}', '{_esc(session_id)}',
                        '{_esc(col.get("DATABASE_NAME", ""))}',
                        '{_esc(col.get("SCHEMA_NAME", ""))}',
                        '{_esc(col.get("TABLE_NAME", ""))}',
                        {_sql_str(col.get("COLUMN_NAME"))},
                        {_sql_str(col.get("DATA_TYPE"))},
                        {col.get("IS_NULLABLE", "NULL")},
                        {col.get("ORDINAL_POSITION", "NULL")},
                        {_sql_str(col.get("COMMENT"))})"""
        ).collect()


def get_cached_schema(session_id: str) -> list[dict]:
    """Get cached schema for a session."""
    return run_query(
        f"""SELECT * FROM CONTEXT_ENGINE.SCHEMA_CACHE
            WHERE SESSION_ID = '{_esc(session_id)}'
            ORDER BY DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, ORDINAL_POSITION"""
    )


# =============================================================================
# DOCUMENT PROCESSOR
# =============================================================================

def process_file(uploaded_file, stage_path: str) -> dict:
    """Process an uploaded file and extract text content."""
    file_type = _get_file_type(uploaded_file.name)

    try:
        if file_type == "pdf":
            try:
                text = _extract_pdf(stage_path)
            except Exception:
                # Fallback: try reading PDF bytes with basic extraction
                try:
                    import PyPDF2
                    reader = PyPDF2.PdfReader(io.BytesIO(uploaded_file.getvalue()))
                    pages = [page.extract_text() or "" for page in reader.pages]
                    text = "\n\n".join(pages)
                except Exception as e2:
                    raise RuntimeError(
                        f"PDF extraction failed. Cortex PARSE_DOCUMENT error and "
                        f"PyPDF2 fallback also failed: {e2}"
                    )
        elif file_type == "pptx":
            text = _extract_pptx(uploaded_file)
        elif file_type == "docx":
            text = _extract_docx(uploaded_file)
        elif file_type in ("txt", "md", "csv"):
            text = _extract_text(uploaded_file)
        else:
            return {
                "extracted_text": None,
                "summary": None,
                "status": "FAILED",
                "error": f"Unsupported file type: {file_type}",
            }

        summary = ""
        if text and len(text.strip()) > 50:
            try:
                summary = call_cortex_summarize(text[:50000])
            except Exception as e:
                summary = f"(Summary generation failed: {e})"

        return {
            "extracted_text": text,
            "summary": summary,
            "status": "COMPLETED",
            "error": None,
        }

    except Exception as e:
        return {
            "extracted_text": None,
            "summary": None,
            "status": "FAILED",
            "error": str(e),
        }


def stage_file(uploaded_file, session_id: str) -> str:
    """Upload a file to the Snowflake internal stage. Returns the stage path."""
    session = get_session()
    safe_name = uploaded_file.name.replace("'", "").replace(" ", "_")
    stage_path = f"@CONTEXT_ENGINE.UPLOADS/{session_id}/{safe_name}"
    stage_dir = f"@CONTEXT_ENGINE.UPLOADS/{session_id}/"
    file_bytes = uploaded_file.getvalue()

    # Try multiple approaches for SiS compatibility
    # Approach 1: put_stream (works in newer Snowpark versions)
    try:
        stream = io.BytesIO(file_bytes)
        session.file.put_stream(
            stream,
            stage_dir + safe_name,
            auto_compress=False,
            overwrite=True,
        )
        return stage_path
    except (AttributeError, Exception):
        pass

    # Approach 2: tempfile + file.put (works in local dev)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=f"_{safe_name}"
        ) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        session.file.put(
            tmp_path,
            stage_dir,
            auto_compress=False,
            overwrite=True,
        )
        return stage_path
    except Exception:
        pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # Approach 3: write via /tmp directly (SiS fallback)
    try:
        tmp_dir = "/tmp"
        os.makedirs(f"{tmp_dir}/{session_id}", exist_ok=True)
        local_path = f"{tmp_dir}/{session_id}/{safe_name}"
        with open(local_path, "wb") as f:
            f.write(file_bytes)
        session.file.put(
            local_path,
            stage_dir,
            auto_compress=False,
            overwrite=True,
        )
        return stage_path
    except Exception:
        pass
    finally:
        try:
            if local_path and os.path.exists(local_path):
                os.unlink(local_path)
        except Exception:
            pass

    raise RuntimeError(
        f"Could not stage file '{uploaded_file.name}'. "
        "Ensure the stage @CONTEXT_ENGINE.UPLOADS exists and is accessible."
    )


def _extract_pdf(stage_path: str) -> str:
    """Extract text from a PDF using Snowflake CORTEX.PARSE_DOCUMENT()."""
    session = get_session()
    result = session.sql(
        f"""SELECT SNOWFLAKE.CORTEX.PARSE_DOCUMENT(
                BUILD_SCOPED_FILE_URL('{stage_path}'),
                '{{"mode": "LAYOUT"}}'
            ) AS parsed"""
    ).collect()

    if result:
        parsed = result[0]["PARSED"]
        if isinstance(parsed, dict):
            pages = parsed.get("content", [])
            if isinstance(pages, list):
                return "\n\n".join(
                    page.get("text", "") for page in pages if isinstance(page, dict)
                )
            return str(parsed)
        return str(parsed)
    return ""


def _extract_pptx(uploaded_file) -> str:
    """Extract text from a PowerPoint file."""
    if _Presentation is None:
        return "(python-pptx is not installed — PowerPoint extraction unavailable)"

    prs = _Presentation(io.BytesIO(uploaded_file.getvalue()))
    texts = []

    for slide_num, slide in enumerate(prs.slides, 1):
        slide_texts = [f"--- Slide {slide_num} ---"]
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = paragraph.text.strip()
                    if text:
                        slide_texts.append(text)
            if shape.has_table:
                table = shape.table
                for row in table.rows:
                    row_text = " | ".join(
                        cell.text.strip() for cell in row.cells
                    )
                    if row_text.strip(" |"):
                        slide_texts.append(row_text)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                slide_texts.append(f"[Speaker Notes: {notes}]")

        texts.append("\n".join(slide_texts))

    return "\n\n".join(texts)


def _extract_docx(uploaded_file) -> str:
    """Extract text from a Word document."""
    if _Document is None:
        return "(python-docx is not installed — Word document extraction unavailable)"

    doc = _Document(io.BytesIO(uploaded_file.getvalue()))
    texts = []

    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text:
            texts.append(text)

    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells)
            if row_text.strip(" |"):
                texts.append(row_text)

    return "\n".join(texts)


def _extract_text(uploaded_file) -> str:
    """Extract text from a plain text file."""
    return uploaded_file.getvalue().decode("utf-8", errors="replace")


def _get_file_type(filename: str) -> str:
    """Get normalized file type from filename."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext


# =============================================================================
# VOICE TRANSCRIPTION (OpenAI Whisper)
# =============================================================================

def _get_openai_api_key() -> str:
    """Retrieve the OpenAI API key from a Snowflake secret, st.secrets, or env."""
    # 1. Try Snowflake secret via SYSTEM$GET_SECRET
    try:
        session = get_session()
        result = session.sql(
            "SELECT SYSTEM$GET_SECRET('VSSANALYTICS_DB.DATA_GOVERNANCE.OPENAI_API_KEY', 'secret_string') AS KEY"
        ).collect()
        if result and result[0]["KEY"]:
            return result[0]["KEY"]
    except Exception:
        pass

    # 2. Try st.secrets
    try:
        return st.secrets["openai"]["api_key"]
    except Exception:
        pass

    # 3. Try environment variable
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key

    raise RuntimeError(
        "OpenAI API key not found. Checked: "
        "VSSANALYTICS_DB.DATA_GOVERNANCE.OPENAI_API_KEY secret, "
        "st.secrets['openai']['api_key'], and OPENAI_API_KEY env var."
    )


def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe audio bytes using OpenAI Whisper API."""
    if _OpenAI is None:
        raise RuntimeError(
            "The openai package is not installed. "
            "Add 'openai' to requirements.txt and reinstall."
        )

    api_key = _get_openai_api_key()

    client = _OpenAI(api_key=api_key)
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "recording.wav"

    transcript = client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
        response_format="text",
    )
    return transcript.strip()


def summarize_transcript(transcript: str) -> str:
    """Use Cortex COMPLETE to clean up and summarize a voice transcript."""
    if not transcript or len(transcript.strip()) < 10:
        return transcript

    prompt = f"""The following is a voice transcript from an interview about a Snowflake data environment.
Clean it up by:
- Fixing any obvious transcription errors
- Removing filler words (um, uh, like, you know)
- Preserving all technical details, table names, column names, and business logic exactly as stated
- Keeping the same meaning and intent

Do NOT add information that wasn't in the original. Return only the cleaned-up text.

TRANSCRIPT:
{transcript}

CLEANED TEXT:"""

    try:
        cleaned = call_cortex_complete(prompt)
        return cleaned.strip()
    except Exception:
        return transcript


# =============================================================================
# CONTEXT EXTRACTOR
# =============================================================================

EXTRACTION_PROMPT = """You are a data analyst assistant. Analyze the following document text and extract structured context facts that would help an AI agent understand a data environment better.

For each fact you find, classify it into one of these categories:
- **terminology**: Business terms, acronyms, definitions (e.g., "ARR means Annual Recurring Revenue")
- **metric_definition**: How metrics/KPIs are calculated (e.g., "Revenue = SUM(amount) for completed orders")
- **business_rule**: Rules that affect data interpretation (e.g., "Fiscal year starts in February")
- **table_description**: What a table or dataset contains (e.g., "The ORDERS table stores all customer purchases")
- **relationship**: How tables/entities relate (e.g., "ORDERS joins to CUSTOMERS on customer_id")
- **caveat**: Gotchas, quirks, or data quality notes (e.g., "Amount is stored in cents, not dollars")
- **access_pattern**: Security or access rules (e.g., "SSN should never be exposed in reports")

Return a JSON array of objects with these fields:
- "category": one of the categories above
- "fact": the extracted fact as a clear, concise statement
- "entity_table": the table name referenced (if any, otherwise null)
- "entity_column": the column name referenced (if any, otherwise null)
- "confidence": 0.0-1.0 how confident you are this is correct

Only extract facts that are clearly stated or strongly implied. Do not speculate.

DOCUMENT TEXT:
{text}

Return ONLY valid JSON array, no other text."""


def extract_facts_from_text(text: str, session_id: str, source_type: str,
                            source_id: str, entity_database: str = None,
                            entity_schema: str = None) -> list[dict]:
    """Extract structured facts from document text."""
    if not text or len(text.strip()) < 20:
        return []

    truncated = text[:30000]
    prompt = EXTRACTION_PROMPT.format(text=truncated)

    try:
        response = call_cortex_complete(prompt)
        facts = _parse_facts_response(response)
    except Exception:
        return []

    saved_facts = []
    for fact_data in facts:
        try:
            fact_id = save_fact(
                session_id=session_id,
                source_type=source_type,
                source_id=source_id,
                category=fact_data.get("category", "terminology"),
                fact_text=fact_data.get("fact", ""),
                entity_database=entity_database,
                entity_schema=entity_schema,
                entity_table=fact_data.get("entity_table"),
                entity_column=fact_data.get("entity_column"),
                confidence=fact_data.get("confidence", 0.8),
            )
            saved_facts.append({**fact_data, "fact_id": fact_id})
        except Exception:
            continue

    return saved_facts


def _parse_facts_response(response: str) -> list[dict]:
    """Parse the LLM response into a list of fact dicts."""
    response = response.strip()

    if "```json" in response:
        response = response.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in response:
        response = response.split("```", 1)[1].split("```", 1)[0].strip()

    start = response.find("[")
    end = response.rfind("]")
    if start != -1 and end != -1:
        response = response[start : end + 1]

    try:
        facts = json.loads(response)
        if isinstance(facts, list):
            valid_categories = {
                "terminology", "metric_definition", "business_rule",
                "table_description", "relationship", "caveat", "access_pattern",
            }
            validated = []
            for fact in facts:
                if isinstance(fact, dict) and "fact" in fact:
                    if fact.get("category") not in valid_categories:
                        fact["category"] = "terminology"
                    validated.append(fact)
            return validated
    except json.JSONDecodeError:
        pass

    return []


# =============================================================================
# INTERVIEW ENGINE
# =============================================================================

TOPIC_AREAS = [
    {
        "id": "environment_overview",
        "name": "Environment Overview",
        "icon": "🏔️",
        "starter_question": (
            "Tell me about your Snowflake environment at a high level. "
            "What databases do you have, and what are they used for? "
            "Feel free to describe the big picture — what kind of data lives here "
            "and who uses it."
        ),
    },
    {
        "id": "schema_deep_dive",
        "name": "Schema Deep-Dive",
        "icon": "📂",
        "starter_question": (
            "Let's dig into the schemas. Walk me through the schemas you work with most. "
            "What's the purpose of each one, and how do they relate to each other?"
        ),
    },
    {
        "id": "key_tables",
        "name": "Key Tables & Relationships",
        "icon": "🔗",
        "starter_question": (
            "What are the most important tables in your environment? "
            "How do they connect to each other — what are the join keys and relationships?"
        ),
    },
    {
        "id": "business_terminology",
        "name": "Business Terminology",
        "icon": "📖",
        "starter_question": (
            "What business terms does your team use that might not be obvious from "
            "column names alone? Think about acronyms, jargon, or terms that are "
            "specific to your organization. (e.g., 'ARR' means Annual Recurring Revenue, "
            "'MQL' means Marketing Qualified Lead)"
        ),
    },
    {
        "id": "metrics_kpis",
        "name": "Metrics & KPIs",
        "icon": "📊",
        "starter_question": (
            "What are the key metrics and KPIs your team tracks? "
            "For each one, how is it calculated — what tables and columns feed into it? "
            "Don't worry about being perfectly precise, just describe how you think about them."
        ),
    },
    {
        "id": "business_rules",
        "name": "Business Rules & Logic",
        "icon": "📋",
        "starter_question": (
            "Are there any business rules that affect how data should be queried or interpreted? "
            "For example: 'revenue is only counted for orders with status COMPLETED', "
            "'fiscal year starts in February', 'we exclude internal test accounts from all metrics'. "
            "Share anything that someone querying this data for the first time should know."
        ),
    },
    {
        "id": "common_queries",
        "name": "Common Queries & Use Cases",
        "icon": "🔍",
        "starter_question": (
            "What questions does your team ask of this data most frequently? "
            "Give me examples of the kinds of things people would type into a natural language "
            "query tool. The more specific, the better — even exact phrasings people would use."
        ),
    },
    {
        "id": "data_caveats",
        "name": "Data Caveats & Gotchas",
        "icon": "⚠️",
        "starter_question": (
            "Are there any quirks, known issues, or things that trip people up with this data? "
            "For example: 'the AMOUNT column is in cents not dollars', 'deleted records are "
            "soft-deleted with is_active=false', 'data before 2020 is incomplete'. "
            "What would you warn a new analyst about?"
        ),
    },
    {
        "id": "access_patterns",
        "name": "Access Patterns & Security",
        "icon": "🔒",
        "starter_question": (
            "Are there any columns or tables that should be restricted or handled carefully? "
            "Any data that should never appear in query results (PII, salary data, etc.)? "
            "Any role-based access considerations?"
        ),
    },
    {
        "id": "review_gaps",
        "name": "Review & Gaps",
        "icon": "✅",
        "starter_question": None,
    },
]


def get_topic_by_id(topic_id: str) -> dict | None:
    """Get a topic area by its ID."""
    for topic in TOPIC_AREAS:
        if topic["id"] == topic_id:
            return topic
    return None


def get_current_topic_index(session_id: str) -> int:
    """Determine which topic the user is currently on based on responses."""
    responses = get_responses(session_id)
    covered_topics = set()
    for r in responses:
        if r.get("TOPIC_AREA"):
            covered_topics.add(r["TOPIC_AREA"])

    for i, topic in enumerate(TOPIC_AREAS):
        if topic["id"] not in covered_topics:
            return i
    return len(TOPIC_AREAS) - 1


def get_next_question(session_id: str, current_topic_id: str,
                      user_answer: str = None) -> dict:
    """Generate the next question based on context."""
    responses = get_responses(session_id)

    if current_topic_id == "review_gaps":
        return _generate_review_question(session_id)

    if user_answer:
        followup = _generate_followup(session_id, current_topic_id, user_answer, responses)
        if followup:
            return {
                "question": followup,
                "topic_id": current_topic_id,
                "is_followup": True,
                "is_review": False,
            }

    current_idx = next(
        (i for i, t in enumerate(TOPIC_AREAS) if t["id"] == current_topic_id),
        -1,
    )
    next_idx = current_idx + 1
    if next_idx >= len(TOPIC_AREAS):
        return _generate_review_question(session_id)

    next_topic = TOPIC_AREAS[next_idx]
    question = _contextualize_starter(session_id, next_topic, responses)

    return {
        "question": question,
        "topic_id": next_topic["id"],
        "is_followup": False,
        "is_review": next_topic["id"] == "review_gaps",
    }


def get_starter_question(session_id: str, topic_id: str) -> str:
    """Get the starter question for a topic, contextualized with known schema."""
    topic = get_topic_by_id(topic_id)
    if not topic:
        return "Tell me more about your data environment."
    responses = get_responses(session_id)
    return _contextualize_starter(session_id, topic, responses)


def _contextualize_starter(session_id: str, topic: dict,
                           responses: list[dict]) -> str:
    """Customize a starter question based on previously collected context."""
    base_question = topic.get("starter_question")
    if not base_question:
        return _generate_review_question(session_id)["question"]

    try:
        schema_cache = get_cached_schema(session_id)
    except Exception:
        schema_cache = []

    if not schema_cache and not responses:
        return base_question

    prev_context = ""
    for r in responses[-5:]:
        prev_context += f"Q: {r['QUESTION_TEXT'][:200]}\nA: {r['RAW_ANSWER'][:300]}\n\n"

    schema_context = ""
    if schema_cache:
        tables = {}
        for col in schema_cache[:100]:
            key = f"{col['DATABASE_NAME']}.{col['SCHEMA_NAME']}.{col['TABLE_NAME']}"
            if key not in tables:
                tables[key] = []
            tables[key].append(col["COLUMN_NAME"])
        schema_context = "Known tables: " + ", ".join(tables.keys())

    prompt = f"""You are conducting an interview to gather context about a Snowflake data environment.

The current topic is: {topic['name']}
The default question for this topic is: {base_question}

{"Previous conversation:" if prev_context else ""}
{prev_context}

{"Schema information:" if schema_context else ""}
{schema_context}

Based on the conversation so far, rephrase or enhance the default question to be more specific and relevant. Reference specific databases, schemas, or tables that were mentioned earlier if applicable. Keep the question open-ended so the user can stream-of-consciousness their thoughts.

Return ONLY the question text, nothing else."""

    try:
        enhanced = call_cortex_complete(prompt)
        if enhanced and len(enhanced.strip()) > 20:
            return enhanced.strip().strip('"')
    except Exception:
        pass

    return base_question


def _generate_followup(session_id: str, topic_id: str, user_answer: str,
                       responses: list[dict]) -> str | None:
    """Generate a contextual follow-up question based on the user's answer."""
    topic_responses = [r for r in responses if r.get("TOPIC_AREA") == topic_id]
    if len(topic_responses) >= 4:
        return None

    prompt = f"""You are conducting an interview to gather context about a Snowflake data environment.

Current topic: {topic_id.replace('_', ' ').title()}

The user just said:
"{user_answer[:2000]}"

Previous exchanges in this topic:
{_format_topic_history(topic_responses)}

Based on the user's answer, decide if a follow-up question would help capture more useful context. A follow-up is warranted if:
1. The user mentioned specific tables, columns, or concepts that need clarification
2. There are obvious gaps in the explanation (e.g., mentioned a metric but didn't explain how it's calculated)
3. The user hinted at something interesting that they didn't fully explain

If a follow-up IS warranted, respond with JUST the follow-up question.
If no follow-up is needed (the user covered the topic well), respond with exactly: NO_FOLLOWUP

Keep questions open-ended. Let the user ramble — that's where the best context comes from."""

    try:
        response = call_cortex_complete(prompt)
        response = response.strip().strip('"')
        if response and "NO_FOLLOWUP" not in response and len(response) > 20:
            return response
    except Exception:
        pass

    return None


def _generate_review_question(session_id: str) -> dict:
    """Generate a review/summary question showing what was captured."""
    try:
        facts = get_all_facts(session_id)
    except Exception:
        facts = []

    if not facts:
        summary = "I haven't captured any specific context yet."
    else:
        by_category = {}
        for f in facts:
            cat = f.get("CATEGORY", "other")
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(f["FACT_TEXT"])

        summary_parts = []
        for cat, items in by_category.items():
            label = cat.replace("_", " ").title()
            summary_parts.append(f"**{label}** ({len(items)} items):")
            for item in items[:3]:
                summary_parts.append(f"  - {item[:150]}")
            if len(items) > 3:
                summary_parts.append(f"  - ... and {len(items) - 3} more")
        summary = "\n".join(summary_parts)

    question = f"""Here's a summary of everything I've captured so far:

{summary}

What did I miss? Is there anything you'd like to add, correct, or elaborate on? Feel free to share any additional context that would help an AI agent better understand your data."""

    return {
        "question": question,
        "topic_id": "review_gaps",
        "is_followup": False,
        "is_review": True,
    }


def _format_topic_history(responses: list[dict]) -> str:
    """Format topic responses as conversation history."""
    parts = []
    for r in responses[-3:]:
        parts.append(f"Q: {r['QUESTION_TEXT'][:300]}")
        parts.append(f"A: {r['RAW_ANSWER'][:500]}")
        parts.append("")
    return "\n".join(parts) if parts else "(No prior exchanges)"


def get_coverage_scores(session_id: str) -> dict:
    """Calculate coverage scores per topic area."""
    responses = get_responses(session_id)
    scores = {}
    for topic in TOPIC_AREAS:
        topic_responses = [
            r for r in responses if r.get("TOPIC_AREA") == topic["id"]
        ]
        if not topic_responses:
            scores[topic["id"]] = 0.0
        elif len(topic_responses) >= 3:
            scores[topic["id"]] = 1.0
        else:
            total_chars = sum(len(r.get("RAW_ANSWER", "")) for r in topic_responses)
            scores[topic["id"]] = min(1.0, total_chars / 500)
    return scores


# =============================================================================
# ANSWER PROCESSOR
# =============================================================================

def process_answer(session_id: str, topic_area: str, question: str,
                   raw_answer: str, sequence_number: int) -> dict:
    """Process a user's interview answer through the full pipeline."""
    summary = _summarize_answer(question, raw_answer, topic_area)

    response_id = save_response(
        session_id=session_id,
        topic_area=topic_area,
        question=question,
        raw_answer=raw_answer,
        summarized_answer=summary,
        sequence_number=sequence_number,
    )

    facts = _extract_and_save_facts(
        session_id=session_id,
        response_id=response_id,
        topic_area=topic_area,
        question=question,
        raw_answer=raw_answer,
    )

    return {
        "response_id": response_id,
        "summary": summary,
        "facts": facts,
    }


def _summarize_answer(question: str, answer: str, topic_area: str) -> str:
    """Use Cortex COMPLETE to create a concise summary of the answer."""
    prompt = f"""Summarize the following interview response into clear, concise bullet points.
Focus on capturing specific, actionable facts about the data environment.
Preserve exact names of databases, schemas, tables, columns, and metrics.
Do not add interpretation — only capture what was explicitly stated.

Topic: {topic_area.replace('_', ' ').title()}
Question: {question}
Answer: {answer}

Summary (bullet points):"""

    try:
        summary = call_cortex_complete(prompt)
        return summary.strip()
    except Exception:
        return "(Summary could not be generated)"


def _extract_and_save_facts(session_id: str, response_id: str,
                            topic_area: str, question: str,
                            raw_answer: str) -> list[dict]:
    """Extract structured facts from the answer and save them."""
    try:
        schema_cache = get_cached_schema(session_id)
        known_tables = set()
        for col in schema_cache:
            known_tables.add(col["TABLE_NAME"])
    except Exception:
        known_tables = set()

    category_hint = {
        "environment_overview": "table_description",
        "schema_deep_dive": "table_description",
        "key_tables": "relationship",
        "business_terminology": "terminology",
        "metrics_kpis": "metric_definition",
        "business_rules": "business_rule",
        "common_queries": "metric_definition",
        "data_caveats": "caveat",
        "access_patterns": "access_pattern",
    }.get(topic_area, "terminology")

    prompt = f"""Extract structured facts from this interview response about a Snowflake data environment.

Topic area: {topic_area.replace('_', ' ').title()}
Primary category for this topic: {category_hint}

Question asked: {question}
User's answer: {raw_answer}

{"Known tables in the environment: " + ', '.join(list(known_tables)[:50]) if known_tables else ""}

Extract each distinct fact as a separate item. For each fact, provide:
- "category": one of [terminology, metric_definition, business_rule, table_description, relationship, caveat, access_pattern]
- "fact": clear, concise statement of the fact
- "entity_table": table name if referenced (or null)
- "entity_column": column name if referenced (or null)
- "confidence": 0.0-1.0

Return a JSON array. Only include facts that are clearly stated.
Return ONLY valid JSON array, no other text."""

    try:
        response = call_cortex_complete(prompt)
        facts_data = _parse_json_array(response)
    except Exception:
        facts_data = []

    saved_facts = []
    for fact_data in facts_data:
        try:
            fact_id = save_fact(
                session_id=session_id,
                source_type="INTERVIEW",
                source_id=response_id,
                category=fact_data.get("category", category_hint),
                fact_text=fact_data.get("fact", ""),
                entity_table=fact_data.get("entity_table"),
                entity_column=fact_data.get("entity_column"),
                confidence=fact_data.get("confidence", 0.8),
            )
            saved_facts.append({**fact_data, "fact_id": fact_id})
        except Exception:
            continue

    return saved_facts


def _parse_json_array(response: str) -> list[dict]:
    """Parse a JSON array from an LLM response."""
    response = response.strip()
    if "```json" in response:
        response = response.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in response:
        response = response.split("```", 1)[1].split("```", 1)[0].strip()

    start = response.find("[")
    end = response.rfind("]")
    if start != -1 and end != -1:
        response = response[start : end + 1]

    try:
        data = json.loads(response)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and "fact" in d]
    except json.JSONDecodeError:
        pass
    return []


# =============================================================================
# YAML GENERATOR
# =============================================================================

def generate_semantic_yaml(session_id: str, model_name: str = None) -> str:
    """Generate a complete Semantic View YAML from captured context."""
    facts = get_all_facts(session_id)
    try:
        schema_cache = get_cached_schema(session_id)
    except Exception:
        schema_cache = []

    if not facts and not schema_cache:
        return "# No context captured yet. Run an interview or upload documents first."

    model = _build_model_structure(facts, schema_cache, model_name)
    model = _enrich_with_llm(model, facts)

    return yaml.dump(model, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _build_model_structure(facts: list[dict], schema_cache: list[dict],
                           model_name: str = None) -> dict:
    """Build the base semantic model structure from facts and schema."""
    if not model_name:
        db_facts = [f for f in facts if f.get("ENTITY_DATABASE")]
        if db_facts:
            model_name = f"{db_facts[0]['ENTITY_DATABASE'].lower()}_analytics"
        else:
            model_name = "data_analytics"

    tables_schema = {}
    for col in schema_cache:
        key = (col["DATABASE_NAME"], col["SCHEMA_NAME"], col["TABLE_NAME"])
        if key not in tables_schema:
            tables_schema[key] = []
        tables_schema[key].append(col)

    facts_by_table = {}
    general_facts = []
    for fact in facts:
        if fact.get("ENTITY_TABLE"):
            table_key = fact["ENTITY_TABLE"]
            if table_key not in facts_by_table:
                facts_by_table[table_key] = []
            facts_by_table[table_key].append(fact)
        else:
            general_facts.append(fact)

    tables = []

    for (db, schema, table_name), columns in tables_schema.items():
        table_def = _build_table_def(
            db, schema, table_name, columns,
            facts_by_table.get(table_name, [])
        )
        tables.append(table_def)
        facts_by_table.pop(table_name, None)

    for table_name, table_facts in facts_by_table.items():
        db = None
        schema = None
        for f in table_facts:
            if f.get("ENTITY_DATABASE"):
                db = f["ENTITY_DATABASE"]
            if f.get("ENTITY_SCHEMA"):
                schema = f["ENTITY_SCHEMA"]

        table_def = _build_table_def(
            db or "YOUR_DATABASE",
            schema or "YOUR_SCHEMA",
            table_name,
            [],
            table_facts,
        )
        tables.append(table_def)

    model = {
        "name": model_name,
        "description": _generate_model_description(facts),
        "tables": tables,
    }

    relationships = _extract_relationships(facts)
    if relationships:
        model["relationships"] = relationships

    return model


def _build_table_def(db: str, schema: str, table_name: str,
                     columns: list[dict], facts: list[dict]) -> dict:
    """Build a single table definition for the semantic model."""
    table_def = {
        "name": table_name.lower(),
        "base_table": {
            "database": db,
            "schema": schema,
            "table": table_name,
        },
    }

    desc_facts = [f for f in facts if f.get("CATEGORY") == "table_description"]
    if desc_facts:
        table_def["description"] = desc_facts[0]["FACT_TEXT"]
    else:
        table_def["description"] = f"Table {table_name}"

    dimensions = []
    time_dimensions = []
    measures = []

    metric_facts = {
        f.get("ENTITY_COLUMN", "").upper(): f
        for f in facts
        if f.get("CATEGORY") == "metric_definition" and f.get("ENTITY_COLUMN")
    }

    for col in columns:
        col_name = col["COLUMN_NAME"]
        data_type = col.get("DATA_TYPE", "VARCHAR")

        if data_type in ("DATE", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ",
                         "DATETIME"):
            time_dim = {
                "name": col_name.lower(),
                "description": col.get("COMMENT") or f"{col_name} date/time",
                "expr": col_name,
                "data_type": data_type,
            }
            time_dimensions.append(time_dim)
        elif data_type in ("NUMBER", "FLOAT", "DECIMAL", "NUMERIC", "INT", "INTEGER",
                           "BIGINT", "SMALLINT", "DOUBLE"):
            metric_fact = metric_facts.get(col_name.upper())
            if metric_fact:
                measure = {
                    "name": col_name.lower(),
                    "description": metric_fact["FACT_TEXT"],
                    "expr": f"SUM({col_name})",
                    "data_type": "NUMBER",
                }
                measures.append(measure)
            else:
                dim = {
                    "name": col_name.lower(),
                    "description": col.get("COMMENT") or col_name,
                    "expr": col_name,
                    "data_type": data_type,
                }
                dimensions.append(dim)
        else:
            dim = {
                "name": col_name.lower(),
                "description": col.get("COMMENT") or col_name,
                "expr": col_name,
                "data_type": data_type,
            }
            dimensions.append(dim)

    for fact in facts:
        if fact.get("CATEGORY") == "metric_definition":
            col = fact.get("ENTITY_COLUMN", "")
            if col and col.upper() not in {m["name"].upper() for m in measures}:
                measures.append({
                    "name": col.lower() if col else fact["FACT_TEXT"][:30].lower().replace(" ", "_"),
                    "description": fact["FACT_TEXT"],
                    "expr": f"SUM({col})" if col else "-- define expression",
                    "data_type": "NUMBER",
                })

    if dimensions:
        table_def["dimensions"] = dimensions
    if time_dimensions:
        table_def["time_dimensions"] = time_dimensions
    if measures:
        table_def["measures"] = measures

    return table_def


def _extract_relationships(facts: list[dict]) -> list[dict]:
    """Extract relationship definitions from relationship facts."""
    rel_facts = [f for f in facts if f.get("CATEGORY") == "relationship"]
    if not rel_facts:
        return []

    facts_text = "\n".join(f["FACT_TEXT"] for f in rel_facts)
    prompt = f"""Given these relationship descriptions between database tables, extract structured join definitions.

Relationships described:
{facts_text}

For each relationship, provide a JSON array with objects containing:
- "left_table": the left table name (lowercase)
- "right_table": the right table name (lowercase)
- "left_columns": list of column names on the left side
- "right_columns": list of column names on the right side

Return ONLY valid JSON array, no other text."""

    try:
        response = call_cortex_complete(prompt)
        response = response.strip()
        if "```" in response:
            response = response.split("```json", 1)[-1].split("```", 1)[0].strip()
        start = response.find("[")
        end = response.rfind("]")
        if start != -1 and end != -1:
            rels = json.loads(response[start:end + 1])
            return [
                {
                    "left_table": r["left_table"],
                    "right_table": r["right_table"],
                    "join_columns": [
                        {"left": l, "right": ri}
                        for l, ri in zip(r.get("left_columns", []),
                                         r.get("right_columns", []))
                    ],
                }
                for r in rels
                if isinstance(r, dict) and "left_table" in r and "right_table" in r
            ]
    except Exception:
        pass

    return []


def _generate_model_description(facts: list[dict]) -> str:
    """Generate a description for the semantic model."""
    desc_facts = [f for f in facts if f.get("CATEGORY") == "table_description"]
    if desc_facts:
        tables_mentioned = set()
        for f in desc_facts:
            if f.get("ENTITY_TABLE"):
                tables_mentioned.add(f["ENTITY_TABLE"])
        if tables_mentioned:
            return f"Semantic model covering {', '.join(sorted(tables_mentioned))}"
    return "Semantic model for data analytics"


def _enrich_with_llm(model: dict, facts: list[dict]) -> dict:
    """Use LLM to add synonyms and improve descriptions."""
    term_facts = [f for f in facts if f.get("CATEGORY") == "terminology"]
    if not term_facts:
        return model

    for table in model.get("tables", []):
        for dim_list in [table.get("dimensions", []), table.get("time_dimensions", []),
                         table.get("measures", [])]:
            for item in dim_list:
                name = item["name"]
                matching_terms = [
                    t for t in term_facts
                    if name.lower() in t["FACT_TEXT"].lower()
                ]
                if matching_terms:
                    synonyms = []
                    for t in matching_terms:
                        text = t["FACT_TEXT"]
                        if "means" in text.lower() or "also known as" in text.lower():
                            synonyms.append(text.split("means")[-1].strip()[:50])
                    if synonyms:
                        item["synonyms"] = synonyms[:5]

    return model


# =============================================================================
# VQR GENERATOR
# =============================================================================

def generate_vqr(session_id: str) -> str:
    """Generate a Verified Query Repository YAML from captured context."""
    facts = get_all_facts(session_id)
    try:
        schema_cache = get_cached_schema(session_id)
    except Exception:
        schema_cache = []

    if not facts:
        return "# No context captured yet."

    metric_facts = [f for f in facts if f.get("CATEGORY") == "metric_definition"]
    rule_facts = [f for f in facts if f.get("CATEGORY") == "business_rule"]
    caveat_facts = [f for f in facts if f.get("CATEGORY") == "caveat"]

    schema_context = _build_schema_context(schema_cache)
    rules_context = "\n".join(f"- {f['FACT_TEXT']}" for f in rule_facts[:20])
    caveats_context = "\n".join(f"- {f['FACT_TEXT']}" for f in caveat_facts[:10])

    queries = []
    for metric in metric_facts[:15]:
        query_set = _generate_queries_for_metric(
            metric, schema_context, rules_context, caveats_context
        )
        queries.extend(query_set)

    common_queries = _generate_common_queries(facts, schema_context, rules_context)
    queries.extend(common_queries)

    if not queries:
        return "# No queries could be generated. Add more metric definitions and common query patterns."

    vqr = {
        "verified_queries": [
            {
                "name": q["name"],
                "question": q["question"],
                "sql": q["sql"],
                "verified_at": str(date.today()),
                "verified_by": "context_engine_draft",
                "status": "DRAFT",
            }
            for q in queries
        ]
    }

    output = "# Verified Query Repository (VQR)\n"
    output += "# STATUS: DRAFT — Review and verify each query before deploying\n"
    output += "#\n"
    output += "# These queries were auto-generated from captured context.\n"
    output += "# Verify the SQL is correct before marking as verified.\n\n"
    output += yaml.dump(vqr, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return output


def _build_schema_context(schema_cache: list[dict]) -> str:
    """Build a concise schema description for LLM context."""
    if not schema_cache:
        return "Schema information not available."

    tables = {}
    for col in schema_cache:
        key = f"{col['DATABASE_NAME']}.{col['SCHEMA_NAME']}.{col['TABLE_NAME']}"
        if key not in tables:
            tables[key] = []
        tables[key].append(f"{col['COLUMN_NAME']} ({col.get('DATA_TYPE', 'VARCHAR')})")

    parts = []
    for table_name, columns in list(tables.items())[:20]:
        cols_str = ", ".join(columns[:15])
        parts.append(f"  {table_name}: [{cols_str}]")

    return "Available tables and columns:\n" + "\n".join(parts)


def _generate_queries_for_metric(metric: dict, schema_context: str,
                                 rules_context: str,
                                 caveats_context: str) -> list[dict]:
    """Generate example queries for a single metric."""
    prompt = f"""Generate 2 example natural language questions and corresponding Snowflake SQL queries for this metric.

Metric: {metric['FACT_TEXT']}
{f"Related table: {metric.get('ENTITY_TABLE', '')}" if metric.get('ENTITY_TABLE') else ""}
{f"Related column: {metric.get('ENTITY_COLUMN', '')}" if metric.get('ENTITY_COLUMN') else ""}

{schema_context}

Business rules to follow:
{rules_context if rules_context else "None specified"}

Data caveats:
{caveats_context if caveats_context else "None specified"}

For each query, provide:
- "name": a short snake_case identifier
- "question": the natural language question a user might ask
- "sql": valid Snowflake SQL that answers the question

Return a JSON array with exactly 2 objects. Return ONLY valid JSON, no other text."""

    try:
        response = call_cortex_complete(prompt)
        return _parse_query_response(response)
    except Exception:
        return []


def _generate_common_queries(facts: list[dict], schema_context: str,
                             rules_context: str) -> list[dict]:
    """Generate queries from common question patterns mentioned in facts."""
    question_facts = []
    for f in facts:
        text = f["FACT_TEXT"].lower()
        if any(kw in text for kw in ["how many", "what is", "show me", "total",
                                      "average", "count", "list", "top"]):
            question_facts.append(f)

    if not question_facts:
        return []

    questions_text = "\n".join(
        f"- {f['FACT_TEXT']}" for f in question_facts[:10]
    )

    prompt = f"""These are common questions that users ask about their data. Generate SQL queries for each.

Common questions/patterns:
{questions_text}

{schema_context}

Business rules:
{rules_context if rules_context else "None"}

For each question that can be answered with SQL, provide:
- "name": short snake_case identifier
- "question": the natural language question
- "sql": valid Snowflake SQL

Return a JSON array. Return ONLY valid JSON, no other text."""

    try:
        response = call_cortex_complete(prompt)
        return _parse_query_response(response)
    except Exception:
        return []


def _parse_query_response(response: str) -> list[dict]:
    """Parse LLM response into query objects."""
    response = response.strip()
    if "```json" in response:
        response = response.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in response:
        response = response.split("```", 1)[1].split("```", 1)[0].strip()

    start = response.find("[")
    end = response.rfind("]")
    if start != -1 and end != -1:
        try:
            queries = json.loads(response[start:end + 1])
            return [
                q for q in queries
                if isinstance(q, dict) and "name" in q and "question" in q and "sql" in q
            ]
        except json.JSONDecodeError:
            pass
    return []


# =============================================================================
# INSTRUCTIONS GENERATOR
# =============================================================================

def generate_custom_instructions(session_id: str) -> str:
    """Generate Cortex Analyst custom instructions from captured context."""
    facts = get_all_facts(session_id)
    if not facts:
        return "# No context captured yet."

    rules = [f["FACT_TEXT"] for f in facts if f.get("CATEGORY") == "business_rule"]
    caveats = [f["FACT_TEXT"] for f in facts if f.get("CATEGORY") == "caveat"]
    access = [f["FACT_TEXT"] for f in facts if f.get("CATEGORY") == "access_pattern"]
    terms = [f["FACT_TEXT"] for f in facts if f.get("CATEGORY") == "terminology"]

    if not any([rules, caveats, access, terms]):
        return "# No business rules, caveats, or access patterns captured yet."

    raw_instructions = []

    if rules:
        raw_instructions.append("BUSINESS RULES:")
        raw_instructions.extend(f"- {r}" for r in rules)
    if caveats:
        raw_instructions.append("\nDATA CAVEATS:")
        raw_instructions.extend(f"- {c}" for c in caveats)
    if access:
        raw_instructions.append("\nACCESS RESTRICTIONS:")
        raw_instructions.extend(f"- {a}" for a in access)
    if terms:
        raw_instructions.append("\nBUSINESS TERMINOLOGY:")
        raw_instructions.extend(f"- {t}" for t in terms[:20])

    raw_text = "\n".join(raw_instructions)

    prompt = f"""You are helping create custom instructions for a Snowflake Cortex Analyst AI agent.
These instructions will guide how the agent generates SQL queries.

Here are the raw business rules, caveats, access restrictions, and terminology collected from interviews:

{raw_text}

Rewrite these into a clean, well-organized set of instructions for the AI agent. The instructions should:
1. Be clear and actionable — each instruction should tell the agent what to DO or NOT DO
2. Be organized by topic (data rules, terminology, restrictions)
3. Remove duplicates and consolidate related instructions
4. Use imperative language (e.g., "Always filter...", "Never expose...", "Use X when calculating Y")
5. Keep it concise — the total should be under 2000 words

Format as a numbered list grouped by section headers. Do NOT wrap in markdown code blocks."""

    try:
        instructions = call_cortex_complete(prompt)
        return instructions.strip()
    except Exception:
        return raw_text


def generate_markdown_doc(session_id: str) -> str:
    """Generate a comprehensive human-readable Markdown document of all context."""
    facts = get_all_facts(session_id)
    if not facts:
        return "# Context Document\n\nNo context captured yet."

    by_category = {}
    for f in facts:
        cat = f.get("CATEGORY", "other")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(f)

    doc_parts = ["# Data Context Document\n"]
    doc_parts.append("*Generated by Snowflake Context Engine*\n")
    doc_parts.append("---\n")

    if "table_description" in by_category:
        doc_parts.append("## Databases, Schemas & Tables\n")
        by_table = {}
        for f in by_category["table_description"]:
            key = f.get("ENTITY_TABLE", "General")
            if f.get("ENTITY_SCHEMA"):
                key = f"{f['ENTITY_SCHEMA']}.{key}"
            if f.get("ENTITY_DATABASE"):
                key = f"{f['ENTITY_DATABASE']}.{key}"
            if key not in by_table:
                by_table[key] = []
            by_table[key].append(f["FACT_TEXT"])

        for table_name, descs in sorted(by_table.items()):
            doc_parts.append(f"### `{table_name}`\n")
            for desc in descs:
                doc_parts.append(f"- {desc}\n")
            doc_parts.append("")

    if "relationship" in by_category:
        doc_parts.append("## Table Relationships\n")
        for f in by_category["relationship"]:
            doc_parts.append(f"- {f['FACT_TEXT']}\n")
        doc_parts.append("")

    if "terminology" in by_category:
        doc_parts.append("## Business Terminology\n")
        doc_parts.append("| Term | Definition |")
        doc_parts.append("|------|-----------|")
        for f in by_category["terminology"]:
            text = f["FACT_TEXT"]
            if " means " in text.lower():
                parts = text.split(" means ", 1)
                doc_parts.append(f"| {parts[0].strip()} | {parts[1].strip()} |")
            elif " - " in text:
                parts = text.split(" - ", 1)
                doc_parts.append(f"| {parts[0].strip()} | {parts[1].strip()} |")
            else:
                doc_parts.append(f"| — | {text} |")
        doc_parts.append("")

    if "metric_definition" in by_category:
        doc_parts.append("## Metrics & KPIs\n")
        doc_parts.append("| Metric | Definition | Source Table |")
        doc_parts.append("|--------|-----------|-------------|")
        for f in by_category["metric_definition"]:
            table = f.get("ENTITY_TABLE", "—")
            doc_parts.append(f"| {f.get('ENTITY_COLUMN', '—')} | {f['FACT_TEXT']} | `{table}` |")
        doc_parts.append("")

    if "business_rule" in by_category:
        doc_parts.append("## Business Rules\n")
        for i, f in enumerate(by_category["business_rule"], 1):
            doc_parts.append(f"{i}. {f['FACT_TEXT']}\n")
        doc_parts.append("")

    if "caveat" in by_category:
        doc_parts.append("## Data Caveats & Gotchas\n")
        for f in by_category["caveat"]:
            doc_parts.append(f"- ⚠️ {f['FACT_TEXT']}\n")
        doc_parts.append("")

    if "access_pattern" in by_category:
        doc_parts.append("## Access Patterns & Security\n")
        for f in by_category["access_pattern"]:
            doc_parts.append(f"- 🔒 {f['FACT_TEXT']}\n")
        doc_parts.append("")

    return "\n".join(doc_parts)


# =============================================================================
# RAG / CORTEX SEARCH EXPORT
# =============================================================================

def generate_rag_chunks(session_id: str) -> list[dict]:
    """Generate chunked context documents optimized for RAG / Cortex Search.

    Each chunk is a self-contained JSON object with:
    - chunk_id: unique identifier
    - content: the text content for embedding/search
    - category: fact category for filtering
    - entity_database, entity_schema, entity_table, entity_column: metadata
    - source_type: INTERVIEW or DOCUMENT
    - tags: list of searchable tags
    - session_id: originating session
    """
    facts = get_all_facts(session_id)
    if not facts:
        return []

    try:
        schema_cache = get_cached_schema(session_id)
    except Exception:
        schema_cache = []

    try:
        responses = get_responses(session_id)
    except Exception:
        responses = []

    try:
        documents = get_documents(session_id)
    except Exception:
        documents = []

    chunks = []

    # --- Chunk Type 1: Individual facts (fine-grained, best for precise retrieval) ---
    for fact in facts:
        tags = [fact.get("CATEGORY", "")]
        if fact.get("ENTITY_TABLE"):
            tags.append(fact["ENTITY_TABLE"].lower())
        if fact.get("ENTITY_COLUMN"):
            tags.append(fact["ENTITY_COLUMN"].lower())
        if fact.get("ENTITY_DATABASE"):
            tags.append(fact["ENTITY_DATABASE"].lower())

        chunks.append({
            "chunk_id": f"fact_{fact['FACT_ID']}",
            "chunk_type": "fact",
            "content": fact["FACT_TEXT"],
            "category": fact.get("CATEGORY", "general"),
            "entity_database": fact.get("ENTITY_DATABASE"),
            "entity_schema": fact.get("ENTITY_SCHEMA"),
            "entity_table": fact.get("ENTITY_TABLE"),
            "entity_column": fact.get("ENTITY_COLUMN"),
            "source_type": fact.get("SOURCE_TYPE", "UNKNOWN"),
            "confidence": fact.get("CONFIDENCE", 0.8),
            "is_verified": bool(fact.get("IS_VERIFIED", False)),
            "tags": tags,
            "session_id": session_id,
        })

    # --- Chunk Type 2: Grouped facts by table (coarser, good for table-level context) ---
    facts_by_table = {}
    for fact in facts:
        table = fact.get("ENTITY_TABLE")
        if table:
            if table not in facts_by_table:
                facts_by_table[table] = []
            facts_by_table[table].append(fact)

    for table_name, table_facts in facts_by_table.items():
        categories_present = list(set(f.get("CATEGORY", "") for f in table_facts))
        content_parts = [f"Context for table: {table_name}\n"]
        for cat in sorted(categories_present):
            cat_facts = [f for f in table_facts if f.get("CATEGORY") == cat]
            cat_label = cat.replace("_", " ").title()
            content_parts.append(f"\n{cat_label}:")
            for f in cat_facts:
                content_parts.append(f"- {f['FACT_TEXT']}")

        db = next((f.get("ENTITY_DATABASE") for f in table_facts if f.get("ENTITY_DATABASE")), None)
        schema = next((f.get("ENTITY_SCHEMA") for f in table_facts if f.get("ENTITY_SCHEMA")), None)

        chunks.append({
            "chunk_id": f"table_{table_name.lower()}_{session_id[:8]}",
            "chunk_type": "table_context",
            "content": "\n".join(content_parts),
            "category": "table_context",
            "entity_database": db,
            "entity_schema": schema,
            "entity_table": table_name,
            "entity_column": None,
            "source_type": "AGGREGATED",
            "confidence": 1.0,
            "is_verified": False,
            "tags": [table_name.lower()] + categories_present,
            "session_id": session_id,
        })

    # --- Chunk Type 3: Interview Q&A pairs (rich conversational context) ---
    for resp in responses:
        content = f"Q: {resp['QUESTION_TEXT']}\nA: {resp['RAW_ANSWER']}"
        if resp.get("SUMMARIZED_ANSWER"):
            content += f"\n\nSummary: {resp['SUMMARIZED_ANSWER']}"

        chunks.append({
            "chunk_id": f"interview_{resp['RESPONSE_ID']}",
            "chunk_type": "interview_qa",
            "content": content,
            "category": resp.get("TOPIC_AREA", "general"),
            "entity_database": None,
            "entity_schema": None,
            "entity_table": None,
            "entity_column": None,
            "source_type": "INTERVIEW",
            "confidence": 1.0,
            "is_verified": False,
            "tags": [resp.get("TOPIC_AREA", "general"), "interview"],
            "session_id": session_id,
        })

    # --- Chunk Type 4: Document summaries (broad context from uploads) ---
    for doc in documents:
        if doc.get("PROCESSING_STATUS") != "COMPLETED":
            continue

        if doc.get("SUMMARY"):
            chunks.append({
                "chunk_id": f"doc_summary_{doc['DOCUMENT_ID']}",
                "chunk_type": "document_summary",
                "content": f"Document: {doc['FILENAME']}\n\n{doc['SUMMARY']}",
                "category": "document",
                "entity_database": None,
                "entity_schema": None,
                "entity_table": None,
                "entity_column": None,
                "source_type": "DOCUMENT",
                "confidence": 1.0,
                "is_verified": False,
                "tags": ["document", doc.get("FILE_TYPE", ""), doc["FILENAME"].lower()],
                "session_id": session_id,
            })

        # For longer docs, chunk the extracted text into ~1000 char segments
        extracted = doc.get("EXTRACTED_TEXT", "")
        if extracted and len(extracted) > 500:
            text_chunks = _chunk_text(extracted, chunk_size=1000, overlap=150)
            for idx, text_chunk in enumerate(text_chunks):
                chunks.append({
                    "chunk_id": f"doc_chunk_{doc['DOCUMENT_ID']}_{idx}",
                    "chunk_type": "document_chunk",
                    "content": f"Source: {doc['FILENAME']} (chunk {idx + 1}/{len(text_chunks)})\n\n{text_chunk}",
                    "category": "document",
                    "entity_database": None,
                    "entity_schema": None,
                    "entity_table": None,
                    "entity_column": None,
                    "source_type": "DOCUMENT",
                    "confidence": 0.9,
                    "is_verified": False,
                    "tags": ["document", doc.get("FILE_TYPE", ""), doc["FILENAME"].lower()],
                    "session_id": session_id,
                })

    # --- Chunk Type 5: Schema descriptions (structural context) ---
    if schema_cache:
        tables_schema = {}
        for col in schema_cache:
            key = f"{col['DATABASE_NAME']}.{col['SCHEMA_NAME']}.{col['TABLE_NAME']}"
            if key not in tables_schema:
                tables_schema[key] = []
            tables_schema[key].append(col)

        for table_fqn, columns in tables_schema.items():
            col_descriptions = []
            for col in columns:
                desc = f"  - {col['COLUMN_NAME']} ({col.get('DATA_TYPE', 'VARCHAR')})"
                if col.get("COMMENT"):
                    desc += f": {col['COMMENT']}"
                col_descriptions.append(desc)

            parts = table_fqn.split(".")
            db = parts[0] if len(parts) > 0 else None
            schema = parts[1] if len(parts) > 1 else None
            table = parts[2] if len(parts) > 2 else parts[-1]

            content = f"Table: {table_fqn}\nColumns:\n" + "\n".join(col_descriptions)

            chunks.append({
                "chunk_id": f"schema_{table_fqn.lower().replace('.', '_')}",
                "chunk_type": "schema",
                "content": content,
                "category": "table_description",
                "entity_database": db,
                "entity_schema": schema,
                "entity_table": table,
                "entity_column": None,
                "source_type": "SCHEMA_INTROSPECTION",
                "confidence": 1.0,
                "is_verified": True,
                "tags": [table.lower(), "schema", "columns"],
                "session_id": session_id,
            })

    return chunks


def _chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Split text into overlapping chunks for RAG indexing."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size

        # Try to break at a sentence or paragraph boundary
        if end < len(text):
            for sep in ["\n\n", "\n", ". ", "! ", "? "]:
                break_point = text.rfind(sep, start + chunk_size // 2, end + 100)
                if break_point > start:
                    end = break_point + len(sep)
                    break

        chunks.append(text[start:end].strip())
        start = end - overlap

    return [c for c in chunks if c]


def generate_rag_jsonl(session_id: str) -> str:
    """Generate JSONL (one JSON object per line) for bulk loading into Cortex Search."""
    chunks = generate_rag_chunks(session_id)
    lines = [json.dumps(chunk, default=str) for chunk in chunks]
    return "\n".join(lines)


def generate_cortex_search_sql(session_id: str, table_name: str = "CONTEXT_ENGINE.RAG_CHUNKS") -> str:
    """Generate SQL to create and populate a Cortex Search-ready table."""
    sql_parts = [
        f"-- Cortex Search table for RAG context",
        f"-- Generated from session {session_id}",
        f"",
        f"CREATE TABLE IF NOT EXISTS {table_name} (",
        f"    CHUNK_ID VARCHAR(255) PRIMARY KEY,",
        f"    CHUNK_TYPE VARCHAR(50),",
        f"    CONTENT VARCHAR(16777216),",
        f"    CATEGORY VARCHAR(100),",
        f"    ENTITY_DATABASE VARCHAR(255),",
        f"    ENTITY_SCHEMA VARCHAR(255),",
        f"    ENTITY_TABLE VARCHAR(255),",
        f"    ENTITY_COLUMN VARCHAR(255),",
        f"    SOURCE_TYPE VARCHAR(50),",
        f"    CONFIDENCE FLOAT,",
        f"    IS_VERIFIED BOOLEAN,",
        f"    TAGS ARRAY,",
        f"    SESSION_ID VARCHAR(255),",
        f"    CREATED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()",
        f");",
        f"",
        f"-- Create Cortex Search service on this table:",
        f"-- CREATE OR REPLACE CORTEX SEARCH SERVICE {table_name}_SEARCH",
        f"--   ON {table_name}",
        f"--   WAREHOUSE = YOUR_WAREHOUSE",
        f"--   TARGET_LAG = '1 hour'",
        f"--   AS (",
        f"--     SELECT",
        f"--       CHUNK_ID,",
        f"--       CONTENT,",
        f"--       CATEGORY,",
        f"--       ENTITY_TABLE,",
        f"--       CHUNK_TYPE,",
        f"--       TAGS",
        f"--     FROM {table_name}",
        f"--   );",
    ]
    return "\n".join(sql_parts)


# =============================================================================
# STREAMLIT UI — Single-Page App with Sidebar Navigation
# =============================================================================

CATEGORY_LABELS = {
    "terminology": ("📖", "Business Terminology"),
    "metric_definition": ("📊", "Metrics & KPIs"),
    "business_rule": ("📋", "Business Rules"),
    "table_description": ("🗃️", "Table Descriptions"),
    "relationship": ("🔗", "Relationships"),
    "caveat": ("⚠️", "Caveats & Gotchas"),
    "access_pattern": ("🔒", "Access Patterns"),
}


def _render_fact_card(fact: dict, show_timestamp: bool = False):
    """Render an individual fact with edit/delete controls."""
    fact_id = fact["FACT_ID"]
    cat = fact.get("CATEGORY", "")
    cat_label = CATEGORY_LABELS.get(cat, ("", cat))[1]

    col1, col2, col3 = st.columns([0.7, 0.15, 0.15])

    with col1:
        if fact.get("IS_VERIFIED"):
            st.markdown(f"✅ {fact['FACT_TEXT']}")
        else:
            st.markdown(fact["FACT_TEXT"])

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


# ---------------------------------------------------------------------------
# Page Config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Snowflake Context Engine",
    page_icon="❄️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.sidebar.title("❄️ Context Engine")
st.sidebar.markdown("Build context for Snowflake Intelligence agents.")

# ---------------------------------------------------------------------------
# Page Navigation (render FIRST so it always appears)
# ---------------------------------------------------------------------------
st.sidebar.markdown("---")
_NAV_PAGES = ["🏠 Home", "📄 Upload Documents", "💬 Context Interview", "📝 Review & Edit", "🚀 Export & Deploy"]

# Allow programmatic navigation: set st.session_state._nav_request before rerun
if "_nav_request" in st.session_state:
    st.session_state._nav_page = st.session_state._nav_request
    del st.session_state._nav_request
elif "_nav_page" not in st.session_state:
    st.session_state._nav_page = _NAV_PAGES[0]

page = st.sidebar.radio(
    "Navigate",
    _NAV_PAGES,
    key="_nav_page",
)

# ---------------------------------------------------------------------------
# Session Selection / Creation (always in sidebar)
# ---------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.header("Session")

sessions = []
try:
    sessions = list_sessions()
except Exception as e:
    st.sidebar.warning(f"Could not load sessions: {e}")

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


# =============================================================================
# PAGE: HOME
# =============================================================================
if page == "🏠 Home":
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
        """
    )

    # -----------------------------------------------------------------
    # My Context Sessions
    # -----------------------------------------------------------------
    st.markdown("---")
    st.header("My Context Sessions")

    home_sessions = []
    try:
        home_sessions = list_sessions()
    except Exception as e:
        st.warning(f"Could not load sessions: {e}")

    if not home_sessions:
        st.info("No sessions yet. Create one from the sidebar to get started.")
    else:
        for s in home_sessions:
            sid = s["SESSION_ID"]
            name = s.get("SESSION_NAME", "Untitled")
            status = s.get("STATUS", "UNKNOWN")
            created = s.get("CREATED_AT", "")
            updated = s.get("UPDATED_AT", created)

            # Status badge
            if status == "COMPLETED":
                badge = "✅"
            elif status == "IN_PROGRESS":
                badge = "🔄"
            else:
                badge = "📋"

            # Fact count for this session
            fact_count = None
            try:
                facts = get_all_facts(sid)
                fact_count = len(facts) if facts else 0
            except Exception:
                fact_count = None

            # Coverage scores
            try:
                scores = get_coverage_scores(sid)
                covered_topics = sum(1 for v in scores.values() if v >= 0.8)
                total_topics = len(TOPIC_AREAS)
            except Exception:
                covered_topics = 0
                total_topics = len(TOPIC_AREAS)

            with st.container(border=True):
                col_info, col_stats, col_action = st.columns([3, 2, 1])

                with col_info:
                    st.markdown(f"### {badge} {name}")
                    st.caption(f"Created: {created}")
                    if updated and updated != created:
                        st.caption(f"Last updated: {updated}")

                with col_stats:
                    if fact_count is not None:
                        st.metric("Facts", fact_count)
                    st.caption(f"Topics: {covered_topics}/{total_topics} complete")
                    st.progress(covered_topics / total_topics if total_topics else 0)

                with col_action:
                    if st.button("Continue →", key=f"home_open_{sid}", type="primary",
                                 use_container_width=True):
                        st.session_state.interview_session_id = sid
                        st.session_state._nav_request = "💬 Context Interview"
                        st.rerun()

    if "interview_session_id" in st.session_state:
        st.sidebar.markdown("---")
        st.sidebar.markdown("### Current Session")
        st.sidebar.markdown(f"Session: `{st.session_state.interview_session_id[:8]}...`")


# =============================================================================
# PAGE: UPLOAD DOCUMENTS
# =============================================================================
elif page == "📄 Upload Documents":
    st.title("📄 Upload Documents")
    st.markdown("Upload files containing business context, data dictionaries, or documentation.")

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
                    # Reset file buffer position before each operation
                    uploaded_file.seek(0)

                    st.write("Staging file to Snowflake...")
                    staged_path = None
                    try:
                        staged_path = stage_file(uploaded_file, session_id)
                    except Exception as e:
                        st.warning(f"Could not stage file to Snowflake: {e}")
                        st.write("Proceeding with in-memory processing...")
                        staged_path = f"@CONTEXT_ENGINE.UPLOADS/{session_id}/{uploaded_file.name}"

                    doc_id = save_document(
                        session_id=session_id,
                        filename=uploaded_file.name,
                        file_type=file_type,
                        file_size_bytes=uploaded_file.size,
                        stage_path=staged_path,
                    )

                    st.write("Extracting text content...")
                    uploaded_file.seek(0)
                    result = process_file(uploaded_file, staged_path)

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


# =============================================================================
# PAGE: CONTEXT INTERVIEW
# =============================================================================
elif page == "💬 Context Interview":
    st.title("💬 Context Interview")

    if "interview_session_id" not in st.session_state:
        st.info("Please create or select a session from the sidebar first.")

        try:
            sessions_list = list_sessions()
            if sessions_list:
                st.markdown("### Existing Sessions")
                for s in sessions_list:
                    if st.button(f"Resume: {s['SESSION_NAME']}", key=s["SESSION_ID"]):
                        st.session_state.interview_session_id = s["SESSION_ID"]
                        st.rerun()
        except Exception:
            pass
        st.stop()

    session_id = st.session_state.interview_session_id

    # Initialize Chat State
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

    # Sidebar: Schema Discovery
    st.sidebar.markdown("---")
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
                    for db in databases[:10]:
                        try:
                            schemas_list = get_schemas(db)
                            for schema in schemas_list[:20]:
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

    # Start Interview
    if not st.session_state.interview_started:
        st.markdown(
            """
            ### Ready to start the context interview?

            I'll ask you a series of open-ended questions about your data environment.
            Each question builds on your previous answers to go deeper.

            **Tips:**
            - **Type or talk** — use the text box or expand 🎙️ Voice Mode to record your answer
            - Don't worry about being perfectly structured — stream of consciousness is great
            - You can skip topics, jump to any topic from the sidebar, or switch to free-form mode
            - Every answer is summarized and broken into individual facts you can review later
            - You can pause and resume the interview any time
            """
        )

        if st.button("Start Interview", type="primary"):
            st.session_state.interview_started = True

            topic = TOPIC_AREAS[0]
            question = get_starter_question(session_id, topic["id"])
            st.session_state.chat_messages.append({
                "role": "assistant",
                "content": question,
                "topic_id": topic["id"],
            })
            st.rerun()
        st.stop()

    # Chat Display
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

            if msg["role"] == "user" and msg.get("summary"):
                with st.expander("📝 AI Summary", expanded=False):
                    st.markdown(msg["summary"])
                if msg.get("facts"):
                    with st.expander(f"📌 Extracted Facts ({len(msg['facts'])})", expanded=False):
                        for fact in msg["facts"]:
                            cat = fact.get("category", "").replace("_", " ").title()
                            st.markdown(f"- **[{cat}]** {fact.get('fact', '')}")

    # -----------------------------------------------------------------
    # Input: Text + Voice side-by-side
    # -----------------------------------------------------------------
    user_input = None

    # Voice recording section — shown inline above the chat input
    _audio_input_available = hasattr(st, "audio_input")

    if _audio_input_available:
        with st.expander("🎙️ **Voice Mode** — Record your answer instead of typing", expanded=False):
            st.caption(
                "Click the microphone to record, then stop when you're done. "
                "Your recording will be transcribed and cleaned up automatically."
            )
            try:
                audio_data = st.audio_input(
                    "Record your answer",
                    key="voice_recording",
                )
            except Exception:
                audio_data = None
                st.warning("Voice recording widget failed to load. Try typing instead.")

            if audio_data is not None:
                audio_bytes = audio_data.getvalue()
                if audio_bytes and len(audio_bytes) > 0:
                    # Store transcription in session state to survive reruns
                    rec_key = f"voice_transcript_{len(st.session_state.chat_messages)}"
                    if rec_key not in st.session_state:
                        with st.spinner("Transcribing audio with OpenAI Whisper..."):
                            try:
                                raw_transcript = transcribe_audio(audio_bytes)
                                if raw_transcript:
                                    cleaned = summarize_transcript(raw_transcript)
                                    st.session_state[rec_key] = {
                                        "status": "ok",
                                        "raw": raw_transcript,
                                        "cleaned": cleaned,
                                    }
                                else:
                                    st.session_state[rec_key] = {
                                        "status": "empty",
                                    }
                            except Exception as e:
                                st.session_state[rec_key] = {
                                    "status": "error",
                                    "error": str(e),
                                }

                    transcript_data = st.session_state.get(rec_key) or {}
                    if transcript_data.get("status") == "ok":
                        st.markdown("**Raw Transcript:**")
                        st.info(transcript_data["raw"])
                        if transcript_data["cleaned"] != transcript_data["raw"]:
                            st.markdown("**Cleaned Up:**")
                            st.success(transcript_data["cleaned"])
                        if st.button("✅ Submit Voice Answer", type="primary"):
                            user_input = transcript_data["cleaned"]
                            del st.session_state[rec_key]
                    elif transcript_data.get("status") == "error":
                        st.error(f"Transcription failed: {transcript_data['error']}")
                        if st.button("🔄 Retry", key=f"retry_{rec_key}"):
                            del st.session_state[rec_key]
                            st.rerun()
                    elif transcript_data.get("status") == "empty":
                        st.warning("No speech detected. Try recording again.")
                        if st.button("🔄 Retry", key=f"retry_{rec_key}"):
                            del st.session_state[rec_key]
                            st.rerun()

    # Text input — always available
    text_input = st.chat_input("Type your answer here... (or use 🎙️ Voice Mode above)")
    if text_input:
        user_input = text_input

    if user_input:
        st.session_state.chat_messages.append({
            "role": "user",
            "content": user_input,
        })
        with st.chat_message("user"):
            st.markdown(user_input)

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

        st.session_state.chat_messages[-1]["summary"] = result["summary"]
        st.session_state.chat_messages[-1]["facts"] = result["facts"]

        if st.session_state.free_mode:
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

        st.session_state.chat_messages.append({
            "role": "assistant",
            "content": next_q["question"],
            "topic_id": next_q["topic_id"],
        })

        st.rerun()


# =============================================================================
# PAGE: REVIEW & EDIT
# =============================================================================
elif page == "📝 Review & Edit":
    st.title("📝 Review & Edit Context")

    if "interview_session_id" not in st.session_state:
        st.info("Please select a session first.")
        try:
            sessions_list = list_sessions()
            for s in sessions_list:
                if st.button(f"Open: {s['SESSION_NAME']}", key=s["SESSION_ID"]):
                    st.session_state.interview_session_id = s["SESSION_ID"]
                    st.rerun()
        except Exception:
            pass
        st.stop()

    session_id = st.session_state.interview_session_id

    view_mode = st.radio(
        "View Mode",
        ["By Category", "By Entity", "Timeline", "All Facts"],
        horizontal=True,
    )

    try:
        all_facts = get_all_facts(session_id)
    except Exception as e:
        st.error(f"Could not load facts: {e}")
        st.stop()

    if not all_facts:
        st.info("No context facts captured yet. Start an interview or upload documents to get started.")
        st.stop()

    st.markdown(f"**{len(all_facts)} total facts** captured in this session.")

    # By Category View
    if view_mode == "By Category":
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

        other_facts = [f for f in all_facts if f.get("CATEGORY") not in CATEGORY_LABELS]
        if other_facts:
            with st.expander(f"❓ Other ({len(other_facts)})"):
                for fact in other_facts:
                    _render_fact_card(fact)

    # By Entity View
    elif view_mode == "By Entity":
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
                entity_facts = by_entity[entity_name]
                with st.expander(f"🗃️ {entity_name} ({len(entity_facts)} facts)"):
                    for fact in entity_facts:
                        _render_fact_card(fact)

        if unlinked:
            with st.expander(f"📌 General (not linked to a table) ({len(unlinked)})"):
                for fact in unlinked:
                    _render_fact_card(fact)

    # Timeline View
    elif view_mode == "Timeline":
        sorted_facts = sorted(all_facts, key=lambda f: f.get("CREATED_AT", ""))
        for fact in sorted_facts:
            _render_fact_card(fact, show_timestamp=True)

    # All Facts View (table)
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


# =============================================================================
# PAGE: EXPORT & DEPLOY
# =============================================================================
elif page == "🚀 Export & Deploy":
    st.title("🚀 Export & Deploy")

    if "interview_session_id" not in st.session_state:
        st.info("Please select a session first.")
        try:
            sessions_list = list_sessions()
            for s in sessions_list:
                if st.button(f"Open: {s['SESSION_NAME']}", key=s["SESSION_ID"]):
                    st.session_state.interview_session_id = s["SESSION_ID"]
                    st.rerun()
        except Exception:
            pass
        st.stop()

    session_id = st.session_state.interview_session_id

    try:
        facts = get_all_facts(session_id)
        st.markdown(f"**Session has {len(facts)} context facts** to generate from.")
    except Exception:
        facts = []

    if not facts:
        st.warning("No context captured yet. Run an interview or upload documents first.")
        st.stop()

    tab_yaml, tab_vqr, tab_instructions, tab_markdown, tab_rag = st.tabs([
        "📐 Semantic View YAML",
        "✅ Verified Queries (VQR)",
        "📋 Custom Instructions",
        "📄 Markdown Doc",
        "🔍 RAG / Cortex Search",
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
                        sf_session = get_session()
                        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                                         delete=False) as tmp:
                            tmp.write(yaml_to_deploy)
                            tmp_path = tmp.name
                        try:
                            sf_session.file.put(tmp_path, stage_name, auto_compress=False,
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

    # --- RAG / Cortex Search ---
    with tab_rag:
        st.markdown("### RAG / Cortex Search Export")
        st.markdown(
            "Generate chunked context documents optimized for **Retrieval-Augmented Generation (RAG)** "
            "and **Snowflake Cortex Search**. Each chunk includes metadata for filtering and "
            "is sized for efficient vector embedding."
        )

        st.markdown("#### Chunk Types Generated")
        st.markdown("""
- **Individual Facts** — Fine-grained, one fact per chunk (best for precise retrieval)
- **Table Context** — All facts grouped by table (good for table-level questions)
- **Interview Q&A** — Full question-answer pairs from interviews (rich conversational context)
- **Document Summaries** — Summaries of uploaded documents
- **Document Chunks** — Uploaded document text split into ~1000 char overlapping segments
- **Schema Descriptions** — Table/column metadata from schema introspection
        """)

        rag_col1, rag_col2 = st.columns(2)

        with rag_col1:
            if st.button("Generate RAG Chunks", type="primary", key="gen_rag"):
                with st.spinner("Generating RAG chunks..."):
                    chunks = generate_rag_chunks(session_id)
                    jsonl_content = "\n".join(json.dumps(c, default=str) for c in chunks)

                st.session_state.generated_rag_chunks = chunks
                st.session_state.generated_rag_jsonl = jsonl_content
                save_artifact(session_id, "RAG_JSONL", "rag_chunks.jsonl", jsonl_content)
                st.success(f"Generated **{len(chunks)}** chunks!")

        with rag_col2:
            if st.button("Generate Cortex Search SQL", key="gen_cs_sql"):
                rag_table = st.session_state.get("rag_table_name", "CONTEXT_ENGINE.RAG_CHUNKS")
                sql = generate_cortex_search_sql(session_id, rag_table)
                st.session_state.generated_cs_sql = sql

        if "generated_rag_chunks" in st.session_state:
            chunks = st.session_state.generated_rag_chunks

            # Summary stats
            chunk_types = {}
            for c in chunks:
                ct = c.get("chunk_type", "unknown")
                chunk_types[ct] = chunk_types.get(ct, 0) + 1

            st.markdown("#### Chunk Summary")
            summary_cols = st.columns(len(chunk_types))
            for i, (ct, count) in enumerate(sorted(chunk_types.items())):
                with summary_cols[i]:
                    st.metric(ct.replace("_", " ").title(), count)

            # Preview
            st.markdown("#### Preview (first 10 chunks)")
            for chunk in chunks[:10]:
                with st.expander(
                    f"[{chunk['chunk_type']}] {chunk['content'][:80]}..."
                ):
                    st.json(chunk)

            # Downloads
            st.markdown("#### Download")
            dl_col1, dl_col2 = st.columns(2)
            with dl_col1:
                st.download_button(
                    "Download JSONL (for Cortex Search / RAG)",
                    data=st.session_state.generated_rag_jsonl,
                    file_name="rag_chunks.jsonl",
                    mime="application/jsonl",
                )
            with dl_col2:
                st.download_button(
                    "Download JSON Array",
                    data=json.dumps(chunks, indent=2, default=str),
                    file_name="rag_chunks.json",
                    mime="application/json",
                )

            # Load to Snowflake table
            st.markdown("---")
            st.markdown("#### Load to Snowflake Table")
            rag_table_name = st.text_input(
                "Target Table",
                value="CONTEXT_ENGINE.RAG_CHUNKS",
                key="rag_table_name",
            )

            if st.button("Create Table & Load Chunks", key="load_rag"):
                with st.spinner("Creating table and loading chunks..."):
                    try:
                        sql_ddl = generate_cortex_search_sql(session_id, rag_table_name)
                        # Execute the CREATE TABLE statement
                        create_stmt = sql_ddl.split(";")[0] + ";"
                        # Remove comment lines
                        create_stmt = "\n".join(
                            line for line in create_stmt.split("\n")
                            if not line.strip().startswith("--")
                        )
                        run_ddl(create_stmt)

                        # Truncate existing data for this session
                        try:
                            run_ddl(
                                f"DELETE FROM {rag_table_name} "
                                f"WHERE SESSION_ID = '{_esc(session_id)}'"
                            )
                        except Exception:
                            pass

                        # Insert chunks
                        loaded = 0
                        for chunk in chunks:
                            try:
                                tags_sql = "ARRAY_CONSTRUCT(" + ", ".join(
                                    f"'{_esc(t)}'" for t in chunk.get("tags", [])
                                ) + ")"
                                run_ddl(f"""
                                    INSERT INTO {rag_table_name}
                                    (CHUNK_ID, CHUNK_TYPE, CONTENT, CATEGORY,
                                     ENTITY_DATABASE, ENTITY_SCHEMA, ENTITY_TABLE,
                                     ENTITY_COLUMN, SOURCE_TYPE, CONFIDENCE,
                                     IS_VERIFIED, TAGS, SESSION_ID)
                                    VALUES (
                                        '{_esc(chunk["chunk_id"])}',
                                        '{_esc(chunk.get("chunk_type", ""))}',
                                        '{_esc(chunk.get("content", ""))}',
                                        '{_esc(chunk.get("category", ""))}',
                                        {_sql_str(chunk.get("entity_database"))},
                                        {_sql_str(chunk.get("entity_schema"))},
                                        {_sql_str(chunk.get("entity_table"))},
                                        {_sql_str(chunk.get("entity_column"))},
                                        '{_esc(chunk.get("source_type", ""))}',
                                        {chunk.get("confidence", 0.8)},
                                        {chunk.get("is_verified", False)},
                                        {tags_sql},
                                        '{_esc(session_id)}'
                                    )
                                """)
                                loaded += 1
                            except Exception as e:
                                st.warning(f"Failed to insert chunk {chunk['chunk_id']}: {e}")
                                continue

                        st.success(
                            f"Loaded **{loaded}/{len(chunks)}** chunks into `{rag_table_name}`"
                        )
                        st.info(
                            "To create a Cortex Search service, run:\n\n"
                            f"```sql\n"
                            f"CREATE OR REPLACE CORTEX SEARCH SERVICE {rag_table_name}_SEARCH\n"
                            f"  ON {rag_table_name}\n"
                            f"  WAREHOUSE = YOUR_WAREHOUSE\n"
                            f"  TARGET_LAG = '1 hour'\n"
                            f"  AS (\n"
                            f"    SELECT CHUNK_ID, CONTENT, CATEGORY, ENTITY_TABLE,\n"
                            f"           CHUNK_TYPE, TAGS\n"
                            f"    FROM {rag_table_name}\n"
                            f"  );\n"
                            f"```"
                        )
                    except Exception as e:
                        st.error(f"Failed to load chunks: {e}")

        if st.session_state.get("generated_cs_sql"):
            st.markdown("#### Cortex Search Setup SQL")
            st.code(st.session_state.generated_cs_sql, language="sql")
            st.download_button(
                "Download SQL",
                data=st.session_state.generated_cs_sql,
                file_name="cortex_search_setup.sql",
                mime="text/sql",
                key="dl_cs_sql",
            )

    # Version History
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

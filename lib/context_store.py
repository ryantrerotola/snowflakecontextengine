"""
Context Store
=============
CRUD operations for context data: sessions, responses, facts, documents, artifacts.
All data is stored in the CONTEXT_ENGINE schema tables.
"""

import uuid
from datetime import datetime

from lib.snowflake_connection import get_session, run_query, run_ddl


# =============================================================================
# Sessions
# =============================================================================

def create_session(session_name: str, target_database: str = None,
                   target_schema: str = None) -> str:
    """Create a new interview session. Returns the session ID."""
    session_id = str(uuid.uuid4())
    session = get_session()
    session.sql(
        f"""INSERT INTO CONTEXT_ENGINE.INTERVIEW_SESSIONS
            (SESSION_ID, SESSION_NAME, TARGET_DATABASE, TARGET_SCHEMA)
            VALUES ('{session_id}', '{_esc(session_name)}',
                    {_sql_str(target_database)}, {_sql_str(target_schema)})"""
    ).collect()
    return session_id


def get_session_by_id(session_id: str) -> dict | None:
    """Retrieve a session by ID."""
    rows = run_query(
        f"SELECT * FROM CONTEXT_ENGINE.INTERVIEW_SESSIONS WHERE SESSION_ID = '{_esc(session_id)}'"
    )
    return rows[0] if rows else None


def list_sessions() -> list[dict]:
    """List all interview sessions, most recent first."""
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
# Interview Responses
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
# Context Facts
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
# Uploaded Documents
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
# Artifacts
# =============================================================================

def save_artifact(session_id: str, artifact_type: str,
                  artifact_name: str, content: str) -> str:
    """Save a generated artifact. Returns the artifact ID."""
    artifact_id = str(uuid.uuid4())
    # Get next version number
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
# Schema Cache
# =============================================================================

def cache_schema(session_id: str, columns: list[dict]) -> None:
    """Cache introspected schema metadata."""
    session = get_session()
    # Clear existing cache for this session
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
# Helpers
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

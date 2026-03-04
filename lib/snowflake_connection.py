"""
Snowflake Connection Module
============================
Provides a Snowflake session for Streamlit in Snowflake (SiS).
Falls back to snowflake.connector for local development.
"""

import streamlit as st
from snowflake.snowpark.context import get_active_session
from snowflake.snowpark import Session


def get_session() -> Session:
    """Get or create a Snowpark session.

    In SiS, uses the native active session.
    For local dev, reads credentials from st.secrets.
    """
    if "snowpark_session" not in st.session_state:
        try:
            # Streamlit in Snowflake — native session
            session = get_active_session()
        except Exception:
            # Local development fallback
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
        # Use parameterized query via snowpark sql
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
    # Escape single quotes in the prompt
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

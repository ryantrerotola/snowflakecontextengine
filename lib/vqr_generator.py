"""
Verified Query Repository Generator
====================================
Generates verified query entries from captured context facts,
particularly from common queries and metric definitions.
Uses Cortex COMPLETE to generate SQL from natural language descriptions.
"""

import json
from datetime import date

from lib.snowflake_connection import call_cortex_complete
from lib.context_store import get_all_facts, get_cached_schema

import yaml


def generate_vqr(session_id: str) -> str:
    """Generate a Verified Query Repository YAML from captured context.

    Returns YAML string with verified_queries section.
    """
    facts = get_all_facts(session_id)
    try:
        schema_cache = get_cached_schema(session_id)
    except Exception:
        schema_cache = []

    if not facts:
        return "# No context captured yet."

    # Collect relevant facts for query generation
    metric_facts = [f for f in facts if f.get("CATEGORY") == "metric_definition"]
    rule_facts = [f for f in facts if f.get("CATEGORY") == "business_rule"]
    caveat_facts = [f for f in facts if f.get("CATEGORY") == "caveat"]
    table_facts = [f for f in facts if f.get("CATEGORY") == "table_description"]

    # Build schema context string
    schema_context = _build_schema_context(schema_cache)

    # Build business rules context
    rules_context = "\n".join(f"- {f['FACT_TEXT']}" for f in rule_facts[:20])
    caveats_context = "\n".join(f"- {f['FACT_TEXT']}" for f in caveat_facts[:10])

    # Generate queries from metrics
    queries = []
    for metric in metric_facts[:15]:  # Limit to prevent excessive LLM calls
        query_set = _generate_queries_for_metric(
            metric, schema_context, rules_context, caveats_context
        )
        queries.extend(query_set)

    # Generate queries from common question patterns
    common_queries = _generate_common_queries(facts, schema_context, rules_context)
    queries.extend(common_queries)

    if not queries:
        return "# No queries could be generated. Add more metric definitions and common query patterns."

    # Build YAML output
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
    for table_name, columns in list(tables.items())[:20]:  # Limit
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
    # Look for facts that describe common questions
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

"""
Semantic View YAML Generator
==============================
Generates Cortex Analyst Semantic View YAML from captured context facts.
Outputs a complete semantic model with tables, dimensions, time_dimensions,
measures, relationships, and verified queries.
"""

import json

import yaml

from lib.snowflake_connection import call_cortex_complete
from lib.context_store import get_all_facts, get_cached_schema


def generate_semantic_yaml(session_id: str, model_name: str = None) -> str:
    """Generate a complete Semantic View YAML from captured context.

    Args:
        session_id: The session to generate from
        model_name: Name for the semantic model (auto-generated if None)

    Returns:
        YAML string
    """
    facts = get_all_facts(session_id)
    try:
        schema_cache = get_cached_schema(session_id)
    except Exception:
        schema_cache = []

    if not facts and not schema_cache:
        return "# No context captured yet. Run an interview or upload documents first."

    # Build the semantic model structure
    model = _build_model_structure(facts, schema_cache, model_name)

    # Use Cortex to enrich descriptions and synonyms
    model = _enrich_with_llm(model, facts)

    # Convert to YAML
    return yaml.dump(model, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _build_model_structure(facts: list[dict], schema_cache: list[dict],
                           model_name: str = None) -> dict:
    """Build the base semantic model structure from facts and schema."""
    # Determine model name from facts
    if not model_name:
        db_facts = [f for f in facts if f.get("ENTITY_DATABASE")]
        if db_facts:
            model_name = f"{db_facts[0]['ENTITY_DATABASE'].lower()}_analytics"
        else:
            model_name = "data_analytics"

    # Group schema columns by table
    tables_schema = {}
    for col in schema_cache:
        key = (col["DATABASE_NAME"], col["SCHEMA_NAME"], col["TABLE_NAME"])
        if key not in tables_schema:
            tables_schema[key] = []
        tables_schema[key].append(col)

    # Group facts by entity table
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

    # Build table definitions
    tables = []

    # Process tables from schema cache first
    for (db, schema, table_name), columns in tables_schema.items():
        table_def = _build_table_def(
            db, schema, table_name, columns,
            facts_by_table.get(table_name, [])
        )
        tables.append(table_def)
        # Remove from facts_by_table to avoid duplication
        facts_by_table.pop(table_name, None)

    # Process tables mentioned only in facts (not in schema cache)
    for table_name, table_facts in facts_by_table.items():
        # Try to infer database/schema from facts
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

    # Build the model
    model = {
        "name": model_name,
        "description": _generate_model_description(facts),
        "tables": tables,
    }

    # Add relationships from relationship facts
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

    # Get description from facts
    desc_facts = [f for f in facts if f.get("CATEGORY") == "table_description"]
    if desc_facts:
        table_def["description"] = desc_facts[0]["FACT_TEXT"]
    else:
        table_def["description"] = f"Table {table_name}"

    # Build dimensions, time_dimensions, and measures from columns and facts
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

        # Determine if this is a time dimension, measure, or regular dimension
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
            # Check if there's a metric definition for this column
            metric_fact = metric_facts.get(col_name.upper())
            if metric_fact:
                measure = {
                    "name": col_name.lower(),
                    "description": metric_fact["FACT_TEXT"],
                    "expr": f"SUM({col_name})",  # Default aggregation
                    "data_type": "NUMBER",
                }
                measures.append(measure)
            else:
                # Could be a dimension (e.g., ID) or measure
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

    # Add measures from metric_definition facts not tied to schema columns
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

    # Use LLM to parse relationship facts into structured join definitions
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
        # Parse JSON
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
    # Collect terminology facts for synonym generation
    term_facts = [f for f in facts if f.get("CATEGORY") == "terminology"]
    if not term_facts:
        return model

    terms_text = "\n".join(f["FACT_TEXT"] for f in term_facts[:20])

    # Add synonyms to dimensions and measures based on terminology
    for table in model.get("tables", []):
        for dim_list in [table.get("dimensions", []), table.get("time_dimensions", []),
                         table.get("measures", [])]:
            for item in dim_list:
                # Check if any terminology fact references this item
                name = item["name"]
                matching_terms = [
                    t for t in term_facts
                    if name.lower() in t["FACT_TEXT"].lower()
                ]
                if matching_terms:
                    # Extract synonyms from the terminology
                    synonyms = []
                    for t in matching_terms:
                        # Simple extraction: the term fact itself might contain aliases
                        text = t["FACT_TEXT"]
                        if "means" in text.lower() or "also known as" in text.lower():
                            synonyms.append(text.split("means")[-1].strip()[:50])
                    if synonyms:
                        item["synonyms"] = synonyms[:5]

    return model

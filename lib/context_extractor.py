"""
Context Extractor
=================
Extracts structured context facts from document text using Cortex COMPLETE.
Identifies business terminology, metrics, rules, relationships, and caveats.
"""

import json

from lib.snowflake_connection import call_cortex_complete
from lib.context_store import save_fact


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
    """Extract structured facts from document text.

    Args:
        text: The document text to analyze
        session_id: Current session ID
        source_type: DOCUMENT or INTERVIEW
        source_id: ID of the source document or response
        entity_database: Default database context
        entity_schema: Default schema context

    Returns:
        List of saved fact dicts with fact_id included
    """
    if not text or len(text.strip()) < 20:
        return []

    # Truncate very long texts to stay within model limits
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
    # Try to extract JSON from the response
    response = response.strip()

    # Handle markdown code blocks
    if "```json" in response:
        response = response.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in response:
        response = response.split("```", 1)[1].split("```", 1)[0].strip()

    # Find the JSON array
    start = response.find("[")
    end = response.rfind("]")
    if start != -1 and end != -1:
        response = response[start : end + 1]

    try:
        facts = json.loads(response)
        if isinstance(facts, list):
            # Validate each fact has required fields
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

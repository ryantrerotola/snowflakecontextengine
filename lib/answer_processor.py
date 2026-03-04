"""
Answer Processor
================
Processes user interview responses through a pipeline:
1. Raw capture (verbatim)
2. Summarization via Cortex COMPLETE
3. Fact extraction and categorization
4. Cross-referencing with known entities
"""

import json

from lib.snowflake_connection import call_cortex_complete
from lib.context_store import save_response, save_fact, get_cached_schema
from lib.context_extractor import extract_facts_from_text


def process_answer(session_id: str, topic_area: str, question: str,
                   raw_answer: str, sequence_number: int) -> dict:
    """Process a user's interview answer through the full pipeline.

    Returns dict with:
        response_id: str
        summary: str
        facts: list[dict]
    """
    # Step 1: Summarize the answer
    summary = _summarize_answer(question, raw_answer, topic_area)

    # Step 2: Save the response
    response_id = save_response(
        session_id=session_id,
        topic_area=topic_area,
        question=question,
        raw_answer=raw_answer,
        summarized_answer=summary,
        sequence_number=sequence_number,
    )

    # Step 3: Extract structured facts
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
    """Extract structured facts from the answer and save them.

    Uses the topic area to guide categorization:
    - environment_overview / schema_deep_dive -> table_description
    - key_tables -> relationship
    - business_terminology -> terminology
    - metrics_kpis -> metric_definition
    - business_rules -> business_rule
    - common_queries -> metric_definition (query patterns)
    - data_caveats -> caveat
    - access_patterns -> access_pattern
    """
    # Get schema context for entity resolution
    try:
        schema_cache = get_cached_schema(session_id)
        known_tables = set()
        for col in schema_cache:
            known_tables.add(col["TABLE_NAME"])
    except Exception:
        known_tables = set()

    # Build a context-aware extraction prompt
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

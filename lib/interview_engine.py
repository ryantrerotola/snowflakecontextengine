"""
Interview Engine
================
Adaptive conversational interview that progressively builds context about
a Snowflake data environment. Uses Cortex COMPLETE to generate follow-up
questions based on previous answers.
"""

import json

from lib.snowflake_connection import call_cortex_complete
from lib.context_store import get_responses, get_cached_schema, get_all_facts


# =============================================================================
# Topic Areas
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
        "starter_question": None,  # Dynamically generated from collected context
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
    return len(TOPIC_AREAS) - 1  # All done, go to review


def get_next_question(session_id: str, current_topic_id: str,
                      user_answer: str = None) -> dict:
    """Generate the next question based on context.

    Returns dict with keys: question, topic_id, is_followup, is_review
    """
    responses = get_responses(session_id)

    # If this is the review topic, generate a summary review
    if current_topic_id == "review_gaps":
        return _generate_review_question(session_id)

    # If user just answered, try to generate a follow-up
    if user_answer:
        followup = _generate_followup(session_id, current_topic_id, user_answer, responses)
        if followup:
            return {
                "question": followup,
                "topic_id": current_topic_id,
                "is_followup": True,
                "is_review": False,
            }

    # Move to next topic
    current_idx = next(
        (i for i, t in enumerate(TOPIC_AREAS) if t["id"] == current_topic_id),
        -1,
    )
    next_idx = current_idx + 1
    if next_idx >= len(TOPIC_AREAS):
        return _generate_review_question(session_id)

    next_topic = TOPIC_AREAS[next_idx]

    # Contextualize the starter question with schema info if available
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

    # If we have schema cache, enrich the question with known entities
    try:
        schema_cache = get_cached_schema(session_id)
    except Exception:
        schema_cache = []

    if not schema_cache and not responses:
        return base_question

    # Build context from previous responses
    prev_context = ""
    for r in responses[-5:]:  # Last 5 responses for context
        prev_context += f"Q: {r['QUESTION_TEXT'][:200]}\nA: {r['RAW_ANSWER'][:300]}\n\n"

    # Build schema context
    schema_context = ""
    if schema_cache:
        tables = {}
        for col in schema_cache[:100]:  # Limit to prevent prompt overflow
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
    """Generate a contextual follow-up question based on the user's answer.

    Returns None if no follow-up is needed (move to next topic).
    """
    # Count how many follow-ups we've done for this topic
    topic_responses = [r for r in responses if r.get("TOPIC_AREA") == topic_id]
    if len(topic_responses) >= 4:
        # Cap at 4 exchanges per topic to avoid getting stuck
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
        # Group facts by category
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
            # Estimate based on answer length
            total_chars = sum(len(r.get("RAW_ANSWER", "")) for r in topic_responses)
            scores[topic["id"]] = min(1.0, total_chars / 500)
    return scores

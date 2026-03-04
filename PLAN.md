# Snowflake Context Engine - Implementation Plan

## Product Vision

A Streamlit application that enables teams to upload and generate rich context for Snowflake Intelligence agents. The app combines **document uploads** (PDFs, PowerPoints, text) with an **intelligent conversational interview** that builds progressively deeper understanding of your data environment. All captured context is saved in both human-editable and Snowflake-native formats (Semantic View YAML, Verified Query Repository, Custom Instructions).

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                   Streamlit App (SiS)                    │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐ │
│  │  Upload Tab   │  │ Interview Tab│  │  Review Tab   │ │
│  │              │  │              │  │               │ │
│  │ PDFs, PPTs,  │  │ Adaptive Q&A │  │ View/Edit     │ │
│  │ TXT, DOCX    │  │ conversation │  │ all context   │ │
│  │              │  │              │  │               │ │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘ │
│         │                 │                   │         │
│  ┌──────▼─────────────────▼───────────────────▼───────┐ │
│  │              Context Processing Engine              │ │
│  │  - Document parsing (Cortex PARSE_DOCUMENT)        │ │
│  │  - LLM summarization (Cortex COMPLETE)             │ │
│  │  - Context structuring & deduplication              │ │
│  └──────────────────────┬─────────────────────────────┘ │
│                         │                               │
│  ┌──────────────────────▼─────────────────────────────┐ │
│  │              Output Generation Engine               │ │
│  │  - Semantic View YAML                              │ │
│  │  - Verified Query Repository (VQR)                 │ │
│  │  - Custom Instructions                             │ │
│  │  - Human-readable context docs (Markdown)          │ │
│  └──────────────────────┬─────────────────────────────┘ │
│                         │                               │
└─────────────────────────┼───────────────────────────────┘
                          │
              ┌───────────▼───────────┐
              │    Snowflake Stage    │
              │  & Semantic Views     │
              └───────────────────────┘
```

---

## Phase 1: Project Foundation & Core Infrastructure

### 1.1 Project Setup
- **Files**: `app.py` (main entry), `requirements.txt`, `environment.yml`
- Python dependencies: `streamlit`, `snowflake-snowpark-python`, `snowflake-ml-python`, `pyyaml`, `python-pptx`, `pypdf2`
- Snowflake connection configuration (via Streamlit secrets / SiS native connection)

### 1.2 Database Objects
- Create a dedicated schema: `CONTEXT_ENGINE`
- Tables:
  - `INTERVIEW_SESSIONS` - tracks each interview session (id, user, timestamp, status)
  - `INTERVIEW_RESPONSES` - stores raw Q&A pairs (session_id, question, raw_answer, summarized_answer, category, timestamp)
  - `UPLOADED_DOCUMENTS` - metadata for uploaded files (id, filename, type, upload_time, stage_path, extracted_text, summary)
  - `CONTEXT_ARTIFACTS` - generated outputs (id, session_id, artifact_type, content, version, created_at)
- Internal stage: `@CONTEXT_ENGINE.UPLOADS` for file staging

### 1.3 App Navigation Structure
- Multi-page Streamlit app with sidebar navigation:
  1. **Upload Documents** - file upload and processing
  2. **Context Interview** - conversational Q&A
  3. **Review & Edit** - view/edit all captured context
  4. **Export & Deploy** - generate and deploy Snowflake artifacts

---

## Phase 2: Document Upload & Processing

### 2.1 Upload Interface (`pages/upload.py`)
- `st.file_uploader` supporting: `.pdf`, `.pptx`, `.docx`, `.txt`, `.csv`, `.md`
- Multi-file upload with drag-and-drop
- Upload progress tracking and file list display
- Files staged to `@CONTEXT_ENGINE.UPLOADS` via Snowpark `session.file.put()`

### 2.2 Document Processing (`lib/document_processor.py`)
- **PDFs**: Use Snowflake's `SNOWFLAKE.CORTEX.PARSE_DOCUMENT()` function
  - Extracts text content from staged PDF files
  - Handles multi-page documents
- **PowerPoints (.pptx)**: Use `python-pptx` to extract slide text, notes, and table data
- **Word docs (.docx)**: Use `python-docx` for text extraction
- **Plain text / Markdown**: Direct read
- After extraction, use `SNOWFLAKE.CORTEX.SUMMARIZE()` to create concise summaries
- Store both raw extracted text and summaries in `UPLOADED_DOCUMENTS` table

### 2.3 Context Extraction from Documents (`lib/context_extractor.py`)
- After documents are processed, use `SNOWFLAKE.CORTEX.COMPLETE()` with a structured prompt to extract:
  - Business terminology and definitions
  - Referenced metrics and KPIs
  - Business rules and logic
  - Table/column relationships mentioned
  - Data quality notes or caveats
- Store extracted context items tagged by category

---

## Phase 3: Conversational Context Interview (Core Feature)

### 3.1 Interview Engine (`lib/interview_engine.py`)

The interview follows a **progressive disclosure** pattern - it starts broad and drills deeper based on answers. The engine maintains a conversation state machine with these topic areas:

#### Topic Flow (ordered, with adaptive branching):

```
1. ENVIRONMENT OVERVIEW
   "Tell me about your Snowflake environment at a high level.
    What databases do you have, and what are they used for?"
        │
        ▼
2. SCHEMA DEEP-DIVE (for each database mentioned)
   "You mentioned [database]. Walk me through the schemas in it.
    What's the purpose of each schema, and how do they relate?"
        │
        ▼
3. KEY TABLES & RELATIONSHIPS
   "What are the most important tables in [schema]?
    How do they connect to each other?"
        │
        ▼
4. BUSINESS TERMINOLOGY
   "What business terms does your team use that might not be obvious
    from column names? (e.g., 'ARR' means Annual Recurring Revenue)"
        │
        ▼
5. METRICS & KPIS
   "What are the key metrics your team tracks?
    How is each one calculated - what tables/columns feed into them?"
        │
        ▼
6. BUSINESS RULES & LOGIC
   "Are there any business rules that affect how data should be queried?
    (e.g., 'revenue is only counted for status=ACTIVE orders',
    'fiscal year starts in February')"
        │
        ▼
7. COMMON QUERIES & USE CASES
   "What questions does your team ask of this data most frequently?
    Can you give me examples of questions someone might type in?"
        │
        ▼
8. DATA CAVEATS & GOTCHAS
   "Are there any quirks, known issues, or things that trip people up?
    (e.g., 'the AMOUNT column is in cents not dollars',
    'deleted records are soft-deleted with is_active=false')"
        │
        ▼
9. ACCESS PATTERNS & SECURITY
   "Are there any columns or tables that should be restricted?
    Any data that should never appear in query results?"
        │
        ▼
10. REVIEW & GAPS
    "Here's what I've captured so far. [summary]
     What did I miss? Anything you'd like to add or correct?"
```

#### Adaptive Questioning Logic:
- Each answer is analyzed in real-time using `CORTEX.COMPLETE()` to:
  1. **Summarize** the key facts from the answer
  2. **Identify follow-up areas** that need clarification
  3. **Generate contextual follow-up questions** based on what was said
- Example: If user mentions "We have a REVENUE table that joins to CUSTOMERS," the engine generates follow-ups like:
  - "What's the join key between REVENUE and CUSTOMERS?"
  - "Are there different types of revenue tracked in that table?"
  - "What time granularity is the REVENUE data at?"
- The engine tracks a **coverage score** across categories to ensure breadth
- Users can skip topics, go back, or free-form add context at any point

### 3.2 Interview UI (`pages/interview.py`)
- Chat-style interface using `st.chat_message` and `st.chat_input`
- Left sidebar shows:
  - Progress through topic areas (checklist)
  - Coverage score per category
  - "Jump to topic" navigation
- Each response is displayed with:
  - The raw user answer
  - A collapsible "AI Summary" showing what was captured
  - Edit button to refine the summary
- Session persistence - can pause and resume interviews
- "Add context freely" mode - user can just type whatever comes to mind

### 3.3 Answer Processing Pipeline (`lib/answer_processor.py`)
Each user response goes through:
1. **Raw capture** - store verbatim in `INTERVIEW_RESPONSES`
2. **Summarization** - `CORTEX.COMPLETE()` extracts structured facts
3. **Categorization** - tag facts by type (business_rule, metric_definition, table_description, relationship, caveat, etc.)
4. **Deduplication** - check against existing context for conflicts or duplicates
5. **Cross-reference** - link new facts to previously mentioned entities (tables, columns, schemas)

---

## Phase 4: Review & Edit Interface

### 4.1 Context Dashboard (`pages/review.py`)
- **By Category view**: business rules, metrics, table descriptions, relationships, caveats - each in expandable sections
- **By Entity view**: select a database/schema/table and see all context related to it
- **Timeline view**: see context in the order it was captured
- Each context item is:
  - Editable (inline edit with save)
  - Deletable
  - Taggable (add/remove category tags)
  - Traceable (shows source: "From interview Q5" or "From uploaded doc: handbook.pdf")

### 4.2 Human-Readable Export
- Generate a clean Markdown document organizing all context:
  ```markdown
  # Data Context: [Environment Name]

  ## Databases & Schemas
  ### ANALYTICS_DB
  - **Purpose**: Core analytics warehouse
  - **Schemas**:
    - `SALES` - Sales transaction data...

  ## Business Rules
  - Revenue is only counted for orders with status = 'COMPLETED'
  ...

  ## Metrics & KPIs
  | Metric | Definition | Source Tables |
  |--------|-----------|--------------|
  | ARR    | SUM(amount) WHERE type='recurring' | REVENUE |
  ...
  ```

---

## Phase 5: Snowflake-Native Output Generation

### 5.1 Semantic View YAML Generator (`lib/yaml_generator.py`)
- Takes all captured context and generates Semantic View YAML:
  ```yaml
  name: sales_analytics
  description: "Sales analytics semantic model..."
  tables:
    - name: orders
      base_table:
        database: ANALYTICS_DB
        schema: SALES
        table: ORDERS
      description: "All customer orders..."
      dimensions:
        - name: order_status
          synonyms: ["status", "order state"]
          description: "Current order status. Only COMPLETED orders count toward revenue."
          expr: ORDER_STATUS
          data_type: VARCHAR
          is_enum: true
          sample_values: ["COMPLETED", "PENDING", "CANCELLED"]
      time_dimensions:
        - name: order_date
          synonyms: ["date", "when ordered"]
          description: "Date the order was placed (UTC)"
          expr: ORDER_DATE
          data_type: DATE
      measures:
        - name: total_revenue
          synonyms: ["revenue", "sales"]
          description: "Total revenue from completed orders"
          expr: "SUM(CASE WHEN ORDER_STATUS = 'COMPLETED' THEN AMOUNT ELSE 0 END)"
          data_type: NUMBER
  ```
- Business rules become: descriptions, custom instructions, and filter conditions
- Metrics become: measure definitions with proper aggregation expressions
- Relationships become: join definitions between tables
- User can preview the YAML before deploying

### 5.2 Verified Query Repository Generator (`lib/vqr_generator.py`)
- From "Common Queries" interview answers and metric definitions, generate VQR entries:
  ```yaml
  verified_queries:
    - name: monthly_revenue
      question: "What was our revenue last month?"
      sql: |
        SELECT DATE_TRUNC('MONTH', ORDER_DATE) AS month,
               SUM(AMOUNT) AS revenue
        FROM ANALYTICS_DB.SALES.ORDERS
        WHERE ORDER_STATUS = 'COMPLETED'
          AND ORDER_DATE >= DATEADD('MONTH', -1, CURRENT_DATE())
        GROUP BY 1
      verified_at: "2026-03-02"
      verified_by: "context_engine"
  ```
- Uses `CORTEX.COMPLETE()` to generate SQL from natural language questions + captured context
- Marks queries as "DRAFT" for human review before verification

### 5.3 Custom Instructions Generator (`lib/instructions_generator.py`)
- Compiles business rules, caveats, and access patterns into Cortex Analyst custom instructions
- Format: plain text instructions that guide SQL generation behavior
- Example output:
  ```
  - Always filter orders by ORDER_STATUS = 'COMPLETED' when calculating revenue
  - The AMOUNT column is stored in cents; divide by 100 for dollar amounts
  - Fiscal year starts February 1st; use FISCAL_YEAR column instead of calendar year
  - Never expose SSN or SALARY columns in query results
  ```

### 5.4 Deploy to Snowflake (`pages/export.py`)
- Preview all generated artifacts side-by-side
- One-click deployment options:
  - **Upload YAML to Stage**: `PUT` the semantic model YAML to a named stage
  - **Create Semantic View**: Execute `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML()`
  - **Save Custom Instructions**: Apply via `ALTER SEMANTIC VIEW ... SET CUSTOM_INSTRUCTIONS`
- Version tracking - each deployment creates a versioned snapshot
- Rollback capability - can restore previous versions

---

## Phase 6: Polish & Advanced Features

### 6.1 Context Enrichment
- Auto-suggest: After interview, use `CORTEX.COMPLETE()` to suggest additional context the user might want to add (identify gaps)
- Schema introspection: Connect to `INFORMATION_SCHEMA` to auto-discover tables, columns, and data types, pre-populating the semantic model
- Sample value extraction: Query actual data to populate `sample_values` in dimensions

### 6.2 Multi-Session Support
- Multiple interview sessions per environment
- Merge context from different team members
- Conflict resolution when different people describe the same entity differently

### 6.3 Iterative Refinement
- After deploying, capture Cortex Analyst accuracy feedback
- Feed back into the context to iteratively improve

---

## File Structure

```
snowflakecontextengine/
├── app.py                          # Main Streamlit entry point
├── requirements.txt                # Python dependencies
├── environment.yml                 # Conda environment (for SiS)
├── setup/
│   └── create_objects.sql          # DDL for tables, stages, etc.
├── pages/
│   ├── 1_upload.py                 # Document upload page
│   ├── 2_interview.py             # Conversational interview page
│   ├── 3_review.py                # Review & edit context page
│   └── 4_export.py                # Export & deploy page
├── lib/
│   ├── snowflake_connection.py    # Snowflake session management
│   ├── document_processor.py      # File parsing & text extraction
│   ├── context_extractor.py       # Extract structured context from docs
│   ├── interview_engine.py        # Adaptive question generation
│   ├── answer_processor.py        # Process & categorize answers
│   ├── yaml_generator.py          # Generate Semantic View YAML
│   ├── vqr_generator.py           # Generate Verified Query Repository
│   ├── instructions_generator.py  # Generate Custom Instructions
│   └── context_store.py           # CRUD operations for context data
└── prompts/
    ├── summarize_answer.txt       # Prompt template for answer summarization
    ├── generate_followup.txt      # Prompt template for follow-up questions
    ├── extract_context.txt        # Prompt template for document context extraction
    ├── generate_yaml.txt          # Prompt template for YAML generation
    └── generate_vqr.txt           # Prompt template for VQR generation
```

---

## Implementation Order

| Step | What | Why First |
|------|------|-----------|
| 1 | Project setup, connection, DB objects | Foundation everything else depends on |
| 2 | Document upload + processing | Simpler feature, validates Snowflake integration |
| 3 | Interview engine + basic UI | Core differentiator, most complex piece |
| 4 | Answer processing pipeline | Makes interview output useful |
| 5 | Review & edit interface | Users need to verify captured context |
| 6 | YAML / Semantic View generator | Primary Snowflake-native output |
| 7 | VQR + Custom Instructions generator | Completes the output formats |
| 8 | Deploy to Snowflake functionality | Closes the loop |
| 9 | Polish, enrichment, multi-session | Nice-to-haves |

---

## Design Decisions (Resolved)

1. **Deployment**: Streamlit in Snowflake (SiS) - native auth, direct stage access, no connection config needed.
2. **LLM Model**: `CORTEX.COMPLETE('mistral-large2', ...)` for all interview question generation, answer processing, and context extraction.
3. **Context Storage**: Individual facts - each piece of context stored as a tagged, searchable row. Easier to deduplicate, edit, and generate YAML from.
4. **Schema Introspection**: Auto-discover via `INFORMATION_SCHEMA` on connect. Pre-populate databases, schemas, tables, columns, and data types. Interview builds on top of discovered schema.

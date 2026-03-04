-- =============================================================================
-- Snowflake Context Engine - Database Objects
-- =============================================================================
-- Run this script once to create the required schema, tables, and stages.
-- Adjust DATABASE_NAME to match your environment.
-- =============================================================================

USE ROLE SYSADMIN;  -- or your preferred role
-- USE DATABASE <YOUR_DATABASE>;

CREATE SCHEMA IF NOT EXISTS CONTEXT_ENGINE;
USE SCHEMA CONTEXT_ENGINE;

-- -----------------------------------------------------------------------------
-- Internal stage for uploaded documents
-- -----------------------------------------------------------------------------
CREATE STAGE IF NOT EXISTS UPLOADS
    DIRECTORY = (ENABLE = TRUE)
    COMMENT = 'Stage for documents uploaded via the Context Engine app';

-- -----------------------------------------------------------------------------
-- Interview sessions
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS INTERVIEW_SESSIONS (
    SESSION_ID          VARCHAR(36)     NOT NULL DEFAULT UUID_STRING(),
    SESSION_NAME        VARCHAR(500),
    CREATED_BY          VARCHAR(256)    DEFAULT CURRENT_USER(),
    CREATED_AT          TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_AT          TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
    STATUS              VARCHAR(50)     DEFAULT 'IN_PROGRESS',  -- IN_PROGRESS, COMPLETED, ARCHIVED
    TARGET_DATABASE     VARCHAR(256),
    TARGET_SCHEMA       VARCHAR(256),
    NOTES               VARCHAR(10000),
    PRIMARY KEY (SESSION_ID)
);

-- -----------------------------------------------------------------------------
-- Interview responses (individual Q&A pairs)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS INTERVIEW_RESPONSES (
    RESPONSE_ID         VARCHAR(36)     NOT NULL DEFAULT UUID_STRING(),
    SESSION_ID          VARCHAR(36)     NOT NULL,
    TOPIC_AREA          VARCHAR(100),   -- e.g. ENVIRONMENT_OVERVIEW, SCHEMA_DEEP_DIVE, etc.
    QUESTION_TEXT       VARCHAR(10000)  NOT NULL,
    RAW_ANSWER          VARCHAR(50000)  NOT NULL,
    SUMMARIZED_ANSWER   VARCHAR(10000),
    SEQUENCE_NUMBER     INTEGER,
    CREATED_AT          TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (RESPONSE_ID),
    FOREIGN KEY (SESSION_ID) REFERENCES INTERVIEW_SESSIONS(SESSION_ID)
);

-- -----------------------------------------------------------------------------
-- Context facts (individual extracted facts from interviews and documents)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CONTEXT_FACTS (
    FACT_ID             VARCHAR(36)     NOT NULL DEFAULT UUID_STRING(),
    SESSION_ID          VARCHAR(36),
    SOURCE_TYPE         VARCHAR(50)     NOT NULL,  -- INTERVIEW, DOCUMENT, MANUAL
    SOURCE_ID           VARCHAR(36),    -- RESPONSE_ID or DOCUMENT_ID
    CATEGORY            VARCHAR(100)    NOT NULL,  -- business_rule, metric_definition, table_description, relationship, caveat, terminology, access_pattern
    ENTITY_DATABASE     VARCHAR(256),
    ENTITY_SCHEMA       VARCHAR(256),
    ENTITY_TABLE        VARCHAR(256),
    ENTITY_COLUMN       VARCHAR(256),
    FACT_TEXT            VARCHAR(10000)  NOT NULL,
    CONFIDENCE          FLOAT           DEFAULT 1.0,
    IS_VERIFIED         BOOLEAN         DEFAULT FALSE,
    CREATED_AT          TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_AT          TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
    CREATED_BY          VARCHAR(256)    DEFAULT CURRENT_USER(),
    PRIMARY KEY (FACT_ID)
);

-- -----------------------------------------------------------------------------
-- Uploaded documents metadata
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS UPLOADED_DOCUMENTS (
    DOCUMENT_ID         VARCHAR(36)     NOT NULL DEFAULT UUID_STRING(),
    SESSION_ID          VARCHAR(36),
    FILENAME            VARCHAR(1000)   NOT NULL,
    FILE_TYPE           VARCHAR(50)     NOT NULL,  -- pdf, pptx, docx, txt, csv, md
    FILE_SIZE_BYTES     INTEGER,
    STAGE_PATH          VARCHAR(2000),
    EXTRACTED_TEXT      VARCHAR,        -- full extracted text (VARIANT-like large text)
    SUMMARY             VARCHAR(10000),
    PROCESSING_STATUS   VARCHAR(50)     DEFAULT 'PENDING',  -- PENDING, PROCESSING, COMPLETED, FAILED
    ERROR_MESSAGE       VARCHAR(5000),
    UPLOADED_AT         TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
    PROCESSED_AT        TIMESTAMP_NTZ,
    UPLOADED_BY         VARCHAR(256)    DEFAULT CURRENT_USER(),
    PRIMARY KEY (DOCUMENT_ID)
);

-- -----------------------------------------------------------------------------
-- Generated artifacts (YAML, VQR, instructions, markdown)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CONTEXT_ARTIFACTS (
    ARTIFACT_ID         VARCHAR(36)     NOT NULL DEFAULT UUID_STRING(),
    SESSION_ID          VARCHAR(36)     NOT NULL,
    ARTIFACT_TYPE       VARCHAR(50)     NOT NULL,  -- SEMANTIC_YAML, VQR_YAML, CUSTOM_INSTRUCTIONS, MARKDOWN_DOC
    ARTIFACT_NAME       VARCHAR(500),
    CONTENT             VARCHAR,        -- the generated content
    VERSION             INTEGER         DEFAULT 1,
    IS_DEPLOYED         BOOLEAN         DEFAULT FALSE,
    DEPLOYED_AT         TIMESTAMP_NTZ,
    CREATED_AT          TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
    CREATED_BY          VARCHAR(256)    DEFAULT CURRENT_USER(),
    PRIMARY KEY (ARTIFACT_ID),
    FOREIGN KEY (SESSION_ID) REFERENCES INTERVIEW_SESSIONS(SESSION_ID)
);

-- -----------------------------------------------------------------------------
-- Schema introspection cache (auto-discovered schema metadata)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS SCHEMA_CACHE (
    CACHE_ID            VARCHAR(36)     NOT NULL DEFAULT UUID_STRING(),
    SESSION_ID          VARCHAR(36)     NOT NULL,
    DATABASE_NAME       VARCHAR(256)    NOT NULL,
    SCHEMA_NAME         VARCHAR(256)    NOT NULL,
    TABLE_NAME          VARCHAR(256)    NOT NULL,
    COLUMN_NAME         VARCHAR(256),
    DATA_TYPE           VARCHAR(256),
    IS_NULLABLE         BOOLEAN,
    ORDINAL_POSITION    INTEGER,
    COMMENT             VARCHAR(10000),
    CACHED_AT           TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (CACHE_ID)
);

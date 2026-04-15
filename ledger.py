from sqlalchemy import create_engine, text
import os
DATABASE_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+psycopg2://agent:agent123@localhost:5432/agentdb"
)

engine = create_engine(DATABASE_URL)

def init_ledger():
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto;"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS audit_ledger (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                timestamp           TIMESTAMPTZ NOT NULL DEFAULT now(),

                -- Trigger context
                trigger_type        VARCHAR(20) NOT NULL,
                trigger_file        TEXT NOT NULL,
                user_prompt         TEXT,

                -- Analyst output
                issue_type          VARCHAR(20) NOT NULL,
                issue_description   TEXT NOT NULL,
                affected_file       TEXT NOT NULL,
                line_start          INTEGER,
                line_end            INTEGER,

                -- Architect output
                diff                TEXT NOT NULL,
                entities_changed    JSONB NOT NULL,
                complexity_before   VARCHAR(20),
                complexity_after    VARCHAR(20),
                improvement         TEXT,

                -- Validator output
                validation_passed   BOOLEAN NOT NULL,
                sandbox_output      TEXT,
                tests_run           INTEGER DEFAULT 0,
                tests_passed        INTEGER DEFAULT 0,
                retry_count         INTEGER DEFAULT 0,

                -- Metadata
                agent_version       VARCHAR(10) DEFAULT 'v1.0'
            );
        """))
        conn.commit()
        print("audit_ledger table ready.")

def write_log(entry: dict):
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO audit_ledger (
                trigger_type, trigger_file, user_prompt,
                issue_type, issue_description, affected_file,
                line_start, line_end,
                diff, entities_changed,
                complexity_before, complexity_after, improvement,
                validation_passed, sandbox_output,
                tests_run, tests_passed, retry_count
            ) VALUES (
                :trigger_type, :trigger_file, :user_prompt,
                :issue_type, :issue_description, :affected_file,
                :line_start, :line_end,
                :diff, :entities_changed,
                :complexity_before, :complexity_after, :improvement,
                :validation_passed, :sandbox_output,
                :tests_run, :tests_passed, :retry_count
            )
        """), entry)
        conn.commit()
        print(f"  Log entry written for {entry['affected_file']}")

def query_log(limit=10):
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, timestamp, trigger_type, affected_file,
                   issue_type, complexity_before, complexity_after,
                   validation_passed, retry_count,
                   tests_run, tests_passed, entities_changed
            FROM audit_ledger
            ORDER BY timestamp DESC
            LIMIT :limit
        """), {"limit": limit})
        rows = result.fetchall()
        return rows

def query_by_entity(entity_name: str):
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT id, timestamp, affected_file, issue_description,
                   issue_type, complexity_before, complexity_after,
                   validation_passed, retry_count,
                   tests_run, tests_passed, entities_changed
            FROM audit_ledger
            WHERE entities_changed @> :entity
            ORDER BY timestamp DESC
        """), {"entity": f'["{entity_name}"]'})
        rows = result.fetchall()
        return rows
-- schema.sql
CREATE TABLE IF NOT EXISTS Active_Problems (
    parent_id TEXT PRIMARY KEY,          -- e.g., 'INC0000032'
    core_issue_summary TEXT NOT NULL,    -- e.g., 'Email server is down'
    status TEXT NOT NULL,                -- 'Active' or 'Resolved'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- 0017_loan_limits.sql
-- Per-member-type concurrent-loan caps, replacing the single hardcoded
-- MAX_CONCURRENT_LOANS constant in routes/transactions.py. Seeded with
-- that same constant's old value (10) for both member types so nothing
-- changes behaviorally until an admin actually edits a limit.

CREATE TABLE "loan_limits" (
    member_type TEXT PRIMARY KEY CHECK (member_type IN ('trainee', 'trainer')),
    max_concurrent_loans INTEGER NOT NULL
);

INSERT INTO loan_limits (member_type, max_concurrent_loans) VALUES ('trainee', 10), ('trainer', 10);

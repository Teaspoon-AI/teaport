-- Acceptances no longer record a name (counsel's advice, 2026-11-23): the email, the use
-- (personal, or a Teaspoon AI license's id) and the accepted text's hash say who accepted
-- what; a typed, unverified name added nothing but personal data kept for seven years.
-- Dropping the column also erases the names recorded before this.
-- Applied by .github/workflows/worker-deploy.yml before each deploy, never by hand from a branch.
ALTER TABLE acceptances DROP COLUMN name;

-- teaport download gate: the factory's maker acceptances (teaport#464).
-- Applied by .github/workflows/worker-deploy.yml before each deploy, never by hand from a branch.
-- Additive only: the Worker before it still runs against this schema (a rollback).
-- What each column holds, and how long it is kept: docs/engine-license-data.md.

-- One row each time the factory station provisions a box (POST /api/factory; a box
-- provisioned again gets a new one): Teaspoon AI installing
-- its own software on its own hardware, not anyone's acceptance of the license (the box's
-- owner is to accept it at setup, teaport#465). The box's agent writes id into its receipt;
-- install.sh gets download tokens with it (POST /api/token) until it is MAKER_MAX_DAYS old.
-- A download with one is a downloads row with acceptance_id = this id.
CREATE TABLE maker_acceptances (
  id            TEXT PRIMARY KEY,           -- "maker-" + 24 random characters
  created_at    TEXT NOT NULL,              -- ISO 8601, UTC
  device_id     TEXT NOT NULL,              -- SHA-256 of "teaport-appliance:" + the box's appliance serial
  installer     TEXT NOT NULL,              -- the revision the box will install, such as git:<12 hex>
  ip            TEXT,                       -- the station's
  country       TEXT,
  user_agent    TEXT,
  mark          TEXT NOT NULL,              -- which FACTORY_TOKEN recorded it (an HMAC of it): the daily cap counts the current one's
  last_token_at TEXT,
  revoked_at    TEXT                        -- set by hand: the box gets no more tokens
);
CREATE INDEX maker_acceptances_created ON maker_acceptances (created_at);   -- the daily cap and the retention cron
CREATE INDEX maker_acceptances_device ON maker_acceptances (device_id);

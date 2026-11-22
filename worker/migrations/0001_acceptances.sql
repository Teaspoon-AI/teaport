-- teaport download gate: who accepted the Teaport Engine License, and what they downloaded.
-- Applied with: npx wrangler d1 migrations apply teaport-gate --remote (from worker/).
-- What each column holds, and how long it is kept: docs/engine-license-data.md.

-- One row per acceptance (POST /api/accept). id is what the installer keeps in its receipt
-- to get a new download token later (POST /api/token); revoked_at set stops both.
CREATE TABLE acceptances (
  id            TEXT PRIMARY KEY,
  created_at    TEXT NOT NULL,              -- ISO 8601, UTC
  channel       TEXT NOT NULL,              -- stable | beta: whose manifest named the license
  eula_version  TEXT NOT NULL,
  eula_sha256   TEXT NOT NULL,              -- of the exact text accepted
  name          TEXT NOT NULL,
  email         TEXT NOT NULL,
  use_type      TEXT NOT NULL CHECK (use_type IN ('personal', 'licensed')),
  license_id    TEXT,                       -- licensed use: the Teaspoon AI license's id
  device_id     TEXT,                       -- the installer's hash of the box's machine id
  method        TEXT NOT NULL CHECK (method IN ('tty', 'flag')),  -- read and typed "accept", or --accept-eula=<version>
  installer     TEXT NOT NULL,              -- install.sh's revision: git:<12 hex>, or unknown
  ip            TEXT,
  country       TEXT,
  user_agent    TEXT,
  last_token_at TEXT,
  revoked_at    TEXT
);
CREATE INDEX acceptances_email ON acceptances (email);
CREATE INDEX acceptances_device ON acceptances (device_id);
CREATE INDEX acceptances_created ON acceptances (created_at);   -- the retention cron

-- One row per gated file request (GET /dl/...): a resumed download is several rows.
CREATE TABLE downloads (
  id            INTEGER PRIMARY KEY,
  at            TEXT NOT NULL,
  acceptance_id TEXT NOT NULL,              -- "ci": the publish workflow's manifest check; "open": no token, GATE open
  key           TEXT NOT NULL,
  range_start   INTEGER,                    -- a resumed download's first byte
  status        INTEGER NOT NULL,
  ip            TEXT,
  country       TEXT
);
CREATE INDEX downloads_acceptance ON downloads (acceptance_id);
CREATE INDEX downloads_at ON downloads (at);   -- the retention cron

-- One row per acceptance of the Teaport Engine License (POST /v1/engine-license/accept).
-- What each column holds, and how long it is kept: docs/engine-license-data.md.
CREATE TABLE acceptances (
  id                TEXT PRIMARY KEY,        -- random UUID, also in the box's eula-accepted.json
  license_version   TEXT NOT NULL,           -- engine.eula.version, e.g. "1.0"
  license_sha256    TEXT NOT NULL,           -- SHA-256 of the exact text accepted (checked against the published text)
  accepted_at       TEXT NOT NULL,           -- UTC, ISO 8601, server clock
  client_ip         TEXT NOT NULL,           -- CF-Connecting-IP of the accepting request
  method            TEXT NOT NULL CHECK (method IN ('tty', 'flag')),
  installer_version TEXT NOT NULL,           -- install.sh's revision, e.g. "git:c570cb19"
  email             TEXT NOT NULL,           -- the identity the person entered
  revoked_at        TEXT                     -- set by hand to stop a token issuing URLs
);
CREATE INDEX acceptances_email ON acceptances (email);
CREATE INDEX acceptances_accepted_at ON acceptances (accepted_at);

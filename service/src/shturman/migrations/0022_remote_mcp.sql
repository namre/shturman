-- OAuth внешнего read-only MCP. Секретные коды, cookie и токены — только SHA-256.
CREATE TABLE remote_mcp_clients (
    id text PRIMARY KEY, name text NOT NULL, redirect_uris jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz
);
CREATE TABLE remote_mcp_requests (
    id bigserial PRIMARY KEY, client_id text NOT NULL REFERENCES remote_mcp_clients(id),
    cookie_hash text NOT NULL UNIQUE, nonce_hash text NOT NULL, redirect_uri text NOT NULL,
    state text NOT NULL, challenge text NOT NULL, resource text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied','delivered')),
    owner_id bigint NOT NULL, expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE remote_mcp_grants (
    id bigserial PRIMARY KEY, client_id text NOT NULL REFERENCES remote_mcp_clients(id),
    owner_id bigint NOT NULL, resource text NOT NULL,
    revoked_at timestamptz, expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE remote_mcp_codes (
    hash text PRIMARY KEY, request_id bigint NOT NULL REFERENCES remote_mcp_requests(id),
    expires_at timestamptz NOT NULL, used_at timestamptz
);
CREATE TABLE remote_mcp_tokens (
    hash text PRIMARY KEY, grant_id bigint NOT NULL REFERENCES remote_mcp_grants(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK(kind IN ('access','refresh')),
    expires_at timestamptz NOT NULL, used_at timestamptz
);
CREATE INDEX remote_mcp_tokens_grant ON remote_mcp_tokens(grant_id);
CREATE TABLE remote_mcp_limits (
    key text NOT NULL, bucket bigint NOT NULL, n int NOT NULL,
    PRIMARY KEY(key,bucket)
);

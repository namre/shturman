-- Read and disclosure rights are separate. Neither table stores connector credentials.
CREATE TABLE source_grants (
    id bigserial PRIMARY KEY,
    task_id bigint,
    target_chat_id bigint NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    target_topic_tg_id bigint,
    kind text NOT NULL CHECK (kind IN ('chat', 'memory', 'external')),
    source_id text,
    request jsonb NOT NULL,
    mode text NOT NULL CHECK (mode IN ('read', 'disclose')),
    owner_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX source_grants_active ON source_grants(task_id, target_chat_id) WHERE revoked_at IS NULL;

-- Opaque receipts bind each returned fragment to the exact grant, request and revision.
CREATE TABLE source_reads (
    id bigserial PRIMARY KEY,
    task_id bigint NOT NULL,
    grant_id bigint REFERENCES source_grants(id),
    request jsonb NOT NULL,
    source_ref jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX source_reads_task ON source_reads(task_id);

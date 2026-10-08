-- Durable, bounded reply workflow. Model output never creates a grant or owner receipt.
CREATE TABLE reply_tasks (
    id bigserial PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    chat_id bigint NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    target_peer_id bigint NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    target_tg_id bigint NOT NULL,
    trigger_message_id bigint NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    topic_tg_id bigint,
    trigger_hash text NOT NULL,
    policy_revision text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued','generating','waiting_source','waiting_owner','drafting','draft_ready',
         'declined','cancelled','expired','failed')),
    generation integer NOT NULL DEFAULT 0,
    job_id bigint REFERENCES jobs(id) ON DELETE SET NULL,
    draft_id bigint,
    input_messages jsonb NOT NULL DEFAULT '[]',
    source_refs jsonb NOT NULL DEFAULT '[]',
    source_request jsonb,
    owner_question text,
    owner_answer text,
    owner_decided_by bigint,
    owner_decided_at timestamptz,
    requires_approval boolean NOT NULL DEFAULT false,
    prepare_only boolean NOT NULL DEFAULT false,
    nonce text NOT NULL,
    decision_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '30 minutes',
    error_code text,
    UNIQUE(trigger_message_id)
);
CREATE INDEX reply_tasks_waiting ON reply_tasks(status, expires_at);

ALTER TABLE source_grants ADD CONSTRAINT source_grants_task_fk FOREIGN KEY (task_id) REFERENCES reply_tasks(id) ON DELETE CASCADE;
ALTER TABLE source_reads ADD CONSTRAINT source_reads_task_fk FOREIGN KEY (task_id) REFERENCES reply_tasks(id) ON DELETE CASCADE;

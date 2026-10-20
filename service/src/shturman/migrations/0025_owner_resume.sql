-- Fresh independent owner attention extends an existing task, never its immutable scope.
ALTER TABLE reply_tasks ADD COLUMN owner_resume_at timestamptz;
ALTER TABLE reply_tasks ADD COLUMN owner_resume_by bigint;
ALTER TABLE reply_tasks ADD COLUMN owner_resume_via text CHECK (owner_resume_via IS NULL OR owner_resume_via='telegram');

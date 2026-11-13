CREATE TABLE desktop_browser_task_dispatches (
    task_id TEXT PRIMARY KEY REFERENCES desktop_browser_tasks(task_id) ON DELETE CASCADE,
    admission_generation TEXT NOT NULL,
    pairing_id TEXT NOT NULL CHECK (length(pairing_id) BETWEEN 1 AND 128)
);

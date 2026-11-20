-- One existing native browser task per explicitly claimed protected attempt.
-- The task's one-use entry is not a general browser helper admission.
CREATE TABLE protected_recovery_browser_dispatches (
    task_id TEXT PRIMARY KEY REFERENCES desktop_browser_tasks(task_id) ON DELETE CASCADE,
    recovery_id TEXT NOT NULL REFERENCES protected_recovery_desktop_admissions(recovery_id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES protected_recovery_authentication_children(attempt_id)
);
CREATE TRIGGER protected_recovery_browser_dispatch_immutable BEFORE UPDATE ON protected_recovery_browser_dispatches
BEGIN
    SELECT RAISE(ABORT,'Protected browser dispatch identity is immutable');
END;

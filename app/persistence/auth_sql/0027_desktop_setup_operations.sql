-- Presentation receipts never substitute for the domain owners' authority.
CREATE TABLE desktop_setup_journeys (
    journey_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at REAL NOT NULL,
    source_journey_id TEXT
);

CREATE TABLE desktop_setup_operations (
    operation_id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES desktop_setup_journeys(journey_id),
    action TEXT NOT NULL CHECK (action IN (
        'browser.access', 'account.connect', 'account.confirm', 'setup.finalize', 'browser.pair', 'activation.review', 'activation.confirm',
        'auth.select', 'auth.cancel', 'auth.switch'
    )),
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    generation TEXT NOT NULL,
    scope_digest TEXT NOT NULL CHECK (length(scope_digest) = 64),
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    owner_scope_json TEXT NOT NULL CHECK (length(owner_scope_json) <= 8192),
    state TEXT NOT NULL CHECK (state IN ('accepted', 'waiting', 'outcome_unknown', 'completed', 'refused')),
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    CHECK (expires_at > created_at)
);
CREATE UNIQUE INDEX desktop_setup_one_unresolved_action ON desktop_setup_operations(journey_id, action)
    WHERE state IN ('accepted', 'waiting', 'outcome_unknown');

CREATE TRIGGER desktop_setup_operation_scope_immutable BEFORE UPDATE ON desktop_setup_operations
WHEN OLD.operation_id <> NEW.operation_id OR OLD.journey_id <> NEW.journey_id
  OR OLD.action <> NEW.action OR OLD.request_digest <> NEW.request_digest
  OR OLD.generation <> NEW.generation OR OLD.scope_digest <> NEW.scope_digest
  OR OLD.owner_scope_json <> NEW.owner_scope_json
  OR OLD.source_revision <> NEW.source_revision
  OR OLD.created_at <> NEW.created_at OR OLD.expires_at <> NEW.expires_at
  OR (OLD.state IN ('completed', 'refused') AND OLD.state <> NEW.state)
BEGIN
    SELECT RAISE(ABORT, 'Desktop operation scope is immutable');
END;

-- A nonauthorizing display snapshot of an existing Provider decision. The
-- explicit confirmation command captures its exact reference/digest in scope.
CREATE TABLE desktop_decision_projections (
    operation_id TEXT PRIMARY KEY REFERENCES desktop_setup_operations(operation_id) ON DELETE CASCADE,
    projection_json TEXT NOT NULL CHECK (length(projection_json) <= 16384),
    provider_revision INTEGER
);

-- Child authentication commands retain only closed selection and correlation.
CREATE TABLE desktop_authentication_commands (
    operation_id TEXT PRIMARY KEY REFERENCES desktop_setup_operations(operation_id) ON DELETE CASCADE,
    parent_operation_id TEXT NOT NULL REFERENCES desktop_setup_operations(operation_id) ON DELETE CASCADE,
    verb TEXT NOT NULL CHECK (verb IN ('select','cancel','switch')),
    request_json TEXT NOT NULL CHECK (length(request_json) <= 8192)
);
CREATE TRIGGER desktop_authentication_command_immutable BEFORE UPDATE ON desktop_authentication_commands
BEGIN
    SELECT RAISE(ABORT, 'Authentication command is immutable');
END;
CREATE TABLE desktop_authentication_projections (
    operation_id TEXT PRIMARY KEY REFERENCES desktop_setup_operations(operation_id) ON DELETE CASCADE,
    projection_json TEXT NOT NULL CHECK (length(projection_json) <= 8192),
    revision INTEGER NOT NULL CHECK (revision > 0)
);

-- A commercial action has its own source context and operation. It cannot
-- replace the desktop's saved setup journey or renew an uncertain ceremony.
CREATE TABLE desktop_activation_sources (
    operation_id TEXT PRIMARY KEY REFERENCES desktop_setup_operations(operation_id) ON DELETE CASCADE,
    source_journey_id TEXT NOT NULL,
    activation_operation_id TEXT NOT NULL UNIQUE
);
CREATE TRIGGER desktop_activation_source_immutable BEFORE UPDATE ON desktop_activation_sources
BEGIN
    SELECT RAISE(ABORT, 'Activation source is immutable');
END;

CREATE TABLE desktop_browser_observations (
    task_id TEXT PRIMARY KEY REFERENCES desktop_browser_tasks(task_id) ON DELETE CASCADE,
    admission_generation TEXT NOT NULL,
    epoch TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    observation_json TEXT NOT NULL CHECK (length(observation_json) <= 8192)
);

CREATE TABLE desktop_browser_ceremonies (
    operation_id TEXT PRIMARY KEY REFERENCES desktop_setup_operations(operation_id) ON DELETE CASCADE,
    hosted_operation_id TEXT NOT NULL UNIQUE,
    binding_json TEXT NOT NULL CHECK (length(binding_json) <= 8192),
    ceremony_id TEXT UNIQUE,
    provider_expires_at TEXT,
    entry_reference TEXT,
    presentation_state TEXT CHECK (presentation_state IN ('active','retired','expired')),
    domain_status TEXT,
    provider_revision INTEGER,
    CHECK ((ceremony_id IS NULL) = (provider_expires_at IS NULL))
);
CREATE TRIGGER desktop_browser_ceremony_scope_immutable BEFORE UPDATE ON desktop_browser_ceremonies
WHEN OLD.operation_id <> NEW.operation_id OR OLD.hosted_operation_id <> NEW.hosted_operation_id
  OR OLD.binding_json <> NEW.binding_json
  OR (OLD.ceremony_id IS NOT NULL AND (NEW.ceremony_id IS NULL OR OLD.ceremony_id <> NEW.ceremony_id))
  OR (OLD.provider_expires_at IS NOT NULL AND (NEW.provider_expires_at IS NULL OR OLD.provider_expires_at <> NEW.provider_expires_at))
  OR (OLD.provider_revision IS NOT NULL AND (NEW.provider_revision IS NULL OR NEW.provider_revision < OLD.provider_revision))
BEGIN
    SELECT RAISE(ABORT, 'Browser ceremony scope is immutable');
END;

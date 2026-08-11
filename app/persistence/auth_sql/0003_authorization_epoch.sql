CREATE TABLE authorization_epoch (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    value INTEGER NOT NULL CHECK (value >= 0)
);

INSERT INTO authorization_epoch(singleton, value) VALUES (1, 0);

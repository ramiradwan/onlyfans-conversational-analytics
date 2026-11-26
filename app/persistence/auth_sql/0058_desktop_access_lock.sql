CREATE TABLE desktop_access_lock (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    locked INTEGER NOT NULL CHECK (locked IN (0, 1))
);

INSERT INTO desktop_access_lock VALUES (1, 0, 0);

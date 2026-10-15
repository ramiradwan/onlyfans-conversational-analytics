# Bootstrap status

This private inherited pipe reports startup observations from the owned Brain to its native parent. It contains no account, installation key, token or conversation data. A ready frame permits the parent to check the listener; PID, image, SID, origin and the existing entry handshake still govern admission.

Frames are UTF-8 JSON followed by LF, with exactly `schema`, `seq`, `phase` and `reason`. Sequences start at 1 and increase by one. Phases advance in the order in `schema.json`; repeated phases report bounded liveness. Ready and recovery are terminal. Only recovery has a non-null, closed reason.

Limits are 512 bytes per frame, 256 frames and 65,536 total bytes. Heartbeats are throttled to five seconds. Native supervision permits 30 seconds for initial bootstrap, 300 seconds each for checking, checkpointing and validation, 600 seconds for migration, and 30 seconds after ASGI startup completes for a verified listener. The entire startup has a 900-second ceiling. Fifteen seconds without a complete frame after bootstrap starts refuses a hung or unavailable child. Heartbeats do not extend phase or absolute deadlines.

These bounds are implementation limits. A realistic large-data frozen-artifact test remains required to qualify them. The source and native owners are `app/core/bootstrap_status.py` and `desktop/src-tauri/src/bootstrap.rs`.

After ready, the same owned pipe may carry one separate [runtime shutdown observation](shutdown.schema.json), matching [shutdown.ndjson](shutdown.ndjson). Its `after_seq` binds it to the admitted startup; `drained` is true only after ASGI shutdown completes and all application shutdown owners report success. It cannot advance startup or authorize a listener. Missing, false, partial, duplicate or malformed observations refuse servicing. Native servicing also requires exit code zero and an empty owned Job Object; a deadline or explicit Quit cannot authorize relaunch. Ordinary Quit may close the reader without consuming this optional observation.

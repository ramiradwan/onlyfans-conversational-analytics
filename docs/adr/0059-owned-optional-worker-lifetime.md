<!-- CODE-VERIFY: app/nlp/child_protocol.py, app/nlp/child_client.py, app/services/nlp_dependencies.py, desktop/src-tauri/src/worker_host.rs and native/nlp-worker/. Installed execution requires separate qualification. -->

# ADR 0059: Own optional worker execution through the installed launcher

- Status: Accepted boundary; installed execution requires separate qualification.
- Date: 2026-10-23
- Extends: ADRs 0057 and 0058.

## Decision

The installed Core launcher has a private child-host mode entered before application activation. Brain starts it through explicitly inherited pipes and reduced-right process and Job handles. The host verifies the installed Core identity, the live Brain process, user and session, and membership in the exact outer Job. Invalid private invocations exit without entering the user interface. The cached servicing helper is not an execution host.

The host admits only the approved signed optional Engine related to that Core installation. It retains protected package ancestors, executable and runtime files, and checks their declared sizes and hashes. It starts the worker suspended, assigns a separate kill-on-close Job, verifies containment, then resumes it. No executable or DLL path comes from model data, an environment override or a request. Core contains no inference runtime. Full-trust containment is fault isolation, not a security sandbox.

Brain and the worker exchange bounded frames through direct inherited pipes. A separate owner bootstrap binds the canonical admitted profile, observed Engine and adapter identities, and logical artifacts to retained read-only handles. Bootstrap grants no authorization or publication authority. Transport envelopes version operation dispatch separately from the unchanged worker v2 payloads. Frames, including bootstrap, are limited to one MiB; larger descriptors are explicitly refused. Malformed, duplicate, unexpected or truncated messages fail the channel without replay.

Each warmup or analysis operation retains its data closure and process owner until completion. Components prepare and unload serially. The initial provider keeps no idle model residency. A preparation observation is distinct from a live session; execution reopens and verifies dependencies. A request's monotonic deadline includes preparation, switching and I/O. Cancellation acknowledgment does not establish analysis completion, unload or process drain.

Shutdown closes admission, cancels active operations, and waits for their owners. The host monitors Brain lifetime and joins the worker and its empty Job before reporting drain. A timeout may terminate only the owned worker tree; forced termination never becomes a successful analysis or graceful unload. Brain's shutdown receipt includes synchronous preparation, inference, pipe readers and host processes. Data pins and handles remain retained until those readers are gone.

Inherited query handles keep the outer Job object alive. Closing the launcher's original handle alone is therefore not a kernel termination proof. On loss of its parent pipe, Brain requests normal shutdown and forces its own exit after thirty seconds if shutdown does not finish. The same bound remains armed when server shutdown returns without a proved drain. The host independently observes Brain and its lifetime pipe. Normal Quit and servicing retain explicit Job termination and join checks.

## Consequences

Optional package admission, data authenticity, resource policy, analysis authorization and result validation remain separate checks. The default service has no Engine provider or approved profile. This boundary changes neither resource limits nor task availability. Installed startup, cancellation, parent loss and servicing require actual Windows qualification before activation.

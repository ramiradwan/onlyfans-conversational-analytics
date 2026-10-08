<!-- CODE-VERIFY: Check graph_membership_pages.py, shared_graph.py, incremental_graph.py, validation_receipt.py, sql/0020_shared_graph_membership_pages.sql and test_shared_membership_pages.py before changing membership reuse, validation or recovery claims. -->

# ADR 0042: Share immutable graph membership pages

- Status: accepted

## Decision

Each graph segment selects membership pages instead of owning every membership row. Pages divide each existing identity-prefix bucket by one additional hexadecimal digit. A page belongs to one account, record kind and prefix. New page keys group related rows by their creating segment. Its rows select exact record and content identities.

The writer compares the complete ordered membership of a predecessor page with the required membership. It reuses the page only when every pair matches. Otherwise it creates a new page under the current building generation. Existing segment identities, canonical chunks, graph ordering and public graph digests do not change.

Membership insertion checks start at the exact page and follow its segment to the building generation. They do not scan unrelated segments.

Pages are sealed when their segment is sealed. Sealed pages and their membership rows cannot change. Segment selections are immutable after sealing. A page cannot be deleted while any segment selects it, including when foreign-key enforcement is disabled.

Changed segments still validate their complete persisted graph rows and required endpoint closure. Page reuse does not create a validation exemption. Schema-20 receipts require the complete reviewed trigger catalog as well as content tracking. Missing or altered guards prevent proof reuse.

## Storage and recovery

Schema 20 converts existing membership rows into pages in the locked migration transaction. The logical `graph_segment_nodes` and `graph_segment_edges` names become read-only views. The public graph views keep their record shapes. The migration backup retains the previous database layout.

Removing a segment removes its page selections. Removing the last selection reclaims the page and any content no other page or edge requires. Cleanup remains synchronous and transactional. Page references do not form chains between generations.

## Tradeoffs

The layout adds a join to membership reads and a small page manifest to each segment. Segment reads start from the selected page manifest. Endpoint lookups route directly to the page for the node identity. Public point lookups select the identity bucket before reading its pages. It avoids copying unchanged memberships when only part of a bucket changes. Full cold construction still creates the complete membership set. Page size, cold construction, query cost and migration work require measurement; row-count savings alone do not establish lower update latency.

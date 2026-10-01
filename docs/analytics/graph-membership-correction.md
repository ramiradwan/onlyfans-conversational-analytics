<!-- CODE-VERIFY: Check conversation_graph_insertion.py, conversation_sql.py, conversation_id_frames.py, shared_graph.py, graph_membership_selection.py, conversation_integrity_store.py and test_graph_membership_transition.py. -->
# Graph-membership correction

This implements the measured graph-transition plan without changing the graph schema, qualification protocol, retention window, proof lifetime, cache limits or publication authority.

## Construction

The conversation loader can authorize membership-group reuse only for the actual stored unit it just read under its checked predecessor header. A copied unit cannot acquire that authority. The helper rehashes the actual membership bytes against their existing group and unit digests before using the previously proved ID ordering.

Insertion reads content versions from the existing verified-chunk reader when the conversation is large enough to justify reading those chunks. Small conversations retain selective SQL, avoiding account-bucket read amplification. A group whose old and new suffix records are identical still checks the old suffix's membership and actual content, but does not rebuild the whole unchanged group. All original restrictions on node removal, the one permitted boundary-edge removal, unrelated replacements and final membership digests remain.

Loader-local reuse is invalid after the reader closes. Its retained unit and chunk references are released with that scope.

## Independent stored validation

Changed-segment validation now merges the actual predecessor membership, candidate membership and candidate chunk in one ordered pass. It still checks both digests and counts, strict key order, exact changed and removed sets, actual changed payload rows, categories and endpoints. The predecessor and candidate iterators must be exhausted before returning a proof. Cancellation or incomplete input cannot produce a successful result.

Conversation-integrity validation still hashes all actual candidate membership bytes. A group with the same membership hash and count as its checked predecessor may reuse only the proved ordering result. It still checks canonical framing and group boundaries. Content-version verification remains separate: identical IDs never establish identical payloads.

## Transaction-local sharing

Before graph validation, bounded streaming over the actual candidate units selects the identities that conversation-integrity validation will need. These are untrusted hints, not validation results. The graph validator fills them only from independently checked membership/chunk data and enables reuse only after complete success.

Reuse is bound to the same connection, open transaction, write counter, generation identity, account, graph-validation object and content stamp. Missing coverage releases the optional buffer before the ordinary SQL fallback. Exceeded capacity, malformed framing or missing proof also retain the existing independent fallback.

The buffer retains at most the existing 32,768 identities and reserves memory within the existing 32 MiB bound. It does not retain all 46,875 records of the measured changed segments. No buffer survives the verification call: all return and error paths clear it. The caller checks for same-connection writes again after conversation-integrity validation, before any verified result can escape.

## Verification and acceptance

The graph component includes selection preparation in the compared interval. Every sample starts from the same graph input, validates persisted data, recomputes its complete graph-unit oracle and rolls back. Real scheduled tests separately cover canonical equality, stale-reference rejection, cleanup and joined shutdown.

This correction is not itself a visibility qualification pass. Confirm the full-size scheduled cost before the exact unprofiled prefix, and run the prescribed guarded campaign only after the prefix passes. Preserve any failed result and the fixed stopping decision from the correction plan.

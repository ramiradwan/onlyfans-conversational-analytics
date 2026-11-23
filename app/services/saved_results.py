"""Read committed local analysis through its ordinary source and retention checks."""
from app.analytics.factory import create_contribution_reader
from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
from app.persistence.text_analysis_preview import TextAnalysisPreviewStore
from app.security.runtime_policy import AuthContext, IdentityAccountAuthority, RuntimePolicy


def saved_result_page(database, owner, after=""):
    with database.transaction(immediate=False) as db:
        rows = db.execute("""
            SELECT j.contribution_id, CASE WHEN p.job_id IS NULL THEN 'Full' ELSE 'Preview' END AS kind
            FROM knowledge_jobs j
            LEFT JOIN knowledge_contributions f ON f.job_id=j.job_id
            LEFT JOIN text_analysis_preview_outputs p ON p.job_id=j.job_id
            WHERE j.state='committed' AND (f.job_id IS NOT NULL OR p.job_id IS NOT NULL)
              AND j.creator_account_id=? AND j.installation_id=? AND json_extract(j.spec,'$.organization_id')=?
              AND j.contribution_id>? ORDER BY j.contribution_id LIMIT 51
        """, (owner.creator_account_id, owner.installation_id, owner.organization_id, after)).fetchall()
        return {"items": [dict(row) for row in rows[:50]],
                "next": rows[49]["contribution_id"] if len(rows) > 50 else None}


def saved_result_document(database, authentication, auth_db, owner, identifier, kind):
    # The existing readers require an immutable identity scope. It carries zero
    # execution references and exists only inside this verified read operation.
    policy = RuntimePolicy(
        AuthContext(owner.principal_id, owner.creator_account_id, "creator"),
        authentication._authorization_epoch(auth_db),
        identity_authority=IdentityAccountAuthority(owner.organization_id, owner.installation_id,
            owner.installation_key_id, owner.installation_key_jkt, (), ()),
    )
    reader = create_contribution_reader(database, clock=authentication._now)
    if kind == "Full":
        return reader.export_document(identifier, policy=policy)
    preview = TextAnalysisPreviewStore(reader)
    with database.transaction() as db:
        output = preview.read_shown_in(db, identifier, policy=policy)
        preview.validate_saved_reference_in(TextAnalysisAuthorityStore(authentication), auth_db, db, output)
        return preview.saved_view_in(db, output, policy=policy).model_dump(mode="json")

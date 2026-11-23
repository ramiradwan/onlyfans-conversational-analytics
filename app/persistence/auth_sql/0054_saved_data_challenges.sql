-- NULL is the established registration or full-runtime login ceremony.
ALTER TABLE auth_challenges ADD COLUMN saved_data_binding TEXT
    CHECK (saved_data_binding IS NULL OR
           (challenge_kind = 'webauthn' AND credential_id IS NOT NULL
            AND length(saved_data_binding) = 64));

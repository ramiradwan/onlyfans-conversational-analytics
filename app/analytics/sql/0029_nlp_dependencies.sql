-- Selection remains account-owned; rollback is a reference, never authority.
ALTER TABLE nlp_accounts ADD COLUMN previous_profile_json TEXT;
ALTER TABLE nlp_accounts ADD COLUMN dependency_intent TEXT;

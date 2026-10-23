CREATE INDEX nlp_sources_scan_lane
ON nlp_sources(account_ref,seen_epoch,lane DESC,source_ref);

<!-- CODE-VERIFY: Verify command options, canonical-database validation, and output security behavior against app/analytics/rebuild.py before editing. -->

# Rebuild analytics

Use the rebuild command to generate a derived analytics artifact from an existing canonical SQLite database.

The command reads the canonical database without modifying it. It validates the database schema and integrity before rebuilding analytics.

## Run the rebuild

From the repository root:

```powershell
python -m app.analytics.rebuild `
  --canonical-path path/to/canonical.sqlite3 `
  --account-id creator-account-id `
  --output analytics-projection.json
```

Omit `--output` to write the JSON result to standard output.

The source database must already exist. Linked or aliased source paths that cannot be validated safely are rejected.

When writing a file, the command publishes it by atomic replacement and requires private file permissions. The output is derived data and can be discarded and rebuilt from canonical state.

See [Analytics](README.md) for the pipeline overview.

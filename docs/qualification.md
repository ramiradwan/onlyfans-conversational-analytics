<!-- CODE-VERIFY: Verify snapshot qualification command names, message counts, and CI coverage against extension/package.json, the qualification runner, and GitHub Actions before editing. Manual Beta policy gates are not automatically enforced by CI. -->

# Product qualification

Use these checks in addition to ordinary CI when qualifying a Beta release.

## Snapshot qualification

CI runs the 10,000-message Agent snapshot qualification:

```powershell
npm run qualify:snapshot:ci --prefix extension
```

Before a Beta declaration, run the 100,000-message qualification:

```powershell
npm run qualify:snapshot --prefix extension
```

The qualification runner defaults to 100,000 messages. The CI script invokes the same runner with `--messages=10000`.

## Live pagination evidence

Beta qualification also requires one explicitly consented, sanitized live read-only pagination run.

The 100,000-message run and the live pagination evidence are manual release gates. The ordinary CI workflow runs only the 10,000-message qualification and does not automate the live pagination requirement.

Deterministic tests and snapshot qualification alone do not authorize a Beta declaration.

For ordinary change testing, see [Test changes](testing.md). For installed Windows smoke acceptance, see [Run Windows acceptance](installation-and-acceptance.md).

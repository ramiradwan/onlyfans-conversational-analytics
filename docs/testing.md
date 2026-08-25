<!-- CODE-VERIFY: Verify local commands, toolchain versions, CI jobs, qualification commands, and workflow links against package scripts and GitHub Actions before editing. -->

# Test changes

Run the checks that cover the code you changed. CI is the final reference for the full test matrix.

## Common local checks

From the repository root:

```powershell
python -m pytest
npm test --prefix frontend
npm test --prefix extension
npm run build --prefix frontend
npm run build --prefix extension
npm run audit --prefix extension
```

Install backend development dependencies from `requirements-dev.txt` and JavaScript dependencies with `npm ci` in the relevant package before running these commands.

## CI coverage

GitHub Actions uses Python 3.11 and Node.js 22. In addition to the common checks, CI runs contract-integrity tests, the provisioning-page module test, the 10,000-message Agent snapshot qualification, the backend suite on Windows, and capture end-to-end tests.

See [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) for the current commands and job matrix.

Packaging, installed-artifact acceptance, and Beta qualification have separate checks. See [Product qualification](qualification.md) and [Windows acceptance](installation-and-acceptance.md).

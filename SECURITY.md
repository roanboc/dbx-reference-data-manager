# Security policy

## Supported versions

The `main` branch and the latest release are supported. Deploy from a tagged release or from
`main` after CI has passed.

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private vulnerability
reporting on this repository (**Security** tab > **Report a vulnerability**), or contact the
repository owner directly. Include the version or commit, the steps to reproduce and the
impact you expect. You will get an acknowledgement within a few working days.

## Design notes for reviewers

* The app never holds broader rights than the signed-in user: every SQL statement and file
  operation runs on behalf of the user (Databricks Apps user authorization), so Unity Catalog
  is the enforcement point and the app's role checks only decide what to render.
* Every identifier that reaches SQL is validated and quoted, values are bound as parameters,
  and the Databricks backend refuses any statement or volume path outside the configured
  catalog. Deletes never cascade and require a typed confirmation.
* Secrets are never stored in the repository: the workspace injects the warehouse binding and
  the user token; local development uses an untracked `.env`.

See [docs/DESIGN.md](docs/DESIGN.md) §5 and §11 for the full authorisation model and the
security review of deletions and catalog confinement.

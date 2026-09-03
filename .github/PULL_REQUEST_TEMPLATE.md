## What

<!-- One or two sentences: what changes and why. Link the issue if there is one. -->

## How to verify

<!-- Steps a reviewer can follow locally (make seed / make run) or in a dev workspace. -->

## Checklist

- [ ] `make check` passes (ruff + pytest)
- [ ] Behaviour that users see is reflected in the in-app help (`src/rdm/ui/help/`) and, when it changes the design, in `docs/`
- [ ] New settings are added to `src/rdm/config.py`, `.env.example`, `app.yaml` and `resources/app.yml` together
- [ ] Databricks-only behaviour is covered by the SQL-generation tests (`tests/test_databricks_backend.py`)

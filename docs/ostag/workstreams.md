# Governed Workstreams

Workstreams group several OS Tag runs under one immutable recipe and goal. They
are disabled by default and retain their recipe snapshot, digest, run count,
cost budget, required evidence, and connector operation allowlist at admission.

## Enable

```bash
OSTAG_WORKSTREAMS_ENABLED=true
OSTAG_WORKSTREAM_STORE_PATH=./logs/workstreams.sqlite3
```

## Register And Admit

Recipe files use the strict `RecipeDefinition` JSON schema. Registering the same
id/version and digest is idempotent; changing an existing version is rejected.

```bash
uv run ostag workstream recipe-register --file recipe.json --json
uv run ostag workstream admit \
  --recipe software-delivery --version 1 \
  --key feishu:oc_example:goal_1 --goal "Ship governed change" --json
```

Attach existing ledger runs and inspect the frozen workstream:

```bash
uv run ostag workstream attach-run --id workstream_example --run run_example --cost 10 --json
uv run ostag workstream show --id workstream_example --json
uv run ostag workstream list --json
```

Completion is a projection, not a writable flag. Every attached child run must
succeed and the union of their evidence must satisfy the recipe. Failed child
runs block completion. Undeclared connector operations fail closed.

## Example Recipe

```json
{
  "recipeId": "software-delivery",
  "version": 1,
  "title": "Software delivery",
  "steps": [
    {"id": "build", "title": "Build"},
    {"id": "verify", "title": "Verify", "dependsOn": ["build"]}
  ],
  "maxRuns": 3,
  "maxCostCredits": 100,
  "requiredEvidence": ["tests", "provider_merge"],
  "allowedOperations": ["github.read_pr", "github.read_checks"]
}
```

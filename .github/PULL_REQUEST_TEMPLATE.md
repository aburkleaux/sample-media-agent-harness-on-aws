# Description

<!-- What does this change do and why? Link any related issue. -->

## Type of change
- [ ] Bug fix
- [ ] New feature
- [ ] Refactor (no behavior change)
- [ ] Docs / infra / CI

## Validation (all must pass locally before review)
- [ ] `cd agent && uv run --extra dev pytest -q`
- [ ] `cd ui && npx vitest run && npx tsc --noEmit && npm run build`
- [ ] `cd infra && npx cdk synth -c env=staging --quiet`
- [ ] For a bug fix: added a **regression test** that fails without the fix
- [ ] For a refactor: change is a pure move/rename with **no behavior change**

## Notes for reviewers
<!-- Anything that helps review: risk areas, follow-ups, screenshots. -->

By submitting this pull request, I confirm that my contribution is made under
the terms of the project's [LICENSE](../LICENSE).

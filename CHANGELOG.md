# Changelog

## 0.2.0

- Add optional coherent donor profiles and persist the selected profile.
- Reject unknown, mixed, ambiguous, or changed donor profiles.
- Add optional `requires_eapx` constraints in `>=MAJOR.MINOR.PATCH` form.
- Preserve marker and verification behavior for profileless 0.1 recipes.
- Make `plan` inspect APKs nested inside bundles, matching `install`.
- Add public recipe schema, synthetic example, PortMaster/offline-consumption
  documentation, external smoke runner, multi-version CI, and tagged releases.
- Fix the direct test runner so it executes the same complete suite as unittest
  discovery.

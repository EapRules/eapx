# Changelog

## Unreleased

- Brand the optional TTY progress screen as EAPX, “Android Port eXtractor”,
  with a `BY EAPRULES` footer.
- Keep logs and the PortMaster patcher protocol unchanged, and fall back to an
  ASCII-only presentation when the terminal cannot encode the block artwork.

## 0.3.0

- Add generic `critical_regions` validation for compatible donor variants.
- Keep known full-file SHA-256 values as the fast path and fall back to the
  SHA-256 of recipe-ordered byte ranges only for unknown full hashes.
- Require an exact file size and validate hexadecimal/decimal offsets, positive
  region sizes, bounds, and digests while loading the recipe.
- Log the actual full SHA-256 whenever critical regions accept a donor.
- Preserve the exact validation behavior of recipes without the new field.

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

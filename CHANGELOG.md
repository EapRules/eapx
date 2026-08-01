# Changelog

## 0.4.1

- Update the TTY frame in place instead of clearing it for every progress
  change, eliminating visible flicker.
- Keep the frame within the terminal's exact row count and omit the trailing
  newline that scrolled and damaged the footer's last row.
- Use the corrected `BY EAPRULES` artwork and show the current engine version
  directly from the `VERSION` constant.

## 0.4.0

- Brand the optional TTY progress screen as EAPX, “Android Port eXtractor”,
  with a `BY EAPRULES` footer.
- Keep logs and the PortMaster patcher protocol unchanged, and fall back to an
  ASCII-only presentation when the terminal cannot encode the block artwork.
- Document candidate roots, repeated explicit inputs, immediate-child
  discovery, APK/data-directory composition, and raw Android backup layouts.
- Clarify that optional extraction rules still validate non-empty partial
  matches before results from different rules are merged.
- Add regression coverage for multi-source aggregation and the recommended
  top-level validation pattern for output trees assembled by several rules.

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

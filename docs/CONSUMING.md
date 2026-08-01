# Consuming eapx from a port

The distribution contract is an exact offline copy:

```text
EAPX_VERSION=0.4.0
EAPX_SHA256=1af59ef2250c6e42a1bb32c380dc09ea3a1f7ea965288ac4c59d54cc9664070c
```

For a port release:

1. Download `eapx.py` and `SHA256SUMS` from the matching tagged eapx release
   during port development.
2. Verify the checksum.
3. Commit that exact `eapx.py` into the port package together with its recipe.
4. Invoke the local copy during first boot.

Do not download eapx at runtime. Do not add pip dependencies or a Git
submodule. This keeps installations usable offline and makes every port
artifact auditable and reproducible.

The engine version, schema version, and recipe version are separate. A recipe
that needs profiles or another 0.2 feature should declare:

```json
"requires_eapx": ">=0.2.0"
```

Recipes that declare `critical_regions` require the newer engine:

```json
"requires_eapx": ">=0.3.0"
```

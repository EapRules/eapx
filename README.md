# eapx

`eapx` is a dependency-free, first-boot importer for Android-to-Linux
PortMaster ports. A port supplies a declarative JSON recipe; the user supplies
their own Android package or extracted donor tree. The engine discovers inputs
by content, validates them, stages a complete result, and publishes it
transactionally.

The project intentionally remains one self-contained [`eapx.py`](eapx.py)
compatible with Python 3.7.5 and newer. It is not a universal installer and it
does not download proprietary content.

## Commands

```text
python3 eapx.py check  --recipe port.eapx.json
python3 eapx.py plan   --recipe port.eapx.json --game-dir ./port-data
python3 eapx.py install --recipe port.eapx.json --game-dir ./port-data
python3 eapx.py verify --recipe port.eapx.json --game-dir ./port-data
python3 eapx.py --version
```

`install` supports APKs, zip-based bundles, loose splits, blobs, and already
unpacked directories. PortMaster progress integration is autodetected and can
be disabled with `--no-portmaster`.

## Recipes

Recipes use schema version `1`, independently from the engine version and the
recipe's own version. Version 0.2.0 added two optional fields:

- `requires_eapx`, currently restricted to `>=MAJOR.MINOR.PATCH`.
- `profiles`, coherent groups of output checks used to classify a donor and
  reject unknown, mixed, or ambiguous payloads.

Version 0.3.0 adds optional `critical_regions` validation for single files.
A known full-file SHA-256 remains the fast path; an unknown full hash may fall
back to a recipe-declared digest of the exact byte ranges a port depends on.

Recipes without these fields retain the 0.1.0 behavior. See
[`docs/RECIPES.md`](docs/RECIPES.md), the
[`recipe.schema.json`](recipe.schema.json), and the synthetic
[`examples/synthetic.eapx.json`](examples/synthetic.eapx.json).

Donors may be composed from more than one input, such as a primary APK plus a
separately copied Android data directory. Input roots, grouping, repeated
`--input`, directory traversal, and per-rule validation are documented in
[`docs/DONORS.md`](docs/DONORS.md). Recipe authors should read that guide
before adding layouts for multi-source backups.

## Offline consumption by ports

Ports should vendor an exact copy; runtime downloads, pip, and submodules are
not part of the contract:

```text
EAPX_VERSION=0.3.0
EAPX_SHA256=2b029be88b23c30aba14e25c6379f67af09f6c73f4f816e163c78d60eaaee33e
```

Details are in [`docs/CONSUMING.md`](docs/CONSUMING.md). Architectural and
transactional rationale lives in [`DESIGN.md`](DESIGN.md).

## Development

```text
python3 -m py_compile eapx.py test_eapx.py
python3 test_eapx.py
python3 -m unittest discover
python3 eapx.py check --recipe examples/synthetic.eapx.json
```

The two test commands intentionally execute the same complete suite.

External compatibility tests use [`tools/smoke_external.py`](tools/smoke_external.py).
It copies every donor and the recipe into a temporary directory, runs `plan`, a
clean install, marker inspection, `verify`, and a donor-free second install,
then confirms the source paths did not change:

```text
python3 tools/smoke_external.py \
  --recipe /read-only/port/recipe.json \
  --donor /read-only/donor.zip \
  --expected-profile reference
```

## License

Copyright (C) EapRules. Licensed under GPL-3.0-only; see [`LICENSE`](LICENSE).

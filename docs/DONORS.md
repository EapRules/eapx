# Donor discovery and composition

An eapx donor is a set of input candidates, not necessarily one file. A port
may need an APK for its native library and base assets, plus files copied from
an Android data directory. eapx groups compatible candidates and lets recipe
rules build one output plan from all of them.

This guide defines the input-root contract. It is intentionally generic: the
engine does not know package names, game paths, or which files a port needs.

## Candidate roots

Without `--input`, eapx scans the immediate children of each
`input.search_dirs` entry. The defaults are `gamedata/` and the root of
`game-dir`.

Given this tree:

```text
<game-dir>/
└── gamedata/
    ├── base.apk
    └── external-data/
        └── assets/
            └── published/
                └── level.dat
```

eapx discovers two candidates:

- `base.apk`, whose recipe-visible paths are its ZIP members, such as
  `lib/armeabi-v7a/libsample.so` and `assets/config.ini`;
- `external-data/`, whose recipe-visible paths are relative to that exact
  directory, such as `assets/published/level.dat`.

Discovery does not recursively turn every nested file or directory into a new
candidate. For example, this layout discovers only `phone-backup/`:

```text
gamedata/
└── phone-backup/
    ├── base.apk
    └── Android/data/org.example.app/published/
```

`base.apk` is then an ordinary member of the directory tree; eapx does not open
it as an APK. Either place the APK and data root as immediate siblings, pass
them separately with `--input`, or normalize the backup in a temporary
directory.

## Explicit inputs

Both `plan` and `install` accept repeated `--input` options:

```text
python3 eapx.py plan \
  --recipe port.eapx.json \
  --game-dir ./port-data \
  --input /backup/base.apk \
  --input /backup/external-data
```

Explicit inputs replace automatic scanning for that invocation. Repeat the
option once per candidate; passing their common parent is not equivalent.

The external smoke runner has the same model and accepts repeated `--donor`:

```text
python3 tools/smoke_external.py \
  --recipe /read-only/port.eapx.json \
  --donor /read-only/base.apk \
  --donor /read-only/external-data
```

It copies every source into a temporary directory before invoking eapx and
checks that the originals were not modified.

## How candidates are grouped

Primary APKs define donor groups. Split APKs with the same readable Android
package name share a group. Non-APK candidates, including data directories,
ZIP data archives, and OBBs, are auxiliary inputs available to each APK group.

If there is no APK, container candidates are evaluated as possible primaries.
Different complete plans remain an ambiguity and are rejected. Names do not
select a winner.

XAPK, APKM, and APKS files are a separate supported case: eapx materializes
their inner APKs into its temporary cache. This does not make APK files nested
arbitrarily inside an input directory recursive candidates.

## Patterns are relative to each candidate

Suppose a raw Android backup contains:

```text
Android/data/org.example.app/published/maps/first.map
```

The visible path depends on which directory is passed:

| Candidate directory | Recipe-visible path |
|---|---|
| backup root | `Android/data/org.example.app/published/maps/first.map` |
| `Android/data/` | `org.example.app/published/maps/first.map` |
| package directory | `published/maps/first.map` |
| `published/` | `maps/first.map` |

The recipe's `patterns` and literal `strip_prefix` must match the selected
root. `strip_prefix` is not a wildcard capture. Package-specific paths belong
in the port recipe, not in the engine.

A convenient normalized auxiliary root is:

```text
external-data/
└── assets/
    └── published/
        └── ...
```

It can contribute to the same `assets/*` rule as an APK because both candidates
expose paths beginning with `assets/`.

## Validation happens before rule results are merged

An `entries` rule first collects all matching entries from every candidate in
the current donor group. Its own `min_files`, `max_files`, `min_bytes`, and
`max_bytes` checks run immediately on that collection.

Results from different extraction rules are merged only after each rule has
passed. Therefore this does not mean “ignore an incomplete match”:

```json
{
  "required": false,
  "source": {"kind": "entries", "patterns": ["assets/*"]},
  "validate": {"min_files": 1000}
}
```

Its behavior is:

- zero matches: allowed because the rule is optional;
- 1000 or more matches: accepted;
- 1 through 999 matches: rejected by `min_files`.

This distinction matters when an APK has a small base asset set and a separate
rule imports the large external tree. The second rule cannot rescue the first
rule from its own validator.

For partial sources, use separate extraction rules without whole-tree minimums
and validate the finished destination at top level:

```json
{
  "extract": [
    {
      "id": "base-assets",
      "required": false,
      "destination": "assets",
      "source": {
        "kind": "entries",
        "patterns": ["assets/base/*"],
        "strip_prefix": "assets/base/"
      }
    },
    {
      "id": "external-assets",
      "required": false,
      "destination": "assets",
      "source": {
        "kind": "entries",
        "patterns": ["published/*"],
        "strip_prefix": "published/"
      }
    }
  ],
  "validate": [
    {"path": "assets", "min_files": 1000, "min_bytes": 254000000}
  ]
}
```

Real recipes should add file hashes, profiles, or other checks that distinguish
supported coherent payloads. The example only demonstrates where completeness
belongs.

Rules may write the same destination tree. Identical duplicate files collapse;
different content mapped to the same destination is rejected deterministically.

## Troubleshooting

### A small APK count fails before external data is considered

Example: `assets-apk: 131 file(s), expected at least 1000`.

The matching rule has a per-rule minimum. Remove the whole-tree minimum from
that partial extraction rule, map the external layout with another rule, and
validate the completed output in top-level `validate`. Alternatively, normalize
the external data so it contributes to the same rule and candidate-relative
prefix as the APK.

### Reorganizing a nested directory changes nothing

Check the exact directory passed as the candidate. Creating
`data/sample/assets/` below a root produces the visible prefix
`data/sample/assets/`; passing `data/sample/` instead produces `assets/`.

### The APK is discovered but a sibling data tree is not

Ensure the data tree itself is an immediate child of a configured search
directory, or pass it with another `--input`. Do not pass only the common parent
containing both sources.

### The plan imports installer chunks or unrelated assets

Patterns are positive selections, and `*` matches `/` in eapx. Replace a broad
pattern such as `assets/*` with the specific subtrees or files the port needs.
Do not rely on filenames being ignored merely because they look like installer
metadata.

### A symlinked subtree appears empty

Directory candidates are walked without following nested directory symlinks.
Copy the subtree into the temporary smoke workspace or pass the symlink's real
target explicitly as its own candidate.

## Recipe-author checklist

1. List each supported donor layout and choose the candidate root for it.
2. Write patterns relative to that root and keep `strip_prefix` literal.
3. Decide which files must validate independently and which checks describe the
   final merged tree.
4. Treat `required: false` as “zero matches allowed,” not “validation optional.”
5. Test automatic immediate-child discovery and repeated explicit `--input`.
6. Test APK-only, external-data-only where meaningful, combined, incomplete,
   conflicting, and mixed donor sets.
7. Run `plan`, clean `install`, marker inspection, `verify`, and donor-free
   fast-path through `tools/smoke_external.py`.

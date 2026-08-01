# Recipe reference

An eapx recipe declares what input content is acceptable and what the finished
Linux-side tree must contain. Paths are relative, use `/`, and may contain the
literal `{abi}` placeholder where documented. Unknown keys are errors.

## Version fields

- `schema`: recipe format version. It is currently the integer `1`.
- `version`: the individual recipe revision, string or integer.
- `requires_eapx`: optional minimum engine version. Version 0.2 accepts only
  `>=MAJOR.MINOR.PATCH`; malformed or unsatisfied constraints fail while the
  recipe is loaded, before input discovery or extraction.

These three values are deliberately independent.

## Main fields

- `id`: stable recipe identifier.
- `title`: optional display title.
- `abi_order`: preferred Android ABIs. The first complete plan wins within one
  donor group.
- `input.search_dirs`: directories below `game-dir` to scan. Defaults to
  `gamedata` and `.`.
- `extract`: one or more extraction rules.
- `hooks`: optional post-processing commands operating on the staged tree.
- `validate`: optional checks on the complete result.
- `profiles`: optional coherent donor fingerprints.
- `commit`: roots atomically published into `game-dir`.
- `marker`, `log`, `placeholder`, and `space.safety_bytes`: optional runtime
  configuration.

The JSON Schema is the editor and CI contract. The engine performs its own
strict standard-library validation and does not import a JSON Schema package.

## Extraction rules

Each rule has `id`, `destination`, `source`, optional `description`, optional
`required` (default `true`), and optional `validate` checks.

`source.kind` is one of:

- `entry`: copy exactly one matching archive/tree member.
- `entries`: copy a matching subtree; `strip_prefix` controls destination
  layout.
- `blob`: copy a whole input file, including a zip-formatted blob.

Patterns use shell-style matching. Input filenames are logging metadata, not
identity; content and rule checks decide whether a donor matches.

Every pattern is relative to the root of one candidate. An APK or ZIP exposes
its archive member names; an input directory exposes paths relative to that
exact directory. eapx can combine entries from several candidates, but it does
not recursively discover an APK stored inside an input directory.

Tree validators on an extraction rule apply to that rule's collected entries
before results from different rules are merged. `required: false` means that
zero matches are allowed. It does not make a non-empty result exempt from the
rule's validators. When several partial rules build one output tree, validate
each source only as strongly as it can stand alone and put completeness checks
in the top-level `validate` block.

See [`DONORS.md`](DONORS.md) for complete multi-source layouts, command-line
examples, and common failure modes.

## Validation

File checks: `size`, `min_size`, `max_size`, `sha256`, `critical_regions`,
`elf_machine`.

Tree checks: `min_files`, `max_files`, `min_bytes`, `max_bytes`.

`sha256` accepts one digest or a list. A list means any listed value is valid
for that individual check. Use `profiles` when values across several files
must correlate.

### Critical regions

`critical_regions` accepts compatible variants of the same file without
enumerating every full-file hash:

```json
{
  "size": 4096,
  "sha256": ["<known-full-file-sha256>"],
  "critical_regions": {
    "regions": [
      {"offset": "0x100", "size": 16},
      {"offset": 1024, "size": 32}
    ],
    "sha256": "<sha256-of-the-concatenated-region-bytes>"
  }
}
```

The exact `size` check is mandatory. eapx rejects a wrong size before hashing
the file. It then accepts a known full-file `sha256` without reading the
regions. For an unknown full hash, it reads each range, concatenates the bytes
in declaration order, and compares that digest with `critical_regions.sha256`.

Offsets are non-negative decimal integers or strings in `0x` hexadecimal
notation. Region sizes are positive byte counts, and every range must fit
inside the declared file size. When fallback succeeds, the log records both
that critical regions accepted the file and its actual complete SHA-256.

The field is optional. Without it, full-file SHA-256 validation behaves exactly
as it did before 0.3.0. Selecting the ranges is the port's responsibility; the
engine attaches no meaning to their contents.

## Profiles

Each profile has a unique `id`, optional `description`, and non-empty
`validate` list. After staging, eapx evaluates every profile:

- exactly one match is recorded as `donor_profile` in the marker;
- zero matches rejects an unknown or mixed donor;
- multiple matches rejects an ambiguous recipe.

On `verify`, the current profile must equal the marker. A marker made by an
older engine without `donor_profile` is revalidated and upgraded through the
normal adoption path when the recipe has sufficient top-level validation.

Omitting `profiles` preserves legacy marker and verification behavior.

## Hooks

Hooks receive literal placeholders in `argv`, `cwd`, and `env`:
`{game_dir}`, `{stage}`, `{workspace}`, `{recipe_dir}`, and `{abi}`. Their
working directory must remain below `game-dir`. Hooks have timeouts and may
declare checkpoints. Game-specific transformations belong here or in recipes,
never in the engine.

## Commit roots

Commit entries are relative paths or objects with `path` and `exclusive`.
Roots cannot overlap. By default eapx refuses to replace a root containing
unplanned files. `exclusive: true` explicitly authorizes complete replacement.

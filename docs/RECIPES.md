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

## Validation

File checks: `size`, `min_size`, `max_size`, `sha256`, `elf_machine`.

Tree checks: `min_files`, `max_files`, `min_bytes`, `max_bytes`.

`sha256` accepts one digest or a list. A list means any listed value is valid
for that individual check. Use `profiles` when values across several files
must correlate.

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

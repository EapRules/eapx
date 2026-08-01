# PortMaster integration

PortMaster support is optional and autodetected. eapx can update a TTY progress
view, speak the patcher protocol when the corresponding environment is present,
and remove a recipe-declared placeholder only after a successful commit.

Useful install flags:

- `--tty none`: disable TTY progress explicitly.
- `--no-portmaster`: disable PortMaster progress integration.
- `--progress-file PATH`: write the compact progress record to a chosen path.
- `--no-adopt`: require donor input instead of adopting an already valid tree.

The engine exposes only generic hook placeholders and environment values. Any
port-specific conversion, filenames, or compatibility knowledge belongs in the
recipe and its hook tools.

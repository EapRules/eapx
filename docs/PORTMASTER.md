# PortMaster integration

PortMaster support is optional and autodetected. eapx can update a TTY progress
view, speak the patcher protocol when the corresponding environment is present,
and remove a recipe-declared placeholder only after a successful commit.

The full-screen TTY view identifies the tool as **EAPX — Android Port
eXtractor**, reads its displayed version directly from the engine's `VERSION`,
shows `recipe.title` as the game being imported, and carries a `BY EAPRULES`
footer. It clears once, then rewrites a fixed-size frame in place without a
trailing newline, so progress updates neither flicker nor scroll the footer.
The artwork is presentation only: it is not written to the log or sent through
the patcher protocol. Terminals without compatible Unicode encoding receive an
ASCII-only fallback.

Useful install flags:

- `--tty none`: disable TTY progress explicitly.
- `--no-portmaster`: disable PortMaster progress integration.
- `--progress-file PATH`: write the compact progress record to a chosen path.
- `--no-adopt`: require donor input instead of adopting an already valid tree.

The engine exposes only generic hook placeholders and environment values. Any
port-specific conversion, filenames, or compatibility knowledge belongs in the
recipe and its hook tools.

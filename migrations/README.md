# Deployment migrations

Numbered modules (`NNNN_slug.py`) that move an *installation* forward: renamed `.env` keys,
moved data directories, one-off fixes. Applied once, in order, by `./neops migrate` (which
`./neops up` and `./neops install` run first). The record lives in `data/.neops/state.json`.

A migration exports `DESCRIPTION`, `IDEMPOTENT` (may a crashed run simply be re-run?) and
`apply(ctx)`. `ctx` offers `repo`, `data`, `env` (`get`/`set`/`rename`/`unset`, comments kept),
`log`, `mkdir`, `move`, `chown_via_container` and `compose(*args)` for the rare migration that
needs a container (say so in the docstring). Before anything is applied, `.env` and the state
file are copied to `backups/pre-migrate-<timestamp>/`, both readable only by their owner.

Module scope must be import-pure. `discover()` imports every migration file on every call,
including the ones already applied, so anything at module scope runs on each `./neops` command
and on a dry run, whether or not the migration is pending. Keep module scope to constants,
imports and function definitions; put all work inside `apply(ctx)`.

`ctx.env.rename(old, new)` raises `MissingEnv` when `old` is absent and `ValueError` when `new`
already exists, so a migration that renames a key must guard it with `if ctx.env.is_set(old):`
to stay re-runnable after a crash part-way through.

Do not edit a migration after it has shipped; add a new one.

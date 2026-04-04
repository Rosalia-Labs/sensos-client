# Command UX

SensOS client commands should follow one interaction model:

- every command must support a complete non-interactive form using flags only
- if required inputs are missing and `stdin` is a TTY, the command should prompt
  for only those missing values
- if required inputs are missing and `stdin` is not a TTY, the command should
  fail with a clear error naming the missing flags
- prompts should only fill in missing values; they should not change the meaning
  of values already supplied by flags or config
- destructive operations should require explicit confirmation
  - interactive use: prompt for confirmation
  - non-interactive use: require an explicit flag such as `--wipe` or `--yes`

Input precedence should be:

1. explicit CLI flags
2. existing local config or discovered state
3. safe defaults
4. interactive prompts for anything still required

Implementation pattern:

1. parse flags
2. discover current state
3. prompt for missing required inputs if `stdin` is interactive
4. validate a single config object
5. execute from that config object

This keeps CLI and interactive use on the same execution path and avoids drift
between "wizard" behavior and scripted behavior.

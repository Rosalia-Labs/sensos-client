# Overlay

This tree contains repo-owned assets that are used in place after cloning the repo onto a target system.

The model is:

- keep the canonical files in `/sensos-client/overlay`
- expose selected commands through symlinks in `/usr/local/bin`
- expose selected unit files through symlinks in `/etc/systemd/system`
- keep supporting libraries and config files in the repo

Directory layout:

- `overlay/bin/` for operator-facing commands such as `config-network`
- `overlay/lib/` for shared code and helper libraries
- `overlay/libexec/` for helper executables used by services
- `overlay/systemd/` for unit files
- `overlay/etc/` for repo-owned config templates and baseline config files

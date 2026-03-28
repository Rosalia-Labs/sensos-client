# Security Checklist

This checklist is for operators bringing up or auditing a SensOS client. It is written as a set of questions to ask before deployment, with suggested mitigations when the answer increases risk.

The goal is practical review, not theoretical completeness.

## How To Use This

Ask each question before field deployment:

- if the answer is `no`, move on
- if the answer is `yes`, read the mitigation and decide whether that risk is acceptable for the deployment

## Identity And Admin Access

### Are `sensos-admin` and `sensos-runner` password logins still usable?

Expected answer:

- usually `no`

Current repo behavior:

- setup locks both accounts with `passwd -l`
- see [setup/02-users](../setup/02-users)

Why it matters:

- unlocked local passwords make brute-force or reused-password compromise more likely

Mitigation if `yes`:

- relock them
- prefer SSH keys over passwords
- verify there is no unexpected PAM or console login path left open

### Is the bootstrap user still present and still usable?

Expected answer:

- maybe

Current repo behavior:

- the install preserves the bootstrap user and leaves it otherwise unchanged

Why it matters:

- the bootstrap user may still have password login, sudo, or stale SSH keys

Mitigation if `yes`:

- decide whether the bootstrap user should remain on field devices
- if not needed, disable or remove it
- if needed, rotate its password, inspect its SSH keys, and verify sudo access is intentional

### Are `authorized_keys` in place for the access you expect?

Expected answer:

- `yes`, intentionally

Current repo behavior:

- setup installs `keys/sensos_admin_authorized_keys` into `~sensos-admin/.ssh/authorized_keys` when present
- `config-network` also generates an SSH keypair and appends the server public key to `sensos-admin` `authorized_keys`

Why it matters:

- missing keys can lock you out if passwords are disabled
- extra keys can leave unreviewed administrative access in place

Mitigation if `no` or `unknown`:

- inspect `~sensos-admin/.ssh/authorized_keys`
- confirm every key owner and purpose
- remove unused keys
- keep at least one tested recovery path before disabling passwords

## SSH Exposure

### Are SSH passwords enabled?

Expected answer:

- ideally `no` for field deployment

Current repo behavior:

- `config-network` writes `PasswordAuthentication no`
- unless `--disable-ssh-passwords` is used, it then enables password auth only from:
  - loopback
  - RFC1918/private IPv4 ranges
  - local IPv6/private-local ranges
  - the WireGuard subnet for the enrolled network

Why it matters:

- even restricted password login increases attack surface if an attacker gains local-network or VPN adjacency

Mitigation if `yes` and that is too permissive:

- rerun `config-network --disable-ssh-passwords ...`
- confirm you have working SSH keys first
- verify `/etc/ssh/sshd_config.d/sensos.conf` matches your intended policy

### Are SSH keys the only planned admin path?

Expected answer:

- usually `yes`

Why it matters:

- mixed modes are often left in place accidentally

Mitigation if `no`:

- document which paths are intentionally enabled
- restrict them to a deployment-specific need and set an expiration/review point

## Access Point And Local Wireless Exposure

### Is the hotspot enabled?

Expected answer:

- usually `no` in normal field deployment

Current repo behavior:

- `config-hotspot` brings up an AP and sets `connection.autoconnect yes`
- it uses a connection named `sensosap`

Why it matters:

- an active AP creates a local radio entry point near the device

Mitigation if `yes`:

- confirm it is truly required for the deployment
- if it was only for setup, disable or delete the AP profile before deployment
- verify there is no active AP with `nmcli connection show --active`

### Is the hotspot using a default or guessable SSID?

Expected answer:

- ideally `no`

Current repo behavior:

- `config-hotspot` defaults the SSID from `NETWORK_NAME` and `CLIENT_WG_IP`

Why it matters:

- predictable SSIDs make devices easier to identify and target

Mitigation if `yes`:

- set a deployment-specific SSID instead of using the default
- avoid names that disclose project, customer, or location details

### Is the hotspot using a default, reused, or weak password?

Expected answer:

- `no`

Current repo behavior:

- `config-hotspot` requires a password, but it does not generate one for you

Why it matters:

- reused setup passwords tend to spread across fleets

Mitigation if `yes`:

- set a unique strong WPA2 passphrase
- avoid shipping all devices with the same AP password
- if the AP was temporary, remove it after setup

## Network Enrollment And VPN

### Was `config-network --network` explicitly set to the intended deployment network?

Expected answer:

- `yes`

Current repo behavior:

- `--network` is now required and must be provided explicitly

Why it matters:

- enrolling into the wrong network can expose the device to the wrong control plane or administrative population

Mitigation if `no`:

- rerun `config-network` with the correct network
- replace the generated WireGuard config and keys if needed

### Is the configured WireGuard endpoint the one the deployed device will actually be able to reach?

Expected answer:

- `yes`

Current repo behavior:

- `--config-server` is relative to the setup environment
- the server returns a `wg_endpoint`
- `config-network` allows overriding that with `--wg-endpoint`

Why it matters:

- setup-lab reachability and field reachability are often different

Mitigation if `no` or `unknown`:

- rerun `config-network` with the correct `--wg-endpoint`
- verify the endpoint is appropriate for the field network path, not just the enrollment environment

## Secrets And Stored Credentials

### Is the API password present on disk?

Expected answer:

- `yes`, if the device needs to talk to the server API

Current repo behavior:

- stored at `/sensos/keys/api_password`
- written with mode `0640` by the current helper code

Why it matters:

- this is a long-lived credential on disk

Mitigation if `yes`:

- treat the device as holding a live secret
- restrict who can read `/sensos/keys`
- rotate the API password if the device is lost, repurposed, or leaves controlled custody
- consider tightening file ownership/mode if broader readability is unnecessary

### Are there unexpected secrets or private keys under `/sensos/keys` or `/etc/wireguard`?

Expected answer:

- `no`

Why it matters:

- stale or copied credentials are easy to miss during reprovisioning

Mitigation if `yes`:

- inventory what is present
- remove anything not required for the deployed role
- rotate affected credentials if provenance is unclear

## Services And Features

### Are services enabled that are not needed for this deployment?

Examples:

- hotspot
- GPS
- BirdNET
- debug packet capture

Expected answer:

- `no`

Why it matters:

- unnecessary services add attack surface, local listeners, log volume, and operational complexity

Mitigation if `yes`:

- disable or stop the services you do not need
- remove temporary setup configurations before shipping the device

### Is debug packet capture still present from troubleshooting?

Expected answer:

- `no`

Current repo behavior:

- debug captures are intended to be temporary and stored under `/sensos/log/network_capture`

Why it matters:

- retained packet captures can hold sensitive network metadata

Mitigation if `yes`:

- generate the reports you need
- remove the raw capture files with `debug-network-capture report --latest --cleanup` or `debug-network-capture cleanup`

## Physical And Deployment Questions

### If someone gets local network proximity, what can they do?

Ask:

- can they reach an active hotspot?
- can they SSH with a password?
- can they pivot through the VPN subnet?

Mitigation when risk is too high:

- disable hotspot
- disable SSH passwords
- verify WireGuard and local firewall policy match the deployment model

### If someone gets the device filesystem, what secrets do they obtain?

Ask:

- API password?
- WireGuard private key?
- SSH private key?
- location metadata?

Mitigation when risk is too high:

- reduce stored secrets where possible
- rotate credentials on loss or redeployment
- scrub devices before reassignment

## Minimum Recommended Posture

For a typical field deployment, a good baseline is:

- `sensos-admin` and `sensos-runner` passwords locked
- bootstrap user reviewed or removed
- SSH key access verified
- SSH passwords disabled unless there is a clear operational need
- hotspot disabled unless explicitly needed in the field
- hotspot SSID and password not left at predictable or reused values
- `config-network` run with explicit `--network`
- `wg_endpoint` reviewed for field reachability
- API password treated as a live secret
- temporary debug captures removed after review

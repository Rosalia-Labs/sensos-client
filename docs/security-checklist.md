# Security Checklist

This is a simple operator checklist for deployment time. It is not a developer security design document.

The main question is:

- what is the access policy for this device?

Before deployment, make sure you have made deliberate choices about passwords, keys, hotspot access, and recovery access.

## How To Use This

For each question:

- answer it plainly
- if the answer is not what you intended, fix it before deployment
- if you are intentionally accepting a weaker access path for field operations, make sure the team knows that and knows how to use it

## Access Policy

### Did you decide how this device should be accessed in the field?

Expected answer:

- `yes`

Examples:

- hotspot password plus user password
- SSH keys only
- hotspot disabled, VPN only
- setup hotspot enabled temporarily, then removed

If `no`:

- stop and define the intended access policy before deployment
- do not leave the device on whatever defaults happened to be present

### Is this device going to a publicly accessible location?

Expected answer:

- you should know the answer

Why it matters:

- public or semi-public deployments need stricter choices
- controlled internal deployments can justify some operational tradeoffs

If `yes`:

- avoid default hotspot credentials
- prefer stronger passwords or SSH keys
- remove temporary setup access if it is not required

## Passwords

### Did you keep any default passwords?

Expected answer:

- usually `no`

Examples to think about:

- hotspot password
- user password used for SSH or console login

If `yes`:

- change them before deployment
- do not rely on controlled location alone as the only safeguard

### If you are using passwords, did you set them to strong values?

Expected answer:

- `yes`

If `no`:

- replace them with strong passwords or passphrases
- avoid short, reused, guessable, or fleet-wide shared values unless you have explicitly accepted that risk

### If you are using passwords, did you save them in a secure place for future access?

Expected answer:

- `yes`

Why it matters:

- field-friendly password access only works if the team can actually recover the credentials later

If `no`:

- store them in the team password manager or other approved secure record
- make sure the people who may need field access can retrieve them

## SSH Access

### Did you decide whether SSH password login is part of the access policy?

Expected answer:

- `yes`

Why it matters:

- password SSH can be a reasonable operational choice
- key-only SSH can be stronger, but it is not automatically the right answer for every field deployment

If `no`:

- decide now whether password SSH is intentionally enabled or intentionally disabled

### If you decided not to use passwords, did you install public keys and test access?

Expected answer:

- `yes`

If `no`:

- install the required public keys
- test access from the laptop or admin path you expect to use
- do not disable password access until key-based access is verified

### If you decided to keep password SSH, did you test that login path before deployment?

Expected answer:

- `yes`

If `no`:

- test actual login with the intended user and password
- make sure the team knows which account to use

## Hotspot Access

### Is the hotspot enabled?

Expected answer:

- it depends on the deployment

If `yes`:

- make sure that is intentional
- if it was only needed for setup, disable or remove it before deployment

### If the hotspot is enabled, did you set a password-protected hotspot intentionally?

Expected answer:

- `yes`

If `no`:

- run `config-hotspot --password '<strong-passphrase>'`
- preferably also set `--ssid <deployment-specific-ssid>`

### Is the device still using the default automatic hotspot identity from the other repo?

Expected answer:

- `no`

Operational note:

- in a related repo, some devices may come up with:
  - SSID `sensos`
  - password `sensossensos`
- running `config-hotspot` with a password replaces that with a password-protected configured hotspot

If `yes`:

- run `config-hotspot --password '<strong-unique-passphrase>'`
- preferably also set a deployment-specific SSID with `--ssid`
- if hotspot access is not needed, disable or remove the hotspot profile

### If the hotspot is enabled, did you save the hotspot credentials in a secure place?

Expected answer:

- `yes`

If `no`:

- store the hotspot SSID and password in the same secure record used for device access details

## Network And Enrollment

### Did you explicitly set the intended network during enrollment?

Expected answer:

- `yes`

Operational note:

- `config-network` now requires `--network`

If `no`:

- rerun enrollment with the correct `--network`

### Did you verify that the WireGuard endpoint will work in the deployed environment?

Expected answer:

- `yes`

Operational note:

- `--setup-server` is the address used during setup
- the deployed device may need a different reachable endpoint
- in the standard SensOS QEMU workflow, setup uses `10.0.2.2:18765`, while the
  first WireGuard test network should use the server-published endpoint
  `10.0.2.2:51281`
- `config-network` allows override with `--wg-endpoint`

If `no`:

- rerun `config-network` with the correct `--wg-endpoint`

## Recovery

### If the primary access method fails, do you still have a recovery path?

Expected answer:

- `yes`

Examples:

- hotspot plus password
- alternate admin laptop with keys
- console access plan
- documented fallback credentials

If `no`:

- do not deploy until you have one

### Has the actual access method been tested, not just configured?

Expected answer:

- `yes`

If `no`:

- test the real path now
- for example:
  - join the hotspot
  - SSH with the expected account
  - verify the saved credentials are correct

## Minimum Practical Standard

Before deployment, you should be able to say:

- we decided how this device will be accessed
- we did not leave unknown defaults in place
- if we use passwords, they are strong enough and stored securely
- if we use keys, they are installed and tested
- if hotspot access is enabled, it is intentional and password-protected
- we know the deployed network settings are correct
- we have a tested recovery path

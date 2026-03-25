# BirdNET Setup

This repo now includes the BirdNET processing worker as an optional managed
component.

BirdNET is not part of the base client runtime. The base `install` and
`upgrade` flows deploy the code and service unit, but BirdNET itself is only
activated after you opt in and install its separate Python runtime.

## What gets installed by default

Normal client setup installs:

- `/sensos/libexec/process-birdnet.py`
- `/etc/systemd/system/process-birdnet.service`
- `/sensos/birdnet/`

Normal client setup does **not**:

- create the BirdNET venv
- install TensorFlow / BirdNET Python dependencies
- enable or start `process-birdnet.service`

That work is done by `config-birdnet`.

## Prerequisites

Before enabling BirdNET, you need:

1. A working SensOS client install.
2. BirdNET model files under:

```text
/sensos/birdnet/BirdNET_v2.4_tflite/
```

Required files:

```text
/sensos/birdnet/BirdNET_v2.4_tflite/audio-model.tflite
/sensos/birdnet/BirdNET_v2.4_tflite/labels/en_us.txt
```

Optional:

```text
/sensos/birdnet/BirdNET_v2.4_tflite/meta-model.tflite
```

3. BirdNET Python dependencies installed into the BirdNET-specific venv.

The exploratory dependency file is:

[`python/requirements-birdnet.txt`](/Users/tkeitt/Projects/sensos-client/python/requirements-birdnet.txt)

Current exploratory contents:

- `numpy`
- `soundfile`

The managed BirdNET path installs either `tensorflow` or `tflite-runtime`
based on the configured backend.

## Download model files

To download and install the BirdNET model bundle directly into `/sensos/birdnet`:

```bash
sudo -u sensos-admin install-birdnet-models
```

To use a different URL:

```bash
sudo -u sensos-admin install-birdnet-models --url '<zip-url>'
```

## Enable BirdNET

Run as `sensos-admin`:

```bash
sudo -u sensos-admin config-birdnet
```

Or, to download the model bundle first and then enable BirdNET:

```bash
sudo -u sensos-admin config-birdnet --download-models
```

To use TensorFlow Lite instead of TensorFlow:

```bash
sudo -u sensos-admin config-birdnet --backend tflite
```

That will:

- verify the model files exist
- write `/sensos/etc/birdnet.env`
- run `setup/08-birdnet`
- create `/sensos/python/birdnet-venv` if needed
- install or refresh BirdNET Python dependencies lazily
- enable `process-birdnet.service`

To enable and start immediately:

```bash
sudo -u sensos-admin config-birdnet --start-service
```

To disable BirdNET:

```bash
sudo -u sensos-admin config-birdnet --disable
```

## Install BirdNET dependencies manually

If you want to test dependencies before enabling the service:

```bash
python3 -m venv /sensos/python/birdnet-venv
/sensos/python/birdnet-venv/bin/pip install -r /home/<user>/sensos-client/python/requirements-birdnet.txt
```

If you need to test the backend manually, installing `tensorflow`, `numpy`, and
`soundfile` into `/sensos/python/birdnet-venv` should match the managed path for
the TensorFlow backend. For the TensorFlow Lite backend, use `tflite-runtime`
instead of `tensorflow`.

## Generate test WAV files

To generate synthetic queued WAV files:

```bash
python3 /path/to/repo/test/generate-queued-wav --count 3 --preset birdish
```

That writes files under:

```text
/sensos/data/audio_recordings/queued/YYYY/MM/DD/
```

Useful presets:

- `birdish`
- `mixed`
- `noise`

## Start and inspect the service

Start BirdNET manually:

```bash
sudo systemctl start process-birdnet.service
```

Follow logs:

```bash
sudo journalctl -u process-birdnet.service -f
```

Inspect service definition:

```bash
systemctl cat process-birdnet.service
```

## Check outputs

Input WAVs are read from:

```text
/sensos/data/audio_recordings/queued/
```

Processed FLAC outputs are written under:

```text
/sensos/data/audio_recordings/processed/
```

BirdNET state database:

```text
/sensos/data/birdnet/birdnet.db
```

Quick checks:

```bash
find /sensos/data/audio_recordings/processed -type f | head
sqlite3 /sensos/data/birdnet/birdnet.db '.tables'
sqlite3 /sensos/data/birdnet/birdnet.db 'select source_path,status from processed_files;'
```

## Upgrade behavior

Base `./upgrade` continues to deploy BirdNET code and service files, but does
not force BirdNET runtime installation unless BirdNET has been opted in via:

```text
/sensos/etc/birdnet.env
```

Once BirdNET is enabled, `setup/08-birdnet` lazily reconciles the BirdNET venv
based on the contents of
[`python/requirements-birdnet.txt`](/Users/tkeitt/Projects/sensos-client/python/requirements-birdnet.txt).

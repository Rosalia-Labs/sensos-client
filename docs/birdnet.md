# BirdNET Setup

This repo now includes the BirdNET processing worker as an optional managed
component.

BirdNET is not part of the base client runtime. The base `install` and
`upgrade` flows deploy the code and service unit, but BirdNET itself is only
activated after you opt in and install its separate Python runtime.

## What gets installed by default

Normal client setup installs:

- `/sensos/libexec/process-birdnet.py`
- `/etc/systemd/system/sensos-birdnet.service`
- `/sensos/birdnet/`

Normal client setup does **not**:

- create the BirdNET venv
- install TensorFlow / BirdNET Python dependencies
- enable or start `sensos-birdnet.service`

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

[`python/requirements-birdnet.txt`](../python/requirements-birdnet.txt)

Current exploratory contents:

- `numpy`
- `soundfile`

The managed BirdNET path installs either `ai-edge-litert` or `tensorflow`
based on the configured backend. The default backend is LiteRT.

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

If model files are missing, `config-birdnet` now downloads and installs them automatically before enabling BirdNET.

```bash
sudo -u sensos-admin config-birdnet
```

LiteRT is the default backend. To use TensorFlow instead:

```bash
sudo -u sensos-admin config-birdnet --backend tensorflow
```

By default BirdNET processes every input channel independently. Each BirdNET
input and retained output clip is a mono, 3-second window, and its zero-based
channel index is stored in the local database, output filename, and server
upload. To explicitly mix multichannel input down to one analysis stream:

```bash
sudo -u sensos-admin config-birdnet --input-mode mono
```

That will:

- verify the model files exist
- write `/sensos/etc/birdnet.env`
- run `setup/08-birdnet`
- create `/sensos/python/birdnet-venv` if needed
- install or refresh BirdNET Python dependencies lazily
- enable `sensos-birdnet.service`

Supported input modes:

- `mono`: average all channels into one analysis stream
- `split-channels` (default): run BirdNET separately on every channel and include the channel index in output filenames and downstream metadata
- `cardinal-beams`: create device-relative north, east, south, and west delay-and-sum beams from exactly four synchronized microphones, then process each beam as a mono stream

Cardinal beam indices are `0=north`, `1=east`, `2=south`, and `3=west`. These
are device-relative directions until device heading is incorporated. Configure
the physical microphone centers as x,y coordinates in centimeters, in capture
channel order:

```bash
sudo -u sensos-admin config-birdnet \
  --input-mode cardinal-beams \
  --mic-positions-cm '0,200;200,0;0,-200;-200,0' \
  --start-service
```

The provisional default above models a four-microphone cross with microphones
two meters north, east, south, and west of array center. It is intended to
represent installations where microphones are roughly one to three meters from
center. Replace it with measured positions for each hardware layout.
Measurements do not need survey precision, but channel order and device-relative
axis orientation must be consistent. Cardinal beamforming assumes all four
channels share a synchronized audio clock.

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
the TensorFlow backend. For the LiteRT backend, use `ai-edge-litert`
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

For a four-channel directional beamforming test, add `--spatial-mode
cardinal-cycle`. The generated file cycles through north, east, south, and west
plane-wave arrivals in consecutive 3-second segments. Pass the same
`--mic-positions-cm` geometry used by `config-birdnet`.

The normal runtime flow moves stable queued WAVs into:

```text
/sensos/data/audio_recordings/compressed/
```

BirdNET reads from that compressed FLAC backlog rather than directly from the
live queue.

## Start and inspect the service

Start BirdNET manually:

```bash
sudo systemctl start sensos-birdnet.service
```

Follow logs:

```bash
sudo journalctl -u sensos-birdnet.service -f
```

Or inspect the thinning worker separately:

```bash
sudo journalctl -u sensos-thin-data.service -f
```

Inspect service definition:

```bash
systemctl cat sensos-birdnet.service
```

## Check outputs

BirdNET inputs are read from:

```text
/sensos/data/audio_recordings/compressed/
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
sqlite3 /sensos/data/birdnet/birdnet.db 'select source_path,started_at,ended_at,deleted_source from processed_files order by started_at limit 20;'
sqlite3 /sensos/data/birdnet/birdnet.db 'select source_path,channel_index,event_started_at,event_ended_at,top_label from detections order by event_started_at limit 20;'
```

## Upgrade behavior

Base `./upgrade` continues to deploy BirdNET code and service files, but does
not force BirdNET runtime installation unless BirdNET has been opted in via:

```text
/sensos/etc/birdnet.env
```

Once BirdNET is enabled, `setup/08-birdnet` lazily reconciles the BirdNET venv
based on the contents of
[`python/requirements-birdnet.txt`](../python/requirements-birdnet.txt).

# Data Copy

`prepare-data-copy` is the operator command to use before copying `/sensos/data`
off a client without removing the storage media.

This is useful when:

- `/sensos/data` is part of the root device and cannot be swapped
- you are connected remotely over SSH, hotspot, or a local network
- you want to copy data to a laptop or server using `rsync`, `scp`, or a similar tool

## What It Does

When you run:

```sh
prepare-data-copy
```

the command:

- stops the main services that write under `/sensos/data`
- checkpoints SQLite databases under `/sensos/data`
- runs `sync`

This is intended to leave the data tree in a consistent state for copying.

The SQLite checkpoint step matters because some databases use WAL mode. Without
checkpointing first, a naive copy may miss recent writes that still live in the
WAL file rather than the main database file.

## What It Stops

`prepare-data-copy` stops the main data-writing services, including:

- audio recording
- queued-audio compression
- I2C sensor logging
- BirdNET processing
- GPS logging
- data thinning
- the `/sensos/data` free-space monitor timer

By default, those services remain stopped until you restart them or reboot.

## Typical Workflow

On the client:

```sh
prepare-data-copy
```

Then copy the data using your preferred transfer method. For example, from a laptop:

```sh
rsync -a sensos@<device>:/sensos/data/ ./sensos-data/
```

or on the client:

```sh
scp -r /sensos/data user@host:/path/to/destination/
```

After the copy is complete, either reboot the client or restart the stopped services.

If you want the command to restart the services immediately after preparing the
copy window, use:

```sh
prepare-data-copy --resume true
```

That is mainly useful when you are using the command as a quick consistency
checkpoint before a copy that starts immediately.

## Important Limitation

`prepare-data-copy --resume true` does not wait for your transfer to finish. It
simply prepares the data, then starts services again right away.

So:

- use `prepare-data-copy` when you want a quiet, stable copy window
- use `prepare-data-copy --resume true` only when you explicitly want the
  services restarted immediately after preparation

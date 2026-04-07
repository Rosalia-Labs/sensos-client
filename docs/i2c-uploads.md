# I2C Upload API

This document defines the client-to-server contract for uploaded I2C readings.

The client stores readings in a normalized SQLite table and uploads only numeric readings. Non-numeric values are skipped before they ever enter the upload queue, so GPS `"fix"`-style values are not part of this API.

## Endpoint

`POST /i2c-readings/upload`

Authentication:

- HTTP Basic auth
- username: `sensos`
- password: client API password

Content type:

- `application/json`

## Request Body

```json
{
  "schema_version": 1,
  "wireguard_ip": "10.42.1.9",
  "hostname": "sensor-1",
  "client_version": "0.5.0",
  "batch_id": 17,
  "sent_at": "2026-04-07T12:01:00Z",
  "ownership_mode": "server-owns",
  "reading_count": 3,
  "first_reading_id": 101,
  "last_reading_id": 103,
  "first_recorded_at": "2026-04-07T12:00:00Z",
  "last_recorded_at": "2026-04-07T12:00:30Z",
  "readings": [
    {
      "id": 101,
      "timestamp": "2026-04-07T12:00:00Z",
      "device_address": "0x76",
      "sensor_type": "BME280",
      "key": "temperature_c",
      "value": 22.5
    },
    {
      "id": 102,
      "timestamp": "2026-04-07T12:00:00Z",
      "device_address": "0x76",
      "sensor_type": "BME280",
      "key": "humidity_percent",
      "value": 44.1
    },
    {
      "id": 103,
      "timestamp": "2026-04-07T12:00:30Z",
      "device_address": "0x61",
      "sensor_type": "SCD30",
      "key": "co2_ppm",
      "value": 612.4
    }
  ]
}
```

Notes:

- `ownership_mode` is one of `client-retains` or `server-owns`.
- `batch_id` is the client-local upload ledger id.
- `id` inside each reading is the client-local SQLite row id.
- `readings` are already flattened. A single sensor poll can produce multiple rows.
- `reading_count` must match `len(readings)`.

## Ownership Semantics

`client-retains`

- the server has a copy after acceptance
- the client remains the authoritative owner
- the client must not treat server acceptance as permission to delete those readings

`server-owns`

- the server becomes the authoritative owner after acceptance
- the client may later prune local copies according to its retention policy
- the client keeps a local batch ledger and deletion log so the transfer is auditable

## Success Response

The server must return HTTP `200` with a JSON body:

```json
{
  "status": "ok",
  "receipt_id": "receipt-123",
  "accepted_count": 3,
  "server_received_at": "2026-04-07T12:01:01Z"
}
```

Rules:

- `status` must be `ok`
- `receipt_id` must be non-empty and stable enough to use as an audit handle
- `accepted_count` must exactly match the request `reading_count`
- `server_received_at` must be an RFC 3339 UTC timestamp

If any of those conditions are not met, the client treats the upload as failed and keeps the readings locally.

## Local Tracking

The client records transfer state locally in `i2c_readings.db`:

- `i2c_readings`: one row per numeric reading, with `server_copy`, `authoritative_owner`, `uploaded_at`, `server_receipt_id`, and upload-attempt fields
- `i2c_upload_batches`: one row per upload attempt, including ownership mode, id/timestamp bounds, HTTP status, and server receipt
- `i2c_upload_batch_readings`: mapping table from batch to readings
- `i2c_deletion_log`: records local pruning of server-owned data

That model makes ownership explicit even after server-owned local rows are deleted later.

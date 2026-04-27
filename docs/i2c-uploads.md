# I2C Upload API

This document defines the client-to-server contract for uploaded I2C readings.

The client stores numeric readings in a local SQLite table. When
`sensos-upload-i2c.service` runs, it sends rows where `sent_to_server = 0` to
the server for public UI display. After a successful server response, the client
marks those local rows as sent. The client remains the owner of the data.

## Endpoint

`POST /api/v1/client/peer/i2c-readings`

Authentication:

- HTTP Basic auth
- username: peer UUID
- password: peer API password

Content type:

- `application/json`

## Request Body

```json
{
  "hostname": "sensor-1",
  "client_version": "0.5.0",
  "sent_at": "2026-04-07T12:01:00Z",
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
    }
  ]
}
```

Notes:

- `id` is the client-local SQLite row id.
- `readings` are flattened. A single sensor poll can produce multiple rows.
- Only numeric readings are queued for upload.

## Success Response

The server returns HTTP `200` with a JSON body:

```json
{
  "status": "ok",
  "receipt_id": "receipt-123",
  "accepted_count": 2,
  "server_received_at": "2026-04-07T12:01:01Z"
}
```

The client treats any non-`ok` response or failed request as a failed upload and
leaves the readings marked unsent locally.

## Local Tracking

The client records transfer state locally in `i2c_readings.db`:

- `i2c_readings`: one row per numeric reading with `sent_to_server`

No local upload batch ledger is maintained.

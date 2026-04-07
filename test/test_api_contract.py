import base64
import importlib.machinery
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
OVERLAY_ROOT = REPO_ROOT / "overlay"
os.environ["SENSOS_CLIENT_ROOT"] = str(OVERLAY_ROOT)


def load_module(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


config_network = load_module("config_network_test", OVERLAY_ROOT / "bin" / "config-network")
send_status_update = load_module(
    "send_status_update_test", OVERLAY_ROOT / "libexec" / "send_status_update.py"
)
config_location = load_module("config_location_test", OVERLAY_ROOT / "bin" / "config-location")
config_time = load_module("config_time_test", OVERLAY_ROOT / "bin" / "config-time")
upload_hardware_profile = load_module(
    "upload_hardware_profile_test", OVERLAY_ROOT / "bin" / "upload-hardware-profile"
)
i2c_upload = load_module(
    "i2c_upload_test", OVERLAY_ROOT / "libexec" / "upload-i2c-readings.py"
)
utils = load_module("utils_test", OVERLAY_ROOT / "libexec" / "utils.py")


class FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


class ApiContractTests(unittest.TestCase):
    def test_register_peer_parses_current_response_and_registers_wireguard_key(self):
        response = FakeResponse(
            200,
            {
                "wg_ip": "10.42.1.9",
                "wg_public_key": "server-public-key",
                "wg_public_ip": "198.51.100.20",
                "wg_port": 51820,
                "peer_uuid": "peer-123",
                "peer_api_password": "peer-secret",
            },
            text='{"wg_ip":"10.42.1.9"}',
        )

        fake_requests = SimpleNamespace(post=mock.Mock(return_value=response))
        with mock.patch.object(config_network, "http_requests", return_value=fake_requests):
            with mock.patch.object(config_network, "register_wireguard_key") as register_mock:
                result = config_network.register_peer(
                    "config.example",
                    8765,
                    "fieldnet",
                    "client-public-key",
                    "client-password",
                    1,
                    note="sensor node",
                )

        self.assertEqual(
            result,
            (
                "10.42.1.9",
                "fieldnet-1-9",
                "server-public-key",
                "198.51.100.20",
                51820,
                "peer-123",
                "peer-secret",
            ),
        )
        self.assertEqual(
            fake_requests.post.call_args.kwargs["json"],
            {
                "network_name": "fieldnet",
                "subnet_offset": 1,
                "note": "sensor node",
            },
        )
        register_mock.assert_called_once_with(
            "config.example",
            8765,
            "peer-123",
            "10.42.1.9",
            "client-public-key",
            "peer-secret",
        )

    def test_register_wireguard_key_uses_peer_auth_and_server_assigned_identity(self):
        response = FakeResponse(200, {}, text="ok")
        fake_requests = SimpleNamespace(post=mock.Mock(return_value=response))
        with mock.patch.object(config_network, "http_requests", return_value=fake_requests):
            config_network.register_wireguard_key(
                "config.example",
                8765,
                "peer-123",
                "10.42.1.9",
                "client-public-key",
                "peer-secret",
            )

        self.assertEqual(
            fake_requests.post.call_args.kwargs["json"],
            {
                "wg_public_key": "client-public-key",
            },
        )
        self.assertEqual(
            fake_requests.post.call_args.kwargs["headers"]["Authorization"],
            "Basic " + base64.b64encode(b"peer-123:peer-secret").decode(),
        )

    def test_client_status_payload_uses_current_server_field_names(self):
        payload = send_status_update.build_client_status_payload(
            hostname="sensor-1",
            uptime_seconds=123,
            disk_available_gb=80,
            memory_used_mb=256,
            memory_total_mb=1024,
            load_1m=0.1,
            load_5m=0.2,
            load_15m=0.3,
            version="1.2.3",
            status_message="ready",
        )

        self.assertEqual(
            set(payload.keys()),
            {
                "hostname",
                "uptime_seconds",
                "disk_available_gb",
                "memory_used_mb",
                "memory_total_mb",
                "load_1m",
                "load_5m",
                "load_15m",
                "version",
                "status_message",
            },
        )
        self.assertNotIn("wg_ip", payload)
        self.assertNotIn("wireguard_ip", payload)

    def test_healthz_check_is_unauthenticated(self):
        mock_get = mock.Mock(return_value=FakeResponse(200, {"status": "ok"}))
        fake_requests = SimpleNamespace(
            get=mock_get,
            exceptions=SimpleNamespace(ConnectionError=RuntimeError),
        )

        with mock.patch.object(utils, "require_requests", return_value=fake_requests):
            health = utils.get_server_health("config.example", 8765)

        self.assertEqual(health, {"reachable": True, "ready": True, "status": "ok"})
        self.assertEqual(mock_get.call_args.args[0], "http://config.example:8765/healthz")
        self.assertEqual(mock_get.call_args.kwargs, {"timeout": 3})

    def test_client_auth_validation_uses_network_lookup_and_configured_password(self):
        mock_get = mock.Mock(return_value=FakeResponse(404, {"detail": "not found"}))
        fake_requests = SimpleNamespace(get=mock_get)

        with mock.patch.object(utils, "require_requests", return_value=fake_requests):
            validation = utils.validate_api_password(
                "config.example",
                8765,
                "client-password",
                network_name="fieldnet",
            )

        self.assertEqual(
            validation,
            {
                "ok": True,
                "reason": "accepted",
                "status_code": 404,
                "url": "http://config.example:8765/api/v1/client/networks/fieldnet",
            },
        )
        self.assertEqual(
            mock_get.call_args.args[0],
            "http://config.example:8765/api/v1/client/networks/fieldnet",
        )
        self.assertEqual(
            mock_get.call_args.kwargs["headers"],
            {
                "Authorization": "Basic "
                + base64.b64encode(b"sensos:client-password").decode()
            },
        )

    def test_client_auth_validation_rejects_bad_credentials_on_client_route(self):
        mock_get = mock.Mock(return_value=FakeResponse(401, {"detail": "unauthorized"}))
        fake_requests = SimpleNamespace(get=mock_get)

        with mock.patch.object(utils, "require_requests", return_value=fake_requests):
            validation = utils.validate_api_password(
                "config.example",
                8765,
                "wrong-password",
                network_name="fieldnet",
            )

        self.assertEqual(validation["ok"], False)
        self.assertEqual(validation["reason"], "invalid_credentials")
        self.assertEqual(validation["status_code"], 401)

    def test_hardware_profile_payload_includes_required_top_level_fields(self):
        with mock.patch.object(upload_hardware_profile, "collect_model", return_value="Test Device"):
            with mock.patch.object(upload_hardware_profile, "collect_cpu", return_value={"model_name": "cpu"}):
                with mock.patch.object(upload_hardware_profile, "collect_firmware", return_value={"bios_version": "1"}):
                    with mock.patch.object(upload_hardware_profile, "collect_memory", return_value={"mem_total_mb": 1024}):
                        with mock.patch.object(upload_hardware_profile, "collect_disks", return_value={"vda": {"path": "/dev/vda"}}):
                            with mock.patch.object(upload_hardware_profile, "collect_usb_devices", return_value="Bus 001 Device 001: test"):
                                with mock.patch.object(upload_hardware_profile, "collect_network_interfaces", return_value={"eth0": {"ipv4": ["10.0.2.15"]}}):
                                    payload = upload_hardware_profile.build_hardware_profile_payload(
                                        hostname="sensor-1",
                                    )

        self.assertEqual(
            set(payload.keys()),
            {
                "hostname",
                "model",
                "kernel_version",
                "cpu",
                "firmware",
                "memory",
                "disks",
                "usb_devices",
                "network_interfaces",
            },
        )
        self.assertIsInstance(payload["disks"], dict)
        self.assertIsInstance(payload["usb_devices"], str)
        self.assertIsInstance(payload["network_interfaces"], dict)

    def test_i2c_upload_payload_includes_batch_metadata_and_normalized_readings(self):
        payload = i2c_upload.build_i2c_upload_payload(
            hostname="sensor-1",
            client_version="1.2.3",
            batch_id=17,
            ownership_mode="server-owns",
            readings=[
                {
                    "id": 101,
                    "timestamp": "2026-04-07T12:00:00Z",
                    "device_address": "0x76",
                    "sensor_type": "BME280",
                    "key": "temperature_c",
                    "value": 22.5,
                },
                {
                    "id": 102,
                    "timestamp": "2026-04-07T12:00:00Z",
                    "device_address": "0x76",
                    "sensor_type": "BME280",
                    "key": "humidity_percent",
                    "value": 44.1,
                },
            ],
        )

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["hostname"], "sensor-1")
        self.assertEqual(payload["client_version"], "1.2.3")
        self.assertEqual(payload["batch_id"], 17)
        self.assertEqual(payload["ownership_mode"], "server-owns")
        self.assertEqual(payload["reading_count"], 2)
        self.assertEqual(payload["first_reading_id"], 101)
        self.assertEqual(payload["last_reading_id"], 102)
        self.assertEqual(payload["first_recorded_at"], "2026-04-07T12:00:00Z")
        self.assertEqual(payload["last_recorded_at"], "2026-04-07T12:00:00Z")
        self.assertEqual(
            payload["readings"][0],
            {
                "id": 101,
                "timestamp": "2026-04-07T12:00:00Z",
                "device_address": "0x76",
                "sensor_type": "BME280",
                "key": "temperature_c",
                "value": 22.5,
            },
        )

    def test_i2c_upload_response_requires_receipt_and_full_acceptance(self):
        parsed = i2c_upload.parse_upload_response(
            '{"status":"ok","receipt_id":"receipt-123","accepted_count":2,"server_received_at":"2026-04-07T12:01:00Z"}',
            2,
        )

        self.assertEqual(
            parsed,
            {
                "receipt_id": "receipt-123",
                "accepted_count": 2,
                "server_received_at": "2026-04-07T12:01:00Z",
            },
        )

    def test_i2c_upload_response_rejects_partial_acceptance(self):
        with self.assertRaises(ValueError):
            i2c_upload.parse_upload_response(
                '{"status":"ok","receipt_id":"receipt-123","accepted_count":1,"server_received_at":"2026-04-07T12:01:00Z"}',
                2,
            )

    def test_config_network_defaults_steady_state_port_to_8765_not_setup_port(self):
        argv = [
            "config-network",
            "--config-server",
            "10.0.2.2",
            "--setup-port",
            "18765",
            "--network",
            "testing",
        ]
        with mock.patch.object(sys, "argv", argv):
            args = config_network.parse_args()

        self.assertEqual(args.port, 18765)
        self.assertEqual(args.config_port, 8765)

    def test_config_network_keeps_port_as_backward_compatible_setup_port_alias(self):
        argv = [
            "config-network",
            "--config-server",
            "10.0.2.2",
            "--port",
            "18765",
            "--network",
            "testing",
        ]
        with mock.patch.object(sys, "argv", argv):
            args = config_network.parse_args()

        self.assertEqual(args.port, 18765)
        self.assertEqual(args.config_port, 8765)

    def test_write_client_settings_separates_setup_and_steady_state_api_ports(self):
        args = SimpleNamespace(
            config_server="10.0.2.2",
            port=18765,
            config_port=8765,
            network="testing",
        )

        with mock.patch.object(config_network, "write_file") as write_file_mock:
            config_network.write_client_settings(
                args,
                "10.254.0.1",
                "10.254.1.5",
                "10.0.2.2",
                51281,
                peer_uuid="peer-123",
            )

        written = write_file_mock.call_args.args[1]
        self.assertIn("SETUP_API_HOST=10.0.2.2\n", written)
        self.assertIn("SETUP_API_PORT=18765\n", written)
        self.assertIn("SERVER_WG_IP=10.254.0.1\n", written)
        self.assertIn("SERVER_PORT=8765\n", written)
        self.assertIn("WG_ENDPOINT_IP=10.0.2.2\n", written)
        self.assertIn("WG_ENDPOINT_PORT=51281\n", written)

    def test_config_location_prompts_for_missing_values_when_interactive(self):
        args = SimpleNamespace(latitude=None, longitude=None)
        with mock.patch.object(config_location, "is_interactive", return_value=True):
            with mock.patch.object(
                config_location,
                "prompt_for_float",
                side_effect=[30.2672, -97.7431],
            ):
                updated = config_location.fill_missing_location_args(args)

        self.assertEqual(updated.latitude, 30.2672)
        self.assertEqual(updated.longitude, -97.7431)

    def test_config_location_errors_for_missing_values_when_non_interactive(self):
        args = SimpleNamespace(latitude=None, longitude=None)
        with mock.patch.object(config_location, "is_interactive", return_value=False):
            with self.assertRaises(SystemExit) as exc:
                config_location.fill_missing_location_args(args)

        self.assertEqual(exc.exception.code, 1)

    def test_config_time_detects_requested_noninteractive_change(self):
        args = SimpleNamespace(
            input_timezone="America/Chicago",
            year=2026,
            month=None,
            day=None,
            hour=None,
            minute=None,
            second=None,
        )
        self.assertTrue(config_time.has_requested_time_change(args))

    def test_config_time_builds_entered_datetime_from_supplied_flags(self):
        args = SimpleNamespace(
            input_timezone="America/Chicago",
            year=2026,
            month=4,
            day=4,
            hour=8,
            minute=30,
            second=0,
        )
        fake_utc_now = config_time.datetime(2026, 4, 4, 13, 30, 0)

        with mock.patch.object(config_time, "current_utc_time", return_value=fake_utc_now):
            entry_tz, entered_dt = config_time.build_entered_datetime(args, "America/Chicago")

        self.assertEqual(entry_tz, "America/Chicago")
        self.assertEqual(entered_dt.year, 2026)
        self.assertEqual(entered_dt.month, 4)
        self.assertEqual(entered_dt.day, 4)
        self.assertEqual(entered_dt.hour, 8)
        self.assertEqual(entered_dt.minute, 30)
        self.assertEqual(entered_dt.second, 0)


if __name__ == "__main__":
    unittest.main()

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
upload_hardware_profile = load_module(
    "upload_hardware_profile_test", OVERLAY_ROOT / "bin" / "upload-hardware-profile"
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
            "10.42.1.9",
            "client-public-key",
            "client-password",
        )

    def test_register_wireguard_key_uses_assigned_wg_ip_payload(self):
        response = FakeResponse(200, {}, text="ok")
        fake_requests = SimpleNamespace(post=mock.Mock(return_value=response))
        with mock.patch.object(config_network, "http_requests", return_value=fake_requests):
            config_network.register_wireguard_key(
                "config.example",
                8765,
                "10.42.1.9",
                "client-public-key",
                "client-password",
            )

        self.assertEqual(
            fake_requests.post.call_args.kwargs["json"],
            {
                "wg_ip": "10.42.1.9",
                "wg_public_key": "client-public-key",
            },
        )

    def test_client_status_payload_uses_current_server_field_names(self):
        payload = send_status_update.build_client_status_payload(
            wireguard_ip="10.42.1.9",
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
                "wireguard_ip",
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
                "url": "http://config.example:8765/get-network-info?network_name=fieldnet",
            },
        )
        self.assertEqual(
            mock_get.call_args.args[0],
            "http://config.example:8765/get-network-info?network_name=fieldnet",
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
                                        "10.42.1.9",
                                        hostname="sensor-1",
                                    )

        self.assertEqual(
            set(payload.keys()),
            {
                "wg_ip",
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


if __name__ == "__main__":
    unittest.main()

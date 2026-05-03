import base64
import contextlib
import getpass
import importlib.machinery
import importlib.util
import io
import os
import random
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch


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
birdnet_upload = load_module(
    "birdnet_upload_test", OVERLAY_ROOT / "libexec" / "upload-birdnet-results.py"
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
    def setUp(self):
        self._patches = []
        allow_live_contract = os.environ.get("SENSOS_ALLOW_LIVE_API_CONTRACT") == "1"
        if not allow_live_contract:
            self._patches.extend(
                [
                    patch.object(config_network, "require_requests", side_effect=AssertionError("Live HTTP blocked in tests; set SENSOS_ALLOW_LIVE_API_CONTRACT=1 to allow.")),
                    patch.object(config_location, "require_requests", side_effect=AssertionError("Live HTTP blocked in tests; set SENSOS_ALLOW_LIVE_API_CONTRACT=1 to allow.")),
                    patch.object(upload_hardware_profile, "require_requests", side_effect=AssertionError("Live HTTP blocked in tests; set SENSOS_ALLOW_LIVE_API_CONTRACT=1 to allow.")),
                    patch.object(i2c_upload, "require_requests", side_effect=AssertionError("Live HTTP blocked in tests; set SENSOS_ALLOW_LIVE_API_CONTRACT=1 to allow.")),
                    patch.object(birdnet_upload, "require_requests", side_effect=AssertionError("Live HTTP blocked in tests; set SENSOS_ALLOW_LIVE_API_CONTRACT=1 to allow.")),
                    patch.object(utils, "require_requests", side_effect=AssertionError("Live HTTP blocked in tests; set SENSOS_ALLOW_LIVE_API_CONTRACT=1 to allow.")),
                ]
            )
            for p in self._patches:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in reversed(self._patches)])

    def test_contract_harness_overrides_client_root_to_repo_overlay(self):
        self.assertEqual(os.environ.get("SENSOS_CLIENT_ROOT"), str(OVERLAY_ROOT))
        self.assertTrue(str(utils.NETWORK_CONF).startswith(str(OVERLAY_ROOT)))

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

    def test_force_cleanup_falls_back_to_privileged_ls_when_wireguard_dir_not_readable(self):
        with mock.patch.object(config_network, "read_network_conf", return_value={}):
            with mock.patch.object(config_network.os, "listdir", side_effect=PermissionError("denied")):
                with mock.patch.object(config_network.os.path, "exists", return_value=False):
                    with mock.patch.object(
                        config_network,
                        "privileged_shell",
                        return_value=("testing.conf\nREADME.txt\n", 0),
                    ) as privileged_shell_mock:
                        with mock.patch.object(config_network, "read_file", return_value=""):
                            with mock.patch.object(config_network, "remove_file") as remove_file_mock:
                                config_network.remove_sensos_config_files("testing")

        privileged_shell_mock.assert_any_call("ls -1 /etc/wireguard", silent=True)
        remove_file_mock.assert_any_call("/etc/wireguard/testing-private.key")
        remove_file_mock.assert_any_call("/etc/wireguard/testing-public.key")
        remove_file_mock.assert_any_call("/etc/wireguard/testing.conf")
        self.assertNotIn(mock.call(config_network.API_PASSWORD_FILE), remove_file_mock.mock_calls)

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

    def test_get_api_password_uses_cached_api_password_without_prompt(self):
        with mock.patch.object(
            utils, "get_server_health", return_value={"reachable": True, "ready": True, "status": "ok"}
        ):
            with mock.patch.object(
                utils.os.path,
                "exists",
                side_effect=lambda p: p == utils.API_PASSWORD_FILE,
            ):
                with mock.patch.object(utils, "read_file", return_value="setup-secret\n"):
                    with mock.patch.object(
                        utils,
                        "validate_api_password",
                        return_value={"ok": True, "reason": "accepted", "status_code": 200, "url": "x"},
                    ):
                        with mock.patch("builtins.input") as input_mock:
                            password = utils.get_api_password("config.example", 8765, network_name="fieldnet")

        self.assertEqual(password, "setup-secret")
        input_mock.assert_not_called()

    def test_get_api_password_strips_newline_before_validation(self):
        with mock.patch.object(
            utils, "get_server_health", return_value={"reachable": True, "ready": True, "status": "ok"}
        ):
            with mock.patch.object(
                utils.os.path,
                "exists",
                side_effect=lambda p: p == utils.API_PASSWORD_FILE,
            ):
                with mock.patch.object(utils, "read_file", return_value="legacy-secret\n"):
                    with mock.patch.object(utils, "validate_api_password") as validate_mock:
                        validate_mock.return_value = {
                            "ok": True,
                            "reason": "accepted",
                            "status_code": 200,
                            "url": "x",
                        }
                        password = utils.get_api_password("config.example", 8765, network_name="fieldnet")

        self.assertEqual(password, "legacy-secret")
        self.assertEqual(validate_mock.call_args.args[2], "legacy-secret")

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

    def test_i2c_upload_payload_includes_normalized_readings(self):
        payload = i2c_upload.build_i2c_upload_payload(
            hostname="sensor-1",
            client_version="1.2.3",
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

        self.assertEqual(payload["hostname"], "sensor-1")
        self.assertEqual(payload["client_version"], "1.2.3")
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

    def test_i2c_upload_response_accepts_ok_status(self):
        parsed = i2c_upload.parse_upload_response(
            '{"status":"ok","receipt_id":"receipt-123","accepted_count":2,"server_received_at":"2026-04-07T12:01:00Z"}'
        )

        self.assertEqual(
            parsed,
            {
                "receipt_id": "receipt-123",
                "accepted_count": 2,
                "server_received_at": "2026-04-07T12:01:00Z",
            },
        )

    def test_i2c_upload_response_rejects_non_ok_status(self):
        with self.assertRaises(ValueError):
            i2c_upload.parse_upload_response(
                '{"status":"error","receipt_id":"receipt-123","accepted_count":1,"server_received_at":"2026-04-07T12:01:00Z"}'
            )

    def test_i2c_run_upload_session_uses_server_host_in_request_path(self):
        fake_rows = [
            {
                "id": 101,
                "timestamp": "2026-04-07T12:00:00Z",
                "device_address": "0x76",
                "sensor_type": "BME280",
                "key": "temperature_c",
                "value": 22.5,
            }
        ]
        fake_conn = mock.MagicMock()
        fake_db = mock.MagicMock()
        fake_db.__enter__.return_value = fake_conn

        with mock.patch.object(i2c_upload, "connect_db", return_value=fake_db):
            with mock.patch.object(i2c_upload, "ensure_schema"):
                with mock.patch.object(i2c_upload, "select_pending_readings", return_value=fake_rows):
                    with mock.patch.object(
                        i2c_upload,
                        "post_i2c_readings",
                        return_value=(
                            200,
                            '{"status":"ok","receipt_id":"receipt-123","accepted_count":1,"server_received_at":"2026-04-07T12:01:00Z"}',
                        ),
                    ) as post_mock:
                        with mock.patch.object(i2c_upload, "mark_readings_sent"):
                            with mock.patch.object(i2c_upload, "socket") as socket_mock:
                                socket_mock.gethostname.return_value = "sensor-1"
                                i2c_upload.run_upload_session(
                                    {
                                        "batch_size": 100,
                                        "connect_timeout_sec": 5,
                                        "read_timeout_sec": 10,
                                    },
                                    {
                                        "SERVER_WG_IP": "10.254.0.1",
                                        "SERVER_PORT": "8765",
                                        "PEER_UUID": "peer-123",
                                    },
                                    "peer-secret",
                                    "1.2.3",
                                )

        self.assertEqual(
            post_mock.call_args.args[:4],
            ("10.254.0.1", "8765", "peer-123", "peer-secret"),
        )

    def test_birdnet_upload_payload_includes_current_required_fields(self):
        detections = [
            {
                "source_path": "audio_recordings/compressed/2026/04/07/a.flac",
                "channel_index": 0,
                "window_index": 0,
                "max_score_start_frame": 0,
                "label": "Northern Cardinal (Cardinalis cardinalis)",
                "score": 0.91,
                "likely_score": 0.75,
                "volume": 0.018,
                "clip_start_time": "2026-04-07T12:00:00Z",
                "clip_end_time": "2026-04-07T12:00:03Z",
            }
        ]
        payload = birdnet_upload.build_upload_payload(
            hostname="sensor-1",
            client_version="1.2.3",
            detections=detections,
        )

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["hostname"], "sensor-1")
        self.assertEqual(payload["client_version"], "1.2.3")
        self.assertIn("sent_at", payload)
        self.assertEqual(payload["detections"], detections)

    def test_birdnet_upload_response_requires_receipt_and_full_acceptance(self):
        parsed = birdnet_upload.parse_upload_response(
            '{"status":"ok","receipt_id":"receipt-123","accepted_count":1,"server_received_at":"2026-04-07T12:01:00Z"}'
        )

        self.assertEqual(
            parsed,
            {
                "receipt_id": "receipt-123",
                "accepted_count": 1,
                "server_received_at": "2026-04-07T12:01:00Z",
            },
        )

    def test_birdnet_upload_response_rejects_non_ok_status(self):
        with self.assertRaises(ValueError):
            birdnet_upload.parse_upload_response(
                '{"status":"error","receipt_id":"receipt-123","accepted_count":1}'
            )

    def test_config_network_defaults_steady_state_port_to_8765_not_setup_port(self):
        argv = [
            "config-network",
            "--setup-server",
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

    def test_config_network_does_not_upload_hardware_profile_during_enrollment(self):
        args = SimpleNamespace(
            config_server="10.0.2.2",
            port=18765,
            config_port=8765,
            network="testing",
            force=False,
            wg_endpoint=None,
            wg_keepalive=0,
            disable_ssh_passwords=False,
        )

        with mock.patch.object(config_network, "ensure_sensos_admin"):
            with mock.patch.object(config_network, "require_dir"):
                with mock.patch.object(config_network, "require_cmd"):
                    with mock.patch.object(config_network, "setup_logging"):
                        with mock.patch.object(config_network, "parse_args", return_value=args):
                            with mock.patch.object(config_network, "sensos_config_files_exist", return_value=[]):
                                with mock.patch.object(config_network, "get_api_password", return_value="client-password"):
                                    with mock.patch.object(config_network, "ensure_network_exists"):
                                        with mock.patch.object(
                                            config_network,
                                            "configure_wireguard",
                                            return_value=(
                                                "10.254.1.5",
                                                "198.51.100.20",
                                                51281,
                                                "peer-123",
                                                "peer-secret",
                                            ),
                                        ):
                                            with mock.patch.object(
                                                config_network,
                                                "compute_api_server_wg_ip",
                                                return_value="10.254.0.1",
                                            ):
                                                with mock.patch.object(config_network, "write_client_settings"):
                                                    with mock.patch.object(config_network, "write_api_password"):
                                                        with mock.patch.object(config_network, "configure_ssh"):
                                                            with mock.patch.object(config_network, "enable_ssh"):
                                                                with mock.patch.object(config_network, "vnstat_register_interface"):
                                                                    with mock.patch.object(config_network, "restart_wireguard_service"):
                                                                        with mock.patch.object(config_network, "enable_status_update_timer"):
                                                                            with mock.patch.object(
                                                                                config_network,
                                                                                "upload_initial_hardware_profile",
                                                                                create=True,
                                                                            ) as upload_mock:
                                                                                with contextlib.redirect_stdout(io.StringIO()):
                                                                                    config_network.main()

        upload_mock.assert_not_called()

    def test_upload_hardware_profile_resolve_targets_auto_prefers_steady_state_then_setup(self):
        targets = upload_hardware_profile.resolve_upload_targets(
            {
                "SERVER_WG_IP": "10.254.0.1",
                "SERVER_PORT": "8765",
                "SETUP_API_HOST": "10.0.2.2",
                "SETUP_API_PORT": "18765",
            },
            "auto",
        )

        self.assertEqual(targets, [("10.254.0.1", "8765"), ("10.0.2.2", "18765")])

    def test_upload_hardware_profile_setup_transport_uses_setup_api_target(self):
        with mock.patch.object(
            upload_hardware_profile,
            "read_network_conf",
            return_value={
                "SERVER_WG_IP": "10.254.0.1",
                "SERVER_PORT": "8765",
                "SETUP_API_HOST": "10.0.2.2",
                "SETUP_API_PORT": "18765",
                "PEER_UUID": "peer-123",
            },
        ):
            with mock.patch.object(upload_hardware_profile, "read_api_password", return_value="secret"):
                with mock.patch.object(
                    upload_hardware_profile,
                    "build_hardware_profile_payload",
                    return_value={"hostname": "node-1"},
                ):
                    with mock.patch.object(
                        upload_hardware_profile,
                        "upload_hardware_profile",
                        return_value=FakeResponse(200, {}, text="ok"),
                    ) as upload_mock:
                        with mock.patch.object(sys, "argv", ["upload-hardware-profile", "--transport", "setup"]):
                            with contextlib.redirect_stdout(io.StringIO()):
                                rc = upload_hardware_profile.main()

        self.assertEqual(rc, 0)
        upload_mock.assert_called_once_with(
            "10.0.2.2",
            "18765",
            "peer-123",
            "secret",
            {"hostname": "node-1"},
        )

    def test_upload_hardware_profile_auto_falls_back_to_setup_after_connect_failure(self):
        with mock.patch.object(
            upload_hardware_profile,
            "read_network_conf",
            return_value={
                "SERVER_WG_IP": "10.254.0.1",
                "SERVER_PORT": "8765",
                "SETUP_API_HOST": "10.0.2.2",
                "SETUP_API_PORT": "18765",
                "PEER_UUID": "peer-123",
            },
        ):
            with mock.patch.object(upload_hardware_profile, "read_api_password", return_value="secret"):
                with mock.patch.object(
                    upload_hardware_profile,
                    "build_hardware_profile_payload",
                    return_value={"hostname": "node-1"},
                ):
                    with mock.patch.object(
                        upload_hardware_profile,
                        "upload_hardware_profile",
                        side_effect=[Exception("timed out"), FakeResponse(200, {}, text="ok")],
                    ) as upload_mock:
                        with mock.patch.object(sys, "argv", ["upload-hardware-profile"]):
                            with contextlib.redirect_stdout(io.StringIO()):
                                rc = upload_hardware_profile.main()

        self.assertEqual(rc, 0)
        self.assertEqual(upload_mock.call_count, 2)
        self.assertEqual(upload_mock.call_args_list[0].args[0:2], ("10.254.0.1", "8765"))
        self.assertEqual(upload_mock.call_args_list[1].args[0:2], ("10.0.2.2", "18765"))

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

    def test_config_location_send_location_uses_put(self):
        response = FakeResponse(200, {}, text="ok")
        fake_requests = SimpleNamespace(put=mock.Mock(return_value=response))
        with mock.patch.object(config_location, "http_requests", return_value=fake_requests):
            with contextlib.redirect_stdout(io.StringIO()):
                config_location.send_location(
                    "10.0.2.2",
                    "18765",
                    "peer-123",
                    "secret",
                    30.0,
                    -90.0,
                )

        fake_requests.put.assert_called_once()
        self.assertEqual(
            fake_requests.put.call_args.args[0],
            "http://10.0.2.2:18765/api/v1/client/peer/location",
        )
        self.assertEqual(
            fake_requests.put.call_args.kwargs["json"],
            {"latitude": 30.0, "longitude": -90.0},
        )

    def test_config_location_accepts_setup_server_and_setup_port_aliases(self):
        argv = [
            "config-location",
            "--latitude",
            "30",
            "--longitude",
            "-90",
            "--setup-server",
            "10.0.2.2",
            "--setup-port",
            "18765",
        ]
        with mock.patch.object(config_location, "write_local_location_conf"):
            with mock.patch.object(
                config_location,
                "read_network_conf",
                return_value={"PEER_UUID": "peer-123"},
            ):
                with mock.patch.object(config_location, "read_api_password", return_value="secret"):
                    with mock.patch.object(config_location, "send_location") as send_mock:
                        with mock.patch.object(config_location, "ensure_sensos_admin"):
                            with mock.patch.object(sys, "argv", argv):
                                rc = config_location.main()

        self.assertEqual(rc, 0)
        send_mock.assert_called_once_with(
            "10.0.2.2",
            "18765",
            "peer-123",
            "secret",
            30.0,
            -90.0,
        )

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


class LiveApiContractTests(unittest.TestCase):
    @staticmethod
    def _live_env():
        required = [
            "SENSOS_CONTRACT_SETUP_HOST",
            "SENSOS_CONTRACT_SETUP_PORT",
            "SENSOS_CONTRACT_CLIENT_PASSWORD",
        ]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise unittest.SkipTest(
                "Live contract test skipped; missing env: " + ", ".join(missing)
            )
        return {
            "host": os.environ["SENSOS_CONTRACT_SETUP_HOST"],
            "port": os.environ["SENSOS_CONTRACT_SETUP_PORT"],
            "client_password": os.environ["SENSOS_CONTRACT_CLIENT_PASSWORD"],
            "admin_user": os.environ.get("SENSOS_CONTRACT_ADMIN_USER", "sensos"),
        }

    @staticmethod
    def _admin_password() -> str:
        if not sys.stdin.isatty():
            raise unittest.SkipTest(
                "Live contract test skipped; requires TTY prompt for admin password."
            )
        password = getpass.getpass("SensOS admin API password: ").strip()
        if not password:
            raise unittest.SkipTest("Live contract test skipped; empty admin password.")
        return password

    @staticmethod
    def _auth_header(username: str, password: str) -> dict:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def test_live_enroll_and_peer_endpoints_are_idempotent_with_cleanup(self):
        env = self._live_env()
        admin_password = self._admin_password()
        requests = utils.require_requests()
        base = f"http://{env['host']}:{env['port']}"
        suffix = f"{int(time.time())}-{random.randint(1000, 9999)}"
        test_network = f"contract-{suffix}"
        note = f"contract-test-{suffix}"
        peer_ip = None
        network_created = False

        create_network_resp = requests.post(
            f"{base}/api/v1/admin/networks",
            json={
                "name": test_network,
                "wg_public_ip": env["host"],
                "wg_port": random.randint(52000, 52999),
            },
            headers=self._auth_header(env["admin_user"], admin_password),
            timeout=15,
        )
        self.assertEqual(
            create_network_resp.status_code,
            200,
            f"create network failed: {create_network_resp.status_code} {create_network_resp.text}",
        )
        network_created = True

        enroll_resp = requests.post(
            f"{base}/api/v1/client/peers/enroll",
            json={"network_name": test_network, "subnet_offset": 1, "note": note},
            headers=self._auth_header("sensos", env["client_password"]),
            timeout=10,
        )
        self.assertEqual(
            enroll_resp.status_code,
            200,
            f"enroll failed: {enroll_resp.status_code} {enroll_resp.text}",
        )
        enroll = enroll_resp.json()
        wg_ip = enroll["wg_ip"]
        peer_ip = wg_ip
        peer_uuid = enroll["peer_uuid"]
        peer_api_password = enroll["peer_api_password"]

        try:
            wg_resp = requests.post(
                f"{base}/api/v1/client/peer/wireguard-key",
                json={"wg_public_key": "test-live-contract-public-key"},
                headers=self._auth_header(peer_uuid, peer_api_password),
                timeout=10,
            )
            self.assertEqual(
                wg_resp.status_code,
                200,
                f"register-wireguard-key failed: {wg_resp.status_code} {wg_resp.text}",
            )

            location_resp = requests.put(
                f"{base}/api/v1/client/peer/location",
                json={"latitude": 30.2672, "longitude": -97.7431},
                headers=self._auth_header(peer_uuid, peer_api_password),
                timeout=10,
            )
            self.assertEqual(
                location_resp.status_code,
                200,
                f"location update failed: {location_resp.status_code} {location_resp.text}",
            )

            status_resp = requests.post(
                f"{base}/api/v1/client/peer/status",
                json={
                    "hostname": "contract-test-node",
                    "uptime_seconds": 1,
                    "disk_available_gb": 1.0,
                    "memory_used_mb": 10,
                    "memory_total_mb": 100,
                    "load_1m": 0.01,
                    "load_5m": 0.01,
                    "load_15m": 0.01,
                    "version": "test",
                    "status_message": "contract-test",
                },
                headers=self._auth_header(peer_uuid, peer_api_password),
                timeout=10,
            )
            self.assertEqual(
                status_resp.status_code,
                200,
                f"status update failed: {status_resp.status_code} {status_resp.text}",
            )

            hardware_resp = requests.put(
                f"{base}/api/v1/client/peer/hardware-profile",
                json={
                    "hostname": "contract-test-node",
                    "model": "contract-test-model",
                    "kernel_version": "test-kernel",
                    "cpu": {"model_name": "test-cpu"},
                    "firmware": {"bios_version": "test-bios"},
                    "memory": {"mem_total_mb": 100},
                    "disks": {"sda": {"path": "/dev/sda"}},
                    "usb_devices": "",
                    "network_interfaces": {"eth0": {"ipv4": []}},
                },
                headers=self._auth_header(peer_uuid, peer_api_password),
                timeout=10,
            )
            self.assertEqual(
                hardware_resp.status_code,
                200,
                f"hardware upload failed: {hardware_resp.status_code} {hardware_resp.text}",
            )
        finally:
            if network_created:
                delete_network_resp = requests.delete(
                    f"{base}/api/v1/admin/networks/{test_network}",
                    headers=self._auth_header(env["admin_user"], admin_password),
                    timeout=15,
                )
                self.assertIn(
                    delete_network_resp.status_code,
                    (200, 404),
                    (
                        f"network cleanup failed for {test_network}: "
                        f"{delete_network_resp.status_code} {delete_network_resp.text}"
                    ),
                )
                if peer_ip:
                    peer_lookup = requests.get(
                        f"{base}/api/v1/admin/peers/{peer_ip}",
                        headers=self._auth_header(env["admin_user"], admin_password),
                        timeout=10,
                    )
                    self.assertEqual(
                        peer_lookup.status_code,
                        200,
                        f"peer lookup failed: {peer_lookup.status_code} {peer_lookup.text}",
                    )
                    body = peer_lookup.json()
                    self.assertEqual(
                        body.get("exists"),
                        False,
                        f"peer still present after network delete: {peer_ip} -> {body}",
                    )


if __name__ == "__main__":
    unittest.main()

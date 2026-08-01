from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SMOKE_ROOT = Path(__file__).resolve().parents[1]
if str(SMOKE_ROOT) not in sys.path:
    sys.path.insert(0, str(SMOKE_ROOT))

from harness.environment import load_environment  # noqa: E402
from harness.registry import make_scenario  # noqa: E402


class DeviceFlowScenarioContractTest(unittest.TestCase):
    def test_scenario_is_registered(self):
        scenario = make_scenario("device.pair_and_deliver_animation")
        self.assertEqual(scenario.name, "device.pair_and_deliver_animation")

    def test_device_flow_environment_is_loaded_from_process_without_secrets(self):
        values = {
            "BABYMILU_SMOKE_ENVIRONMENT_TYPE": "cloud",
            "BABYMILU_SMOKE_DATA_MODE": "live-shape",
            "BABYMILU_SMOKE_PROJECT": "example-project",
            "BABYMILU_SMOKE_FIRESTORE_DATABASE": "development",
            "BABYMILU_SMOKE_DEVICE_API_URL": "https://device.example",
            "BABYMILU_SMOKE_USER_API_URL": "https://user.example",
            "BABYMILU_SMOKE_CHARACTER_API_URL": "https://character.example",
            "BABYMILU_SMOKE_DEVICE_BIN_URL": "https://bin.example",
            "BABYMILU_SMOKE_DEVICE_ASSETS_BUCKET": "milu-public-new",
        }
        with patch.dict(os.environ, values, clear=True):
            environment = load_environment("missing-device-test-config")

        self.assertEqual(environment.firestore_database, "development")
        self.assertEqual(environment.device_api_url, "https://device.example")
        self.assertEqual(environment.user_api_url, "https://user.example")
        self.assertEqual(environment.character_api_url, "https://character.example")
        self.assertEqual(environment.device_bin_url, "https://bin.example")
        self.assertEqual(environment.device_assets_bucket, "milu-public-new")


if __name__ == "__main__":
    unittest.main()

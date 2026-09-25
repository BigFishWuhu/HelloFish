import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from cloud_sync import CloudSyncClient, CloudSyncConfig  # noqa: E402


class CloudSyncConfigTest(unittest.TestCase):
    def test_nested_configuration_and_string_switch(self) -> None:
        config = CloudSyncConfig.from_params(
            {
                "upload_cloud": "true",
                "cloud_upload": {"url": "https://example.test", "timeout": "bad"},
            }
        )
        self.assertTrue(config.enabled)
        self.assertEqual(config.url, "https://example.test")
        self.assertEqual(config.timeout, 10.0)
        self.assertEqual(CloudSyncClient(config).endpoint, "https://example.test/api/records/upload")

    def test_missing_url_is_rejected_only_when_client_is_created(self) -> None:
        config = CloudSyncConfig.from_params({"upload_cloud": True})
        self.assertTrue(config.enabled)
        with self.assertRaises(ValueError):
            CloudSyncClient(config)

    def test_batch_upload_rejects_more_than_server_limit(self) -> None:
        client = CloudSyncClient(
            CloudSyncConfig(enabled=True, url="https://example.test")
        )
        self.assertEqual(client.upload_many([]), {"saved": 0})
        with self.assertRaisesRegex(ValueError, "500"):
            client.upload_many([{}] * 501)


if __name__ == "__main__":
    unittest.main()

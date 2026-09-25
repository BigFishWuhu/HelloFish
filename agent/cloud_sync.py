"""Upload locally captured contribution records to a HelloFish cloud service."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class CloudSyncConfig:
    enabled: bool = False
    url: str = ""
    username: str = ""
    password: str = ""
    token: str = ""
    timeout: float = 10.0
    retries: int = 2

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "CloudSyncConfig":
        raw = params.get("cloud_upload")
        nested = raw if isinstance(raw, Mapping) else {}
        raw_enabled = params.get("upload_cloud", params.get("cloud_enabled", nested.get("enabled", False)))
        enabled = raw_enabled if isinstance(raw_enabled, bool) else str(raw_enabled).strip().lower() in {"1", "true", "yes", "on"}
        url = str(
            params.get("cloud_url", nested.get("url", params.get("upload_url", "")))
            or ""
        ).strip()
        username = str(params.get("cloud_username", nested.get("username", "")) or "")
        password = str(params.get("cloud_password", nested.get("password", "")) or "")
        token = str(params.get("cloud_token", nested.get("token", "")) or "")
        try:
            timeout_value = float(params.get("cloud_timeout", nested.get("timeout", 10)))
        except (TypeError, ValueError):
            timeout_value = 10.0
        try:
            retries_value = int(params.get("cloud_retries", nested.get("retries", 2)))
        except (TypeError, ValueError):
            retries_value = 2
        timeout = max(1.0, min(60.0, timeout_value))
        retries = max(0, min(5, retries_value))
        return cls(enabled, url.rstrip("/"), username, password, token, timeout, retries)


class CloudSyncClient:
    def __init__(self, config: CloudSyncConfig):
        if not config.url:
            raise ValueError("启用云端上传时必须配置 cloud_url")
        self.config = config
        self.endpoint = (
            config.url if config.url.endswith("/api/records/upload")
            else config.url + "/api/records/upload"
        )

    def upload(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return self.upload_many([record])

    def upload_many(self, records: list[Mapping[str, Any]]) -> dict[str, Any]:
        if not records:
            return {"saved": 0}
        if len(records) > 500:
            raise ValueError("单次云端上传不能超过 500 条记录")
        payload = json.dumps(
            {"records": [dict(record) for record in records]},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "HelloFish-Desktop/1",
        }
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        elif self.config.username:
            value = f"{self.config.username}:{self.config.password}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(value).decode("ascii")
        request = Request(self.endpoint, data=payload, headers=headers, method="POST")
        last_error: Exception | None = None
        for attempt in range(self.config.retries + 1):
            try:
                with urlopen(request, timeout=self.config.timeout) as response:  # noqa: S310
                    body = response.read().decode("utf-8")
                value = json.loads(body) if body else {}
                if not isinstance(value, dict):
                    raise ValueError("云端返回格式错误")
                return value
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.config.retries:
                    time.sleep(0.25 * (attempt + 1))
        raise RuntimeError(f"云端上传失败：{last_error}") from last_error

"""Config resolution: env-first for `ewcp.kernel_url` / `ewcp.kernel_api_key`."""

from __future__ import annotations

import pytest

from ewcp_core.kernel_client import KernelClientConfig


def test_env_wins_over_plugin_config() -> None:
    cfg = KernelClientConfig.resolve(
        {"kernel_url": "http://config-url", "kernel_api_key": "config-key"},
        env={
            "EWCP_KERNEL_URL": "http://env-url",
            "EWCP_KERNEL_API_KEY": "env-key",
        },
    )
    assert cfg.kernel_url == "http://env-url"
    assert cfg.api_key == "env-key"


def test_plugin_config_used_when_env_absent() -> None:
    cfg = KernelClientConfig.resolve(
        {"kernel_url": "http://config-url", "kernel_api_key": "config-key"},
        env={},
    )
    assert cfg.kernel_url == "http://config-url"
    assert cfg.api_key == "config-key"


def test_unset_leaves_none() -> None:
    cfg = KernelClientConfig.resolve({}, env={})
    assert cfg.kernel_url is None
    assert cfg.api_key is None


def test_timeout_and_attempts_configurable() -> None:
    cfg = KernelClientConfig.resolve(
        {"kernel_url": "http://k", "timeout_seconds": 5, "read_max_attempts": 2},
        env={},
    )
    assert cfg.timeout == 5
    assert cfg.max_attempts == 2


def test_empty_env_string_does_not_override() -> None:
    cfg = KernelClientConfig.resolve(
        {"kernel_url": "http://config-url"},
        env={"EWCP_KERNEL_URL": ""},
    )
    assert cfg.kernel_url == "http://config-url"


def test_resolve_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EWCP_KERNEL_URL", "http://from-env")
    monkeypatch.setenv("EWCP_KERNEL_API_KEY", "env-key")
    cfg = KernelClientConfig.resolve({})
    assert cfg.kernel_url == "http://from-env"
    assert cfg.api_key == "env-key"

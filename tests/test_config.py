from __future__ import annotations

import pytest

from durable_mcp.config import ConfigError, load_config, sanitize_tool_name

BASE = {"allow_insecure": "1"}


def _svc(env: dict[str, str], n: int, name: str, url: str = "http://svc/x") -> None:
    env[f"service_{n}_url"] = url
    env[f"service_{n}_name"] = name
    env[f"service_{n}_description"] = f"desc {n}"


def _apply(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key in list(env):
        monkeypatch.setenv(key, env[key])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Order Taco", "order_taco"),
        ("  Send Receipt  ", "send_receipt"),
        ("Re-stock Inventory!", "re-stock_inventory"),
        ("COFFEE", "coffee"),
    ],
)
def test_sanitize_tool_name(raw: str, expected: str) -> None:
    assert sanitize_tool_name(raw) == expected


def test_sanitize_rejects_unusable_name() -> None:
    with pytest.raises(ConfigError):
        sanitize_tool_name("!!!")


def test_scans_services_and_stops_at_first_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    env = dict(BASE)
    _svc(env, 1, "Order Taco")
    _svc(env, 2, "Order Coffee")
    # No service_3_*; service_4_* must therefore be ignored rather than silently
    # renumbered, so a mis-indexed variable is visible as a missing tool.
    _svc(env, 4, "Send Receipt")
    _apply(monkeypatch, env)

    config = load_config()
    assert [s.tool_name for s in config.services] == ["order_taco", "order_coffee"]
    assert config.service_for("order_coffee") is not None
    assert config.service_for("send_receipt") is None


def test_partial_service_definition_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    env = dict(BASE)
    _svc(env, 1, "Order Taco")
    env["service_2_url"] = "http://svc/coffee"  # name and description missing
    _apply(monkeypatch, env)

    with pytest.raises(ConfigError, match="service_2_name"):
        load_config()


def test_colliding_tool_names_refuse_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    env = dict(BASE)
    _svc(env, 1, "Order Taco")
    _svc(env, 2, "order taco")
    _apply(monkeypatch, env)

    with pytest.raises(ConfigError, match="collides"):
        load_config()


def test_rejects_non_http_url(monkeypatch: pytest.MonkeyPatch) -> None:
    env = dict(BASE)
    _svc(env, 1, "Order Taco", url="taco.railway.internal/taco")
    _apply(monkeypatch, env)

    with pytest.raises(ConfigError, match="http://"):
        load_config()


def test_refuses_to_boot_without_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    env: dict[str, str] = {}
    _svc(env, 1, "Order Taco")
    _apply(monkeypatch, env)
    monkeypatch.delenv("allow_insecure", raising=False)

    with pytest.raises(ConfigError, match="mcp_agent_tokens"):
        load_config()


def test_agent_tokens_parse_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"mcp_agent_tokens": "melissa:tok-a, ian:tok-b, tok-c", "admin_token": "adm"}
    _svc(env, 1, "Order Taco")
    _apply(monkeypatch, env)

    config = load_config()
    assert config.agent_tokens == {"tok-a": "melissa", "tok-b": "ian", "tok-c": "agent"}
    assert config.admin_token == "adm"


def test_uppercase_env_vars_also_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_1_URL", "http://svc/taco")
    monkeypatch.setenv("SERVICE_1_NAME", "Order Taco")
    monkeypatch.setenv("SERVICE_1_DESCRIPTION", "d")
    monkeypatch.setenv("ALLOW_INSECURE", "1")

    assert [s.tool_name for s in load_config().services] == ["order_taco"]

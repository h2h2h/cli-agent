from pathlib import Path

import pytest

from cli_agent.runtime._capability.mcp.facts import (
    load_server_config,
    parse_server_config,
)


def test_parse_accepts_a_valid_stdio_config() -> None:
    config, errors = parse_server_config(
        {
            "name": "github",
            "transport": "stdio",
            "command": ["npx", "-y", "@modelcontextprotocol/server-github"],
            "env": ["GITHUB_TOKEN"],
        },
        directory_name="github",
    )
    assert errors == ()
    assert config is not None
    assert config.name == "github"
    assert config.transport == "stdio"
    assert config.command == (
        "npx",
        "-y",
        "@modelcontextprotocol/server-github",
    )
    assert config.url is None
    assert config.env == ("GITHUB_TOKEN",)
    assert config.headers == ()


def test_parse_accepts_a_valid_http_config() -> None:
    config, errors = parse_server_config(
        {
            "name": "weather",
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "WEATHER_TOKEN"},
        },
        directory_name="weather",
    )
    assert errors == ()
    assert config is not None
    assert config.transport == "http"
    assert config.command is None
    assert config.url == "https://example.com/mcp"
    assert config.headers == (("Authorization", "WEATHER_TOKEN"),)


def test_parse_records_env_names_not_values() -> None:
    config, errors = parse_server_config(
        {
            "name": "github",
            "transport": "stdio",
            "command": ["npx", "server"],
            "env": ["GITHUB_TOKEN"],
        },
        directory_name="github",
    )
    assert errors == ()
    assert config is not None
    assert config.env == ("GITHUB_TOKEN",)
    assert "ghp_" not in repr(config)


def test_parse_rejects_a_non_object_config() -> None:
    config, errors = parse_server_config(["npx", "server"], directory_name="x")
    assert config is None
    assert any("must be a JSON object" in error for error in errors)


def test_parse_rejects_unknown_fields() -> None:
    config, errors = parse_server_config(
        {
            "name": "x",
            "transport": "stdio",
            "command": ["npx"],
            "extra": 1,
        },
        directory_name="x",
    )
    assert config is None
    assert any("extra" in error for error in errors)


def test_parse_rejects_missing_required_fields() -> None:
    config, errors = parse_server_config({"name": "x"}, directory_name="x")
    assert config is None
    assert any("transport" in error for error in errors)


def test_parse_rejects_an_invalid_transport() -> None:
    config, errors = parse_server_config(
        {"name": "x", "transport": "sse"},
        directory_name="x",
    )
    assert config is None
    assert any("sse" in error for error in errors)


def test_parse_rejects_invalid_name_rules() -> None:
    cases = (
        ("Github", "must be lowercase"),
        ("x--y", "consecutive hyphens"),
        ("-x", "start or end with a hyphen"),
        ("x_name", "only letters, digits, and hyphens"),
    )
    for name, expected in cases:
        config, errors = parse_server_config(
            {"name": name, "transport": "stdio", "command": ["npx"]},
            directory_name=name,
        )
        assert config is None
        assert any(expected in error for error in errors), (name, errors)


def test_parse_requires_name_matches_directory() -> None:
    config, errors = parse_server_config(
        {"name": "other", "transport": "stdio", "command": ["npx"]},
        directory_name="github",
    )
    assert config is None
    assert any("must match the directory name" in error for error in errors)


def test_parse_rejects_name_over_length_limit() -> None:
    long_name = "a" * 65
    config, errors = parse_server_config(
        {"name": long_name, "transport": "stdio", "command": ["npx"]},
        directory_name=long_name,
    )
    assert config is None
    assert any("character limit" in error for error in errors)


def test_parse_stdio_requires_command() -> None:
    config, errors = parse_server_config(
        {"name": "x", "transport": "stdio"},
        directory_name="x",
    )
    assert config is None
    assert any("command" in error for error in errors)


def test_parse_http_requires_url() -> None:
    config, errors = parse_server_config(
        {"name": "x", "transport": "http"},
        directory_name="x",
    )
    assert config is None
    assert any("url" in error for error in errors)


def test_parse_rejects_non_string_command_items() -> None:
    config, errors = parse_server_config(
        {"name": "x", "transport": "stdio", "command": ["npx", 3]},
        directory_name="x",
    )
    assert config is None
    assert errors


def test_load_server_config_reads_a_valid_file(tmp_path: Path) -> None:
    directory = tmp_path / "github"
    directory.mkdir()
    path = directory / "config.json"
    path.write_text(
        '{"name": "github", "transport": "stdio", '
        '"command": ["npx", "server"]}\n',
        encoding="utf-8",
    )
    config, errors = load_server_config(path)
    assert errors == ()
    assert config is not None
    assert config.name == "github"


def test_load_server_config_rejects_invalid_json(tmp_path: Path) -> None:
    directory = tmp_path / "github"
    directory.mkdir()
    path = directory / "config.json"
    path.write_text("not json", encoding="utf-8")
    config, errors = load_server_config(path)
    assert config is None
    assert any("not readable JSON" in error for error in errors)


def test_config_is_frozen() -> None:
    config, errors = parse_server_config(
        {"name": "x", "transport": "stdio", "command": ["npx"]},
        directory_name="x",
    )
    assert errors == ()
    assert config is not None
    with pytest.raises(AttributeError):
        config.name = "other"  # type: ignore[misc]

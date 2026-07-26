import tomllib
from pathlib import Path

import httpx
import pytest

import cli_agent.config as config_module
from cli_agent.cli import main
from cli_agent.config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    CliConfig,
    CliConfigurationError,
    build_provider,
    parse_cli_config,
)
from cli_agent.runtime import OpenAICompatibleModelProvider


def test_parses_default_cli_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = parse_cli_config(
        ["Inspect the Workspace", "--model", "test-model"],
        environ={DEFAULT_API_KEY_ENV: "default-secret"},
    )

    assert config == CliConfig(
        task="Inspect the Workspace",
        workspace=tmp_path.resolve(),
        base_url=DEFAULT_BASE_URL,
        model="test-model",
        api_key_env=DEFAULT_API_KEY_ENV,
        api_key="default-secret",
    )
    assert "default-secret" not in repr(config)


def test_parses_and_normalizes_cli_overrides(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = parse_cli_config(
        [
            "Run tests",
            "--workspace",
            str(workspace),
            "--base-url",
            "http://localhost:8080/v1/",
            "--model",
            "compatible-model",
            "--api-key-env",
            "COMPATIBLE_API_KEY",
        ],
        environ={"COMPATIBLE_API_KEY": "compatible-secret"},
    )

    assert config == CliConfig(
        task="Run tests",
        workspace=workspace.resolve(),
        base_url="http://localhost:8080/v1",
        model="compatible-model",
        api_key_env="COMPATIBLE_API_KEY",
        api_key="compatible-secret",
    )


def test_reports_missing_api_key_environment_variable(tmp_path: Path) -> None:
    with pytest.raises(
        CliConfigurationError,
        match=f"{DEFAULT_API_KEY_ENV} is not set",
    ):
        parse_cli_config(
            [
                "Inspect",
                "--workspace",
                str(tmp_path),
                "--model",
                "test-model",
            ],
            environ={},
        )


@pytest.mark.parametrize("workspace_kind", ("missing", "file"))
def test_rejects_invalid_workspace(
    tmp_path: Path,
    workspace_kind: str,
) -> None:
    workspace = tmp_path / workspace_kind
    if workspace_kind == "file":
        workspace.write_text("not a directory")

    with pytest.raises(CliConfigurationError, match="existing directory"):
        parse_cli_config(
            [
                "Inspect",
                "--workspace",
                str(workspace),
                "--model",
                "test-model",
            ],
            environ={DEFAULT_API_KEY_ENV: "secret"},
        )


def test_rejects_invalid_base_url_without_exposing_secret(
    tmp_path: Path,
) -> None:
    secret = "must-not-appear"

    with pytest.raises(CliConfigurationError) as raised:
        parse_cli_config(
            [
                "Inspect",
                "--workspace",
                str(tmp_path),
                "--base-url",
                "models.example/v1?key=visible",
                "--model",
                "test-model",
            ],
            environ={DEFAULT_API_KEY_ENV: secret},
        )

    assert "base URL" in str(raised.value)
    assert secret not in str(raised.value)


def test_main_prints_concise_configuration_error_without_secret(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    secret = "must-not-be-logged"
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, secret)

    exit_code = main(
        [
            "Inspect",
            "--workspace",
            str(tmp_path / "missing"),
            "--model",
            "test-model",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "cli-agent: workspace must be an existing directory" in captured.err
    assert secret not in captured.err


def test_builds_provider_separately_from_cli_parsing(
    tmp_path: Path,
) -> None:
    config = CliConfig(
        task="Inspect",
        workspace=tmp_path,
        base_url="https://models.example/v1",
        model="test-model",
        api_key_env="MODEL_API_KEY",
        api_key="secret",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500),
    )

    provider = build_provider(config, transport=transport)

    assert isinstance(provider, OpenAICompatibleModelProvider)


def test_declares_cli_agent_console_entry_point() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text())

    assert project["project"]["name"] == "cli-agent"
    assert project["project"]["scripts"]["cli-agent"] == "cli_agent.cli:main"


def test_declares_direnv_local_credentials_convention() -> None:
    project_root = Path(__file__).parents[1]

    assert (
        project_root.joinpath(".envrc").read_text()
        == "# Use the uv-managed project environment and keep secrets "
        "in the ignored file.\n"
        "PATH_add .venv/bin\n"
        "source_env_if_exists .envrc.local\n"
    )
    assert (
        ".envrc.local" in project_root.joinpath(".gitignore").read_text().splitlines()
    )


def test_main_uses_validated_config_to_build_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class RecordingProvider:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        config_module,
        "OpenAICompatibleModelProvider",
        RecordingProvider,
    )
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, "secret")

    exit_code = main(
        [
            "Inspect",
            "--workspace",
            str(tmp_path),
            "--model",
            "test-model",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "model": "test-model",
        "api_key": "secret",
        "base_url": DEFAULT_BASE_URL,
        "transport": None,
    }

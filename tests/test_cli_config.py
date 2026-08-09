import tomllib
from pathlib import Path

import httpx
import pytest

import cli_agent.cli as cli_module
import cli_agent.config as config_module
from cli_agent.cli import main
from cli_agent.config import (
    API_KEY_ENV,
    BASE_URL_ENV,
    CONTEXT_SAFETY_MARGIN_ENV,
    CONTEXT_WINDOW_ENV,
    DEFAULT_CONTEXT_SAFETY_MARGIN,
    DEFAULT_OUTPUT_RESERVE,
    MODEL_ENV,
    OUTPUT_RESERVE_ENV,
    CliConfig,
    CliConfigurationError,
    build_context_policy,
    build_provider,
    parse_cli_config,
)
from cli_agent.runtime import ContextPolicy, OpenAICompatibleModelProvider


def test_parses_direnv_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = parse_cli_config(
        ["Inspect the Workspace"],
        environ=_environment(),
    )

    assert config == CliConfig(
        task="Inspect the Workspace",
        workspace=tmp_path.resolve(),
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=DEFAULT_CONTEXT_SAFETY_MARGIN,
    )
    assert "secret" not in repr(config)


def test_parses_workspace_override_and_normalizes_base_url(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = parse_cli_config(
        [
            "Run tests",
            "--workspace",
            str(workspace),
        ],
        environ=_environment(base_url="http://localhost:8080/v1/"),
    )

    assert config == CliConfig(
        task="Run tests",
        workspace=workspace.resolve(),
        base_url="http://localhost:8080/v1",
        model="test-model",
        api_key="secret",
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=DEFAULT_CONTEXT_SAFETY_MARGIN,
    )


def test_parses_optional_repertoire_override(tmp_path: Path) -> None:
    repertoire = tmp_path / "user-repertoire"

    config = parse_cli_config(
        [
            "Inspect capabilities",
            "--workspace",
            str(tmp_path),
            "--repertoire",
            str(repertoire),
        ],
        environ=_environment(),
    )

    assert config.repertoire == repertoire.resolve()


def test_parses_interactive_session_without_task(tmp_path: Path) -> None:
    config = parse_cli_config(
        [
            "--workspace",
            str(tmp_path),
        ],
        environ=_environment(),
    )

    assert config == CliConfig(
        task=None,
        workspace=tmp_path.resolve(),
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=DEFAULT_CONTEXT_SAFETY_MARGIN,
    )


def test_loads_user_environment_from_home_dotenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".cli-agent"
    config_dir.mkdir(parents=True)
    config_dir.joinpath(".env").write_text(
        "".join(
            f"export {name}={value}\n"
            for name, value in _environment().items()
        )
    )
    monkeypatch.setenv("HOME", str(home))
    for name in _environment():
        monkeypatch.delenv(name, raising=False)

    config = parse_cli_config(
        ["Inspect", "--workspace", str(tmp_path)],
    )

    assert config == CliConfig(
        task="Inspect",
        workspace=tmp_path.resolve(),
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=DEFAULT_CONTEXT_SAFETY_MARGIN,
    )


def test_process_environment_wins_over_user_dotenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".cli-agent"
    config_dir.mkdir(parents=True)
    config_dir.joinpath(".env").write_text(
        f'export {MODEL_ENV}="from-dotenv"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(MODEL_ENV, raising=False)
    _set_environment(monkeypatch, model="from-process")

    config = parse_cli_config(
        ["Inspect", "--workspace", str(tmp_path)],
    )

    assert config.model == "from-process"
    assert config.api_key == "secret"


def test_rejects_explicit_empty_task(tmp_path: Path) -> None:
    with pytest.raises(CliConfigurationError, match="task must not be empty"):
        parse_cli_config(
            [
                "  ",
                "--workspace",
                str(tmp_path),
            ],
            environ=_environment(),
        )


@pytest.mark.parametrize(
    "missing_name",
    (MODEL_ENV, BASE_URL_ENV, API_KEY_ENV),
)
def test_reports_missing_required_environment_variable(
    tmp_path: Path,
    missing_name: str,
) -> None:
    environment = _environment()
    environment.pop(missing_name)

    with pytest.raises(
        CliConfigurationError,
        match=f"{missing_name} is not set",
    ):
        parse_cli_config(
            [
                "Inspect",
                "--workspace",
                str(tmp_path),
            ],
            environ=environment,
        )


def test_resolves_context_window_from_model_registry_when_unset(
    tmp_path: Path,
) -> None:
    environment = _environment(model="deepseek-v4-flash")
    environment.pop(CONTEXT_WINDOW_ENV)

    config = parse_cli_config(
        [
            "Inspect",
            "--workspace",
            str(tmp_path),
        ],
        environ=environment,
    )

    assert config.context_window_tokens == 1_000_000
    assert config.model == "deepseek-v4-flash"


def test_explicit_context_window_overrides_model_registry(tmp_path: Path) -> None:
    config = parse_cli_config(
        [
            "Inspect",
            "--workspace",
            str(tmp_path),
        ],
        environ=_environment(
            model="deepseek-v4-flash",
            context_window="128000",
        ),
    )

    assert config.context_window_tokens == 128_000


def test_rejects_unknown_model_without_explicit_context_window(
    tmp_path: Path,
) -> None:
    environment = _environment(model="unknown-model")
    environment.pop(CONTEXT_WINDOW_ENV)

    with pytest.raises(
        CliConfigurationError,
        match="no known maximum context window",
    ):
        parse_cli_config(
            [
                "Inspect",
                "--workspace",
                str(tmp_path),
            ],
            environ=environment,
        )


def test_output_reserve_defaults_to_builtin_value(tmp_path: Path) -> None:
    environment = _environment()
    environment.pop(OUTPUT_RESERVE_ENV)

    config = parse_cli_config(
        [
            "Inspect",
            "--workspace",
            str(tmp_path),
        ],
        environ=environment,
    )

    assert config.output_reserve_tokens == DEFAULT_OUTPUT_RESERVE


@pytest.mark.parametrize(
    ("name", "value", "error_match"),
    (
        (CONTEXT_WINDOW_ENV, "not-an-int", "must be an integer"),
        (CONTEXT_WINDOW_ENV, "0", "must be a positive integer"),
        (CONTEXT_WINDOW_ENV, "-100", "must be a positive integer"),
        (OUTPUT_RESERVE_ENV, "not-an-int", "must be an integer"),
        (OUTPUT_RESERVE_ENV, "0", "must be a positive integer"),
        (OUTPUT_RESERVE_ENV, "-100", "must be a positive integer"),
        (
            CONTEXT_SAFETY_MARGIN_ENV,
            "not-an-int",
            "must be an integer",
        ),
        (
            CONTEXT_SAFETY_MARGIN_ENV,
            "-1",
            "must be a non-negative integer",
        ),
    ),
)
def test_rejects_invalid_context_token_environment_variables(
    tmp_path: Path,
    name: str,
    value: str,
    error_match: str,
) -> None:
    environment = _environment()
    environment[name] = value

    with pytest.raises(CliConfigurationError, match=error_match):
        parse_cli_config(
            [
                "Inspect",
                "--workspace",
                str(tmp_path),
            ],
            environ=environment,
        )


def test_rejects_zero_input_budget_before_runtime_open(tmp_path: Path) -> None:
    with pytest.raises(
        CliConfigurationError,
        match="context input budget must be positive",
    ):
        parse_cli_config(
            [
                "Inspect",
                "--workspace",
                str(tmp_path),
            ],
            environ=_environment(
                context_window="4000",
                output_reserve="4000",
            ),
        )


def test_accepts_explicit_context_safety_margin_override(tmp_path: Path) -> None:
    config = parse_cli_config(
        [
            "Inspect",
            "--workspace",
            str(tmp_path),
        ],
        environ=_environment(safety_margin="1_024"),
    )

    assert config.safety_margin_tokens == 1_024


def test_default_output_reserve_and_safety_margin_are_fixed() -> None:
    assert DEFAULT_OUTPUT_RESERVE == 16_384
    assert DEFAULT_CONTEXT_SAFETY_MARGIN == 4_096


def test_builds_explicit_context_policy_from_validated_config(
    tmp_path: Path,
) -> None:
    config = CliConfig(
        task="Inspect",
        workspace=tmp_path,
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=1_024,
    )

    policy = build_context_policy(config)

    assert policy == ContextPolicy(
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=1_024,
    )
    assert policy.input_budget == 122_976


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
            ],
            environ=_environment(),
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
            ],
            environ=_environment(
                base_url="models.example/v1?key=visible",
                api_key=secret,
            ),
        )

    assert "base URL" in str(raised.value)
    assert secret not in str(raised.value)


def test_main_prints_concise_configuration_error_without_secret(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    secret = "must-not-be-logged"
    _set_environment(monkeypatch, api_key=secret)

    exit_code = main(
        [
            "Inspect",
            "--workspace",
            str(tmp_path / "missing"),
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
        api_key="secret",
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
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


def test_declares_direnv_configuration_convention() -> None:
    project_root = Path(__file__).parents[1]
    template = project_root.joinpath(".envrc.example").read_text()
    ignored = project_root.joinpath(".gitignore").read_text().splitlines()

    assert "PATH_add .venv/bin" in template
    assert f"export {MODEL_ENV}=" in template
    assert f"export {BASE_URL_ENV}=" in template
    assert f"export {API_KEY_ENV}=" in template
    assert f"export {CONTEXT_WINDOW_ENV}=" in template
    assert f"export {OUTPUT_RESERVE_ENV}=" in template
    assert ".envrc" in ignored


def test_declares_global_install_script() -> None:
    project_root = Path(__file__).parents[1]
    installer = project_root.joinpath("scripts", "install.sh")

    assert installer.is_file()
    assert installer.stat().st_mode & 0o111
    text = installer.read_text()
    assert "uv tool install --editable ." in text
    assert "uv tool uninstall cli-agent" in text
    assert "uv tool update-shell" in text
    assert ".cli-agent" in text
    assert "cli-agent.env.example" in text
    assert "chmod 600" in text
    assert "UV_TOOL_BIN_DIR" in text
    assert "Restart your terminal" in text


def test_declares_global_dotenv_template() -> None:
    project_root = Path(__file__).parents[1]
    template = project_root.joinpath("cli-agent.env.example").read_text()

    assert f"{MODEL_ENV}=" in template
    assert f"{BASE_URL_ENV}=" in template
    assert f"{API_KEY_ENV}=" in template
    assert f"{CONTEXT_WINDOW_ENV}=" in template
    assert f"{OUTPUT_RESERVE_ENV}=" in template
    assert "PATH_add" not in template


def test_main_uses_validated_config_to_build_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    provider_options: dict[str, object] = {}
    run_call: dict[str, object] = {}

    class RecordingProvider:
        def __init__(self, **kwargs: object) -> None:
            provider_options.update(kwargs)

    async def recording_run_agent(
        config: CliConfig,
        provider: object,
        **streams: object,
    ) -> int:
        run_call.update(
            config=config,
            provider=provider,
            **streams,
        )
        return 0

    monkeypatch.setattr(
        config_module,
        "OpenAICompatibleModelProvider",
        RecordingProvider,
    )
    monkeypatch.setattr(cli_module, "run_agent", recording_run_agent)
    _set_environment(monkeypatch)

    exit_code = main(
        [
            "Inspect",
            "--workspace",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert provider_options == {
        "model": "test-model",
        "api_key": "secret",
        "base_url": "https://models.example/v1",
        "transport": None,
    }
    assert run_call["config"] == CliConfig(
        task="Inspect",
        workspace=tmp_path.resolve(),
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=DEFAULT_CONTEXT_SAFETY_MARGIN,
    )
    assert isinstance(run_call["provider"], RecordingProvider)
    assert run_call["stdin"] is cli_module.sys.stdin
    assert run_call["stdout"] is cli_module.sys.stdout
    assert run_call["stderr"] is cli_module.sys.stderr


def test_main_reports_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    async def interrupting_run_agent(*args: object, **kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "run_agent", interrupting_run_agent)
    _set_environment(monkeypatch)

    exit_code = main(
        [
            "--workspace",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.out == ""
    assert captured.err == "cli-agent: interrupted\n"


def test_main_reports_agent_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    async def failing_run_agent(*args: object, **kwargs: object) -> int:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(cli_module, "run_agent", failing_run_agent)
    _set_environment(monkeypatch)

    exit_code = main(
        [
            "Inspect",
            "--workspace",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "cli-agent: provider unavailable\n"


def _environment(
    *,
    model: str = "test-model",
    base_url: str = "https://models.example/v1",
    api_key: str = "secret",
    context_window: str = "128000",
    output_reserve: str = "4000",
    safety_margin: str | None = None,
) -> dict[str, str]:
    environment = {
        MODEL_ENV: model,
        BASE_URL_ENV: base_url,
        API_KEY_ENV: api_key,
        CONTEXT_WINDOW_ENV: context_window,
        OUTPUT_RESERVE_ENV: output_reserve,
    }
    if safety_margin is not None:
        environment[CONTEXT_SAFETY_MARGIN_ENV] = safety_margin
    return environment


def _set_environment(
    monkeypatch,
    *,
    model: str = "test-model",
    base_url: str = "https://models.example/v1",
    api_key: str = "secret",
) -> None:
    for name, value in _environment(
        model=model,
        base_url=base_url,
        api_key=api_key,
    ).items():
        monkeypatch.setenv(name, value)

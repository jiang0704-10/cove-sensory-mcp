"""An interrupted setup can resume without replacing credentials or working routes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import keyring
import pytest

from cove_sensory_mcp import cli
from cove_sensory_mcp.config.schema import AppConfig, ProviderConfig
from cove_sensory_mcp.config.secrets import KeyringSecretStore, MemorySecretStore
from cove_sensory_mcp.config.store import ConfigStore
from cove_sensory_mcp.errors import ErrorCode, SensoryError
from cove_sensory_mcp.models import Modality, ProviderRef, RouteConfig
from cove_sensory_mcp.providers.base import ProviderCallResult, ProviderRequest
from cove_sensory_mcp.providers.registry import ProviderRegistry
from cove_sensory_mcp.reports.schemas import ObservationEnvelope
from cove_sensory_mcp.services import AppServices
from cove_sensory_mcp.tools.setup import sensory_status, verify_provider_capabilities


@pytest.fixture
def services(tmp_path: Path) -> AppServices:
    return AppServices(ConfigStore(tmp_path / "config.yaml"), MemorySecretStore())


def configured_provider(
    services: AppServices,
    *,
    provider_id: str = "minimax-m3",
    adapter: str = "minimax-m3",
    verified: bool = False,
    environment: bool = False,
) -> ProviderConfig:
    provider = ProviderConfig(
        adapter=adapter,
        base_url="https://api.example.test",
        model="original-model",
        api_key_env="TEST_COVE_RESUME_KEY" if environment else None,
        credential_ref=None if environment else "original-ref",
        declared_capabilities={Modality.IMAGE: True, Modality.VIDEO_VISUAL: True},
        verified_capabilities=(
            {Modality.IMAGE: True, Modality.VIDEO_VISUAL: True} if verified else {}
        ),
    )
    services.config_store.save(AppConfig(providers={provider_id: provider}))
    if not environment:
        services.secret_store.set("original-ref", "original-secret-kept-local")
    return provider


def no_secret(prompt: str) -> str:
    pytest.fail(f"Resuming must not prompt for a replacement secret: {prompt}")


async def no_verification(*args: object, **kwargs: object) -> dict[str, object]:
    pytest.fail("This decision must not cause a paid Provider call")


class RecordingProvider:
    """Replace only remote perception; real verification and config writes still run."""

    def __init__(
        self, provider_id: str, failures: frozenset[Modality] = frozenset()
    ) -> None:
        self.provider_id = provider_id
        self.failures = failures
        self.requested: list[frozenset[Modality]] = []

    async def sense(self, request: ProviderRequest) -> ProviderCallResult:
        self.requested.append(request.requested_modalities)
        modality = next(iter(request.requested_modalities))
        if modality in self.failures:
            raise SensoryError(ErrorCode.PROVIDER_CAPABILITY_REJECTED, "Test rejection")
        return ProviderCallResult(
            observations={
                modality: ObservationEnvelope(
                    modality=modality,
                    summary="A visible test object.",
                    segments=[],
                    transcript=[],
                    warnings=[],
                    confidence="high",
                )
            },
            provider_id=self.provider_id,
            model="original-model",
            remote_file_deleted=None,
        )


@pytest.mark.parametrize(
    ("choice", "provider_id", "adapter"),
    [
        ("minimax-m3", "minimax-m3", "minimax-m3"),
        ("gemini", "gemini", "gemini"),
        ("custom", "studio-eye", "openai-compatible"),
    ],
)
def test_resume_verifies_and_enables_existing_provider_without_reentering_settings(
    services: AppServices, choice: str, provider_id: str, adapter: str
) -> None:
    """The duplicate-provider branch must lead to setup, not reject saved work."""
    original = configured_provider(services, provider_id=provider_id, adapter=adapter)
    answers = iter(
        [
            choice,
            *([provider_id] if choice == "custom" else []),
            "yes",
            "yes",
            *([] if adapter == "minimax-m3" else ["no"]),
            "yes",
        ]
    )
    remote = RecordingProvider(provider_id)

    async def verify(services, provider_id, modalities):
        return await verify_provider_capabilities(
            services,
            provider_id,
            modalities,
            registry=ProviderRegistry({provider_id: remote}),
        )

    messages: list[str] = []
    code = cli.run_configure(
        services, lambda _: next(answers), no_secret, messages.append, verify_fn=verify
    )

    assert code == 0
    saved = services.config_store.load()
    assert saved.providers[provider_id].model == original.model
    assert saved.providers[provider_id].base_url == original.base_url
    assert saved.providers[provider_id].credential_ref == "original-ref"
    assert services.secret_store.get("original-ref") == "original-secret-kept-local"
    assert saved.routes.image == RouteConfig(primary=provider_id)
    assert saved.routes.video_visual == RouteConfig(primary=provider_id)
    assert saved.routes.audio is None
    assert remote.requested == [
        frozenset({Modality.IMAGE}),
        frozenset({Modality.VIDEO_VISUAL}),
    ]
    assert asyncio.run(sensory_status(services))["ready"] is True
    assert "enabled" in " ".join(messages).lower()
    assert "original-secret" not in " ".join(messages)


def test_resume_activates_already_verified_capabilities_without_retesting(
    services: AppServices,
) -> None:
    """A previous self-test must be reusable without another paid verification."""
    configured_provider(services, verified=True)
    answers = iter(["minimax-m3", "yes", "yes"])
    assert (
        cli.run_configure(
            services,
            lambda _: next(answers),
            no_secret,
            lambda _: None,
            verify_fn=no_verification,
        )
        == 0
    )
    assert services.config_store.load().routes.image == RouteConfig(
        primary="minimax-m3"
    )
    assert asyncio.run(sensory_status(services))["ready"] is True


@pytest.mark.parametrize(
    "answers",
    [
        ["minimax-m3", "no"],
        ["minimax-m3", "cancel"],
        ["minimax-m3", "yes", "cancel"],
        ["minimax-m3", "yes", "yes", "no"],
        ["minimax-m3", "yes", "yes", "cancel"],
    ],
)
def test_abandoned_resume_keeps_original_file_and_secret(
    services: AppServices, answers: list[str]
) -> None:
    """Cancelling at any resume stage must not delete or overwrite saved work."""
    configured_provider(services)
    original = services.config_store.path.read_bytes()
    supplied = iter(answers)
    messages: list[str] = []
    cli.run_configure(
        services,
        lambda _: next(supplied),
        no_secret,
        messages.append,
        verify_fn=no_verification,
    )
    assert services.config_store.path.read_bytes() == original
    assert services.secret_store.get("original-ref") == "original-secret-kept-local"
    assert any(
        "resume" in message.lower() or "continu" in message.lower()
        for message in messages
    )


def test_resume_missing_environment_stops_before_network_and_preserves_settings(
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable env reference is a local setup problem, not a rejected API key."""
    monkeypatch.delenv("TEST_COVE_RESUME_KEY", raising=False)
    configured_provider(services, environment=True)
    original = services.config_store.path.read_bytes()
    answers = iter(["minimax-m3", "yes", "yes"])
    messages: list[str] = []
    assert (
        cli.run_configure(
            services,
            lambda _: next(answers),
            no_secret,
            messages.append,
            verify_fn=no_verification,
        )
        == 1
    )
    assert services.config_store.path.read_bytes() == original
    report = " ".join(messages).lower()
    assert "environment variable" in report and "missing" in report
    assert "process" in report and "restart" in report
    assert "invalid api key" not in report


@pytest.mark.parametrize("replace", ["no", "yes"])
def test_resume_requires_permission_to_replace_another_default(
    services: AppServices,
    replace: str,
) -> None:
    """Selecting a role must not silently replace an already configured primary."""
    original = configured_provider(services, verified=True)
    config = services.config_store.load()
    config.providers["other"] = original.model_copy(deep=True)
    config.routes.image = RouteConfig(primary="other")
    services.config_store.save(config)
    answers = iter(["minimax-m3", "yes", "yes"])
    replacement_prompts: list[str] = []

    def answer(prompt: str) -> str:
        if "replace" in prompt.lower():
            replacement_prompts.append(prompt)
            return replace
        if "fallback" in prompt.lower():
            return "no"
        return next(answers)

    assert (
        cli.run_configure(
            services, answer, no_secret, lambda _: None, verify_fn=no_verification
        )
        == 0
    )
    saved = services.config_store.load()
    assert saved.routes.image == RouteConfig(
        primary="minimax-m3" if replace == "yes" else "other"
    )
    assert len(replacement_prompts) == 1


def test_resume_preserves_same_primary_and_its_authorized_fallbacks(
    services: AppServices,
) -> None:
    """Resuming a working primary must not erase its fallback choices."""
    original = configured_provider(services, verified=True)
    config = services.config_store.load()
    config.providers["backup"] = original.model_copy(deep=True)
    route = RouteConfig(
        primary="minimax-m3",
        fallbacks=[ProviderRef(provider="backup", authorized=True)],
    )
    config.routes.image = route
    config.routes.video_visual = route.model_copy(deep=True)
    services.config_store.save(config)
    before = services.config_store.path.read_bytes()
    answers = iter(["minimax-m3", "yes", "yes"])
    assert (
        cli.run_configure(
            services,
            lambda _: next(answers),
            no_secret,
            lambda _: None,
            verify_fn=no_verification,
        )
        == 0
    )
    assert services.config_store.path.read_bytes() == before


def test_resume_detects_settings_changed_during_role_selection(
    services: AppServices,
) -> None:
    """Consent for the original provider must not activate a concurrently edited one."""
    configured_provider(services, verified=True)
    answers = iter(["minimax-m3", "yes", "yes"])

    def answer(prompt: str) -> str:
        if "eye" in prompt.lower():

            def edit(config: AppConfig) -> None:
                config.providers["minimax-m3"].model = "concurrently-changed-model"

            services.config_store.update(edit)
        return next(answers)

    assert (
        cli.run_configure(
            services, answer, no_secret, lambda _: None, verify_fn=no_verification
        )
        == 1
    )
    saved = services.config_store.load()
    assert saved.providers["minimax-m3"].model == "concurrently-changed-model"
    assert saved.routes.image is None


@pytest.mark.parametrize(
    ("verified", "expected"), [(False, "not verified"), (True, "not enabled")]
)
def test_cli_status_explains_saved_but_unfinished_setup(
    services: AppServices,
    verified: bool,
    expected: str,
) -> None:
    """A saved provider needs an actionable state, not a claim that none exists."""
    configured_provider(services, verified=verified)
    messages: list[str] = []
    assert cli.run_status(services, messages.append) == 0
    report = " ".join(messages).lower()
    assert expected in report
    assert "configure" in report and "continu" in report
    assert "original-ref" not in report and "original-secret" not in report


@pytest.mark.parametrize(
    ("verified", "expected"), [(False, "not verified"), (True, "not enabled")]
)
def test_mcp_status_explains_saved_but_unfinished_setup(
    services: AppServices,
    verified: bool,
    expected: str,
) -> None:
    """MCP guidance must not misreport an existing unactivated provider as absent."""
    configured_provider(services, verified=verified)
    status = asyncio.run(sensory_status(services))
    assert status["ready"] is False
    reason = status["capabilities"]["image"]["reason"].lower()
    assert expected in reason
    assert "configure" in reason


@pytest.mark.parametrize(
    "failed_modalities",
    [
        frozenset({Modality.VIDEO_VISUAL}),
        frozenset({Modality.IMAGE, Modality.VIDEO_VISUAL}),
    ],
)
def test_resume_only_enables_successful_verification_results(
    services: AppServices,
    failed_modalities: frozenset[Modality],
) -> None:
    """Provider rejection must never be converted into an enabled route."""
    configured_provider(services)
    remote = RecordingProvider("minimax-m3", failures=failed_modalities)

    async def verify(services, provider_id, modalities):
        return await verify_provider_capabilities(
            services,
            provider_id,
            modalities,
            registry=ProviderRegistry({provider_id: remote}),
        )

    answers = iter(["minimax-m3", "yes", "yes", "yes"])
    messages: list[str] = []
    code = cli.run_configure(
        services, lambda _: next(answers), no_secret, messages.append, verify_fn=verify
    )
    saved = services.config_store.load()
    assert saved.routes.video_visual is None
    assert (
        saved.providers["minimax-m3"].verified_capabilities.get(
            Modality.VIDEO_VISUAL, False
        )
        is False
    )
    if Modality.IMAGE in failed_modalities:
        assert code == 1
        assert saved.routes.image is None
        assert "did not pass" in " ".join(messages)
    else:
        assert code == 0
        assert saved.routes.image == RouteConfig(primary="minimax-m3")
    assert services.secret_store.get("original-ref") == "original-secret-kept-local"


def test_resume_only_tests_still_unverified_modalities(services: AppServices) -> None:
    """Resuming a partial test must not charge for already verified capabilities."""
    configured_provider(services)
    services.config_store.update(
        lambda config: config.providers["minimax-m3"].verified_capabilities.update(
            {Modality.IMAGE: True}
        )
    )
    remote = RecordingProvider("minimax-m3")

    async def verify(services, provider_id, modalities):
        return await verify_provider_capabilities(
            services,
            provider_id,
            modalities,
            registry=ProviderRegistry({provider_id: remote}),
        )

    answers = iter(["minimax-m3", "yes", "yes", "yes"])
    assert (
        cli.run_configure(
            services,
            lambda _: next(answers),
            no_secret,
            lambda _: None,
            verify_fn=verify,
        )
        == 0
    )
    assert remote.requested == [frozenset({Modality.VIDEO_VISUAL})]
    saved = services.config_store.load()
    assert saved.routes.image == RouteConfig(primary="minimax-m3")
    assert saved.routes.video_visual == RouteConfig(primary="minimax-m3")


@pytest.mark.parametrize("stage", ["Continue setting", "eye", "verification now"])
@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_resume_terminal_interruption_preserves_original_configuration(
    services: AppServices,
    stage: str,
    interruption: type[BaseException],
) -> None:
    configured_provider(services)
    before = services.config_store.path.read_bytes()

    def answer(prompt: str) -> str:
        if stage in prompt:
            raise interruption
        return "minimax-m3" if prompt.startswith("Provider [") else "yes"

    assert (
        cli.run_configure(
            services, answer, no_secret, lambda _: None, verify_fn=no_verification
        )
        == 1
    )
    assert services.config_store.path.read_bytes() == before


def test_resume_environment_credential_does_not_consult_the_keyring(
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resuming env-based setup must not depend on a working OS keyring."""
    configured_provider(services, environment=True, verified=True)
    monkeypatch.setenv("TEST_COVE_RESUME_KEY", "private-environment-secret")
    monkeypatch.setattr(
        keyring, "get_password", lambda *args: pytest.fail("keyring read in env mode")
    )
    monkeypatch.setattr(
        keyring, "set_password", lambda *args: pytest.fail("keyring write in env mode")
    )
    services.secret_store = KeyringSecretStore()
    answers = iter(["minimax-m3", "yes", "yes"])
    messages: list[str] = []
    assert (
        cli.run_configure(
            services,
            lambda _: next(answers),
            no_secret,
            messages.append,
            verify_fn=no_verification,
        )
        == 0
    )
    assert (
        services.config_store.load().providers["minimax-m3"].api_key_env
        == "TEST_COVE_RESUME_KEY"
    )
    assert services.config_store.load().routes.image == RouteConfig(
        primary="minimax-m3"
    )
    assert "private-environment-secret" not in " ".join(messages)


def test_resume_detects_settings_changed_during_quota_confirmation(
    services: AppServices,
) -> None:
    """Network consent must not apply to a newly substituted endpoint."""
    configured_provider(services)

    def answer(prompt: str) -> str:
        if "verification now" in prompt:

            def edit(config: AppConfig) -> None:
                config.providers["minimax-m3"].base_url = "https://changed.example.test"

            services.config_store.update(edit)
        return "minimax-m3" if prompt.startswith("Provider [") else "yes"

    assert (
        cli.run_configure(
            services, answer, no_secret, lambda _: None, verify_fn=no_verification
        )
        == 1
    )
    assert services.config_store.load().routes.image is None


def test_resume_rejects_a_late_route_change_during_replacement_confirmation(
    services: AppServices,
) -> None:
    """Concurrent default changes must survive the entire activation transaction."""
    provider = configured_provider(services, verified=True)
    config = services.config_store.load()
    config.providers["other"] = provider.model_copy(deep=True)
    config.providers["late"] = provider.model_copy(deep=True)
    config.routes.image = RouteConfig(primary="other")
    services.config_store.save(config)

    def answer(prompt: str) -> str:
        if "Replace" in prompt:

            def edit(config: AppConfig) -> None:
                config.routes.image = RouteConfig(primary="late")

            services.config_store.update(edit)
        if "fallback" in prompt:
            return "no"
        return "minimax-m3" if prompt.startswith("Provider [") else "yes"

    assert (
        cli.run_configure(
            services, answer, no_secret, lambda _: None, verify_fn=no_verification
        )
        == 1
    )
    saved = services.config_store.load()
    assert saved.routes.image == RouteConfig(primary="late")
    assert saved.routes.video_visual is None


def test_new_provider_is_kept_when_missing_environment_prevents_verification(
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first-run missing credential must leave resumable settings, not claim success."""
    monkeypatch.delenv("TEST_COVE_RESUME_KEY", raising=False)
    answers = iter(
        ["minimax-m3", "env:TEST_COVE_RESUME_KEY", "cn", "MiniMax-M3", "yes"]
    )
    messages: list[str] = []
    assert (
        cli.run_configure(
            services,
            lambda _: next(answers),
            no_secret,
            messages.append,
            verify_fn=no_verification,
        )
        == 1
    )
    saved = services.config_store.load()
    assert saved.providers["minimax-m3"].api_key_env == "TEST_COVE_RESUME_KEY"
    assert saved.routes.image is None
    assert "continue" in " ".join(messages).lower()


def test_status_warns_about_missing_environment_without_reading_its_value(
    services: AppServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_provider(services, environment=True)
    monkeypatch.delenv("TEST_COVE_RESUME_KEY", raising=False)
    messages: list[str] = []
    cli.run_status(services, messages.append)
    report = " ".join(messages).lower()
    assert "environment variable missing" in report
    assert "not a provider authentication check" in report


def test_mcp_status_explains_that_hearing_is_optional_for_minimax(
    services: AppServices,
) -> None:
    configured_provider(services)
    status = asyncio.run(sensory_status(services))
    assert status["capabilities"]["audio"]["enabled"] is False
    assert "not required" in status["capabilities"]["audio"]["reason"]


def test_activation_handles_provider_removed_before_route_planning(
    services: AppServices,
) -> None:
    """A concurrent deletion must produce a bounded setup error, not a KeyError."""
    original = configured_provider(services, verified=True)
    services.config_store.update(lambda config: config.providers.clear())
    with pytest.raises(SensoryError) as caught:
        cli._write_verified_routes(
            services,
            "minimax-m3",
            [Modality.IMAGE],
            lambda _: "yes",
            expected_provider=original,
        )
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert services.config_store.load().routes.image is None


@pytest.mark.parametrize(
    "failure",
    [
        {
            "status": "error",
            "error": {"code": "PROVIDER_AUTH_FAILED", "message": "private-response"},
        },
        {
            "status": "error",
            "results": [
                {
                    "modality": "image",
                    "verified": False,
                    "reason": "PROVIDER_AUTH_FAILED",
                    "provider": "private-provider",
                }
            ],
        },
    ],
)
def test_resume_explains_remote_auth_failure_without_leaking_response(
    services: AppServices,
    failure: dict[str, object],
) -> None:
    """The wizard must distinguish a Provider auth rejection from missing local credentials."""
    configured_provider(services)

    async def rejected(*args: object, **kwargs: object) -> dict[str, object]:
        return failure

    answers = iter(["minimax-m3", "yes", "yes", "yes"])
    messages: list[str] = []
    assert (
        cli.run_configure(
            services,
            lambda _: next(answers),
            no_secret,
            messages.append,
            verify_fn=rejected,
        )
        == 1
    )
    report = " ".join(messages)
    assert "PROVIDER_AUTH_FAILED" in report
    assert "private-response" not in report and "private-provider" not in report
    assert "original-secret" not in report
    assert services.config_store.load().routes.image is None


def test_resume_does_not_print_unknown_error_fields(services: AppServices) -> None:
    """Only whitelisted public error codes may be shown, never raw Provider output."""
    configured_provider(services)

    async def rejected(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "status": "error",
            "error": {"code": "private-secret-value"},
            "results": [{"reason": "another-private-value"}],
        }

    answers = iter(["minimax-m3", "yes", "yes", "yes"])
    messages: list[str] = []
    assert (
        cli.run_configure(
            services,
            lambda _: next(answers),
            no_secret,
            messages.append,
            verify_fn=rejected,
        )
        == 1
    )
    assert "private" not in " ".join(messages).lower()


def test_activation_returns_the_committed_snapshot_for_reporting(
    services: AppServices,
) -> None:
    """Reporting after activation must use its committed result, not another fallible read."""
    configured_provider(services, verified=True)
    committed = cli._write_verified_routes(
        services, "minimax-m3", [Modality.IMAGE], lambda _: "no"
    )
    assert isinstance(committed, AppConfig)
    assert committed.routes.image == RouteConfig(primary="minimax-m3")

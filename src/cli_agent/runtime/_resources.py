"""Backend-neutral Runtime resource aggregate and reconciliation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cli_agent.runtime._capability.deployment import ToolExecutor
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.overlay import CapabilityOverlay
from cli_agent.runtime._capability.snapshot import CapabilitySnapshot
from cli_agent.runtime._composition import RuntimeComponents, WorkspaceConfig
from cli_agent.runtime._database.session_store import SessionStore
from cli_agent.runtime._workspace import Workspace


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    """Same-revision capability facts and execution adapters."""

    snapshot: CapabilitySnapshot
    tool_executor: ToolExecutor
    overlay: CapabilityOverlay | None


@dataclass(frozen=True, slots=True)
class _RuntimeResources:
    """Reference-stable aggregate of Workspace-lifetime Runtime resources.

    ``frozen`` only prevents field rebinding; referenced components continue
    to encapsulate their own mutable state. Environment values stay behind
    Workspace and are not duplicated in this aggregate. The aggregate owns
    the RFC-0012 close sequence: the Library
    worker stops first, then the Workspace flushes and closes its bound
    Backend, and finally the sole state owner closes the shared database.
    """

    workspace: Workspace
    capabilities: CapabilityBinding
    session_store: SessionStore

    async def close(self) -> None:
        """Close Workspace-lifetime resources in reverse dependency order.

        The Library worker stops first, the Backend Workspace is flushed,
        then the Workspace closes its Backend and SessionStore closes the
        shared state database. Every
        step is attempted so a failure cannot leak resources; the first
        failure is raised so the Host never assumes persistence succeeded.
        """

        errors: list[Exception] = []
        library = self.capabilities.snapshot.library
        if library is not None:
            try:
                await library.close()
            except Exception as exc:
                errors.append(exc)
        overlay = self.capabilities.overlay
        if overlay is not None:
            try:
                await overlay.close()
            except Exception as exc:
                errors.append(exc)
        try:
            await self.workspace.flush()
        except Exception as exc:
            errors.append(exc)
        try:
            await self.workspace.close()
        except Exception as exc:
            errors.append(exc)
        try:
            self.session_store.close()
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise errors[0]


class _OpenResources:
    """Best-effort reverse-order closer for a partially opened Runtime.

    Tracks closers as resources are acquired during Runtime open; on any
    failure every already-opened resource is closed, and close failures are
    swallowed so the original open failure reaches the caller.
    """

    def __init__(self) -> None:
        self._closers: list[Callable[[], Awaitable[None]]] = []

    def add(self, closer: Callable[[], Awaitable[None]]) -> None:
        """Register one closer for an opened resource."""
        self._closers.append(closer)

    async def rollback(self) -> None:
        """Close every opened resource in reverse order, best effort."""
        for closer in reversed(self._closers):
            try:
                await closer()
            except Exception:
                pass


async def _reconcile_runtime_resources(
    *,
    config: WorkspaceConfig,
    components: RuntimeComponents,
) -> _RuntimeResources:
    """Reconcile Workspace-lifetime resources in the established order.

    The fixed forward order is Workspace → Source → Provider/Snapshot →
    Overlay → Library facts → Deployment → ToolExecutor. Backend selection
    and every replaceable adapter arrive through ``RuntimeComponents``.
    """

    opened = _OpenResources()
    opened.add(lambda: _close_session_store(components.session_store))
    try:
        workspace = await components.workspace_factory.open(
            config.root,
            repertoire=config.repertoire,
        )
        opened.add(workspace.close)
        source = await components.capability_source_factory.create(workspace)
        snapshot = await components.capability_provider.discover(
            source,
            mcp_discovery=components.mcp_discovery,
            mcp_environment=workspace,
            project_instructions=workspace,
        )
        overlay = await components.capability_overlay_factory.create(workspace)
        opened.add(overlay.close)
        library_catalog = await _LibraryCatalog.reconcile(
            source,
            workspace.filesystem,
            components.summary_cache,
        )
        snapshot = snapshot.with_library(library_catalog)
        deployment_snapshot = await components.capability_deployment.reconcile(
            snapshot,
            workspace,
        )
        return _RuntimeResources(
            workspace=workspace,
            capabilities=CapabilityBinding(
                snapshot=snapshot,
                tool_executor=components.tool_executor_factory.create(
                    workspace,
                    snapshot,
                    deployment_snapshot,
                ),
                overlay=overlay,
            ),
            session_store=components.session_store,
        )
    except BaseException:
        await opened.rollback()
        raise


async def _close_session_store(store: SessionStore) -> None:
    """Close one injected persistence adapter from an async closer."""

    store.close()

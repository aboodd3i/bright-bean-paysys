"""Tool registry and specification for the Slack analytics bot.

Defines the explicit allowlist of approved analytics tools that the LLM
may request.  The registry:

* Registers tools with a name, description, input schema (Pydantic/ninja
  ``Schema`` class), and executor callable.
* Rejects duplicate names, invalid names, and unknown lookups.
* Converts registered tools into provider-neutral
  :class:`~apps.slack_bot.llm.base.LLMToolDefinition` objects for the
  LLM adapters.
* Does **not** execute tools — that is the orchestrator's job.
* Does **not** discover tools dynamically — the allowlist is explicit.

No real BrightBean analytics executors are registered here.  Phase 5
will create a production registry after real tools exist.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ninja import Schema

from .llm.base import LLMToolDefinition

if TYPE_CHECKING:
    from .contracts import ToolContext, ToolResult

logger = logging.getLogger(__name__)

# Tool names must be snake_case identifiers (letters, digits, underscores,
# starting with a letter).  This prevents path traversal, dotted module
# paths, and other injection vectors.
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# Executor protocol
# ---------------------------------------------------------------------------


class ToolExecutor(Protocol):
    """Provider-neutral executor contract.

    Each registered tool binds an executor callable with this signature.
    The executor receives **validated** arguments (a Pydantic model
    instance) and the application-created
    :class:`~apps.slack_bot.contracts.ToolContext`.  It returns a
    :class:`~apps.slack_bot.contracts.ToolResult`.

    The executor must **not** receive:

    * Raw dictionaries (arguments are already validated).
    * Slack objects.
    * Provider-specific objects.
    * Authorization scope from the LLM.
    """

    def __call__(
        self,
        *,
        arguments: Any,
        context: ToolContext,
    ) -> ToolResult: ...


# ---------------------------------------------------------------------------
# Registered tool specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisteredTool:
    """Specification for a single approved analytics tool.

    Fields
    ------
    name : str
        Canonical tool name (snake_case, validated).
    description : str
        Human-readable description for the LLM.  Non-empty.
    input_schema_type : type[Schema]
        Pydantic/ninja Schema class for argument validation.
        Must use ``extra="forbid"`` to reject unknown fields.
    executor : ToolExecutor
        Callable that executes the tool with validated arguments
        and application-created :class:`ToolContext`.
    """

    name: str
    description: str
    input_schema_type: type[Schema]
    executor: ToolExecutor

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("RegisteredTool.name must not be empty")
        if not _TOOL_NAME_RE.match(self.name):
            raise ValueError(
                f"RegisteredTool.name {self.name!r} must be snake_case "
                f"(lowercase letters, digits, underscores, starting with a letter)"
            )
        if not self.description:
            raise ValueError("RegisteredTool.description must not be empty")

    def to_llm_tool_definition(self) -> LLMToolDefinition:
        """Convert to a provider-neutral :class:`LLMToolDefinition`."""
        schema = self.input_schema_type.model_json_schema()
        # Resolve $ref entries inline for provider compatibility.
        schema = _resolve_refs(schema)
        return LLMToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=schema,
        )


def _resolve_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$ref`` entries in a JSON schema for provider compatibility.

    Pydantic generates ``$ref`` for enum types.  Providers like Anthropic
    and Z.AI expect inline schemas.  This resolves ``$ref`` against
    ``$defs`` and removes the ``$defs`` key.
    """
    defs = schema.pop("$defs", {})
    if not defs:
        return schema

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_path = node["$ref"]
                # Format: "#/$defs/TypeName"
                type_name = ref_path.rsplit("/", 1)[-1]
                resolved = defs.get(type_name, {})
                return _resolve({k: v for k, v in resolved.items()})
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    return _resolve(schema)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Explicit allowlist registry of approved analytics tools.

    The registry does **not** execute tools.  It owns only:

    * Tool-name → :class:`RegisteredTool` lookup.
    * Duplicate-name prevention.
    * Conversion to provider-neutral :class:`LLMToolDefinition` objects.
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        """Register *tool*.  Raises if the name is already taken."""
        if tool.name in self._tools:
            raise ValueError(
                f"Tool {tool.name!r} is already registered"
            )
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    def get(self, name: str) -> RegisteredTool:
        """Retrieve a registered tool by exact canonical name.

        Raises ``KeyError`` if the tool is not registered.
        """
        if name not in self._tools:
            raise KeyError(f"Tool {name!r} is not registered")
        return self._tools[name]

    def contains(self, name: str) -> bool:
        """True when *name* is a registered tool."""
        return name in self._tools

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Registered tool names in insertion order."""
        return tuple(self._tools.keys())

    def to_llm_tool_definitions(self) -> list[LLMToolDefinition]:
        """Convert all registered tools to provider-neutral definitions.

        Returns definitions in insertion (registration) order.
        """
        return [
            self._tools[name].to_llm_tool_definition()
            for name in self._tools
        ]

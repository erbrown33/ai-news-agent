"""
config/loader.py — YAML loader, Pydantic validation, and multi-agent discovery.

Traces: SRC-071 (config loading; fail loudly on errors),
        SRC-072 (multiple agents discoverable by scheduler),
        SRC-073 (secrets always from env vars — never from YAML),
        SRC-145 ({agent_id} placeholder resolved at load time)

Public API:
    load_agent_config(path)          → AgentConfig
    load_scheduler_config(path)      → SchedulerConfig
    load_all_enabled_agents(sched)   → dict[str, AgentConfig]
    validate_no_secrets_in_yaml(raw) → None  (raises ConfigError if violated)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import ValidationError

from ai_news_agent.config.models import AgentConfig, SchedulerConfig

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Sentinel patterns — any of these appearing in YAML body (outside comments)
# means a secret may have leaked into the config file (SRC-073).
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # OpenAI API key: sk- followed by alphanumeric, hyphens, underscores (e.g. sk-proj-..., sk-...)
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    # Twitter/X bearer token: starts with AAA and is base64-like (long)
    re.compile(r"\bAAA[A-Za-z0-9+/]{30,}"),
    # Anthropic key-like strings
    re.compile(r"\banthrop[A-Za-z0-9_-]{20,}"),
    # Brave Search key prefix
    re.compile(r"\bBSC[A-Za-z0-9]{20,}"),
]


# ---------------------------------------------------------------------------
# ConfigError
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """
    Raised for:
    - YAML parse errors
    - Pydantic schema validation failures
    - Missing required configuration files
    - Detected secrets in YAML body (SRC-073)

    Traces: SRC-071 (fail loudly at startup with actionable error messages)
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_yaml(path: str | Path) -> dict[str, Any]:
    """
    Read a YAML file and return its parsed contents as a dict.

    Raises :class:`ConfigError` if the file is missing or YAML is malformed.
    """
    resolved = Path(path)
    if not resolved.exists():
        raise ConfigError(
            f"Config file not found: {resolved.absolute()}\n"
            "Ensure the file exists and the path in scheduler.yaml is correct."
        )
    try:
        with resolved.open("r", encoding="utf-8") as fh:
            data: dict[str, Any] = yaml.safe_load(fh) or {}
        return data
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"YAML parse error in {resolved}:\n{exc}\n"
            "Check indentation, quote matching, and special-character escaping."
        ) from exc


def validate_no_secrets_in_yaml(raw_yaml: str, source_name: str = "<unknown>") -> None:
    """
    Scan the YAML body (non-comment lines) for common secret value patterns.

    This is a defence-in-depth check — not a substitute for keeping secrets in
    env vars. It catches common mistakes like accidentally pasting an API key
    into the config.

    Traces: SRC-073 (secrets in env vars only — never in YAML)

    Args:
        raw_yaml:    The full raw YAML file content as a string.
        source_name: Displayed in error messages (e.g. the file path).

    Raises:
        ConfigError: If any secret-like value is detected in a non-comment line.
    """
    for lineno, line in enumerate(raw_yaml.splitlines(), start=1):
        stripped = line.strip()
        # Skip blank lines and YAML comments
        if not stripped or stripped.startswith("#"):
            continue
        for pattern in _SECRET_PATTERNS:
            if pattern.search(stripped):
                raise ConfigError(
                    f"Possible secret value detected in {source_name} at line {lineno}:\n"
                    f"  {line!r}\n"
                    "Secrets must be stored in environment variables, NOT in YAML config files. "
                    "(SRC-073)"
                )


# ---------------------------------------------------------------------------
# Public loaders
# ---------------------------------------------------------------------------


def load_agent_config(path: str | Path) -> AgentConfig:
    """
    Load and validate a per-agent YAML configuration file.

    Steps:
    1. Read YAML from ``path``.
    2. Scan for secret values in non-comment lines (SRC-073).
    3. Parse into :class:`AgentConfig` via Pydantic v2 schema validation.
       - ``{agent_id}`` in ``output_dir`` is resolved automatically by the
         :meth:`AgentConfig.resolve_output_dir` model validator (SRC-145).
    4. Log the loaded config summary at DEBUG level.

    Secrets are NEVER read from YAML — they come from :class:`RuntimeSecrets`
    (environment variables) validated separately at startup. (SRC-073)

    Returns:
        A validated, immutable :class:`AgentConfig` instance.

    Raises:
        :class:`ConfigError`: If the file is missing, YAML is malformed,
            schema validation fails, or secret values are detected.

    Traces: SRC-071–SRC-073, SRC-145
    """
    resolved = Path(path)

    # Read raw YAML text first (for secret scan)
    if not resolved.exists():
        raise ConfigError(
            f"Agent config file not found: {resolved.absolute()}\n"
            "Ensure the path in configs/scheduler.yaml is correct."
        )

    raw_text = resolved.read_text(encoding="utf-8")

    # Defence-in-depth: scan for accidentally committed secrets (SRC-073)
    validate_no_secrets_in_yaml(raw_text, source_name=str(resolved))

    # Parse YAML
    try:
        data: dict[str, Any] = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error in {resolved}:\n{exc}") from exc

    # Pydantic validation (includes {agent_id} resolution via model_validator)
    try:
        config = AgentConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            f"Invalid agent configuration at {resolved}:\n{exc}\n\n"
            "Refer to configs/default-agent.yaml for a documented example "
            "or run: python -m ai_news_agent.config.validator --agent <path>"
        ) from exc

    log.debug(
        "config_agent_loaded",
        agent_id=config.agent_id,
        provider=config.llm.provider,
        model=config.llm.model,
        output_dir=config.output_dir,
        twitter_enabled=config.twitter.enabled,
        handle_count=len(config.twitter.handles),
    )

    return config


def load_scheduler_config(
    path: str | Path = "configs/scheduler.yaml",
) -> SchedulerConfig:
    """
    Load and validate the root scheduler configuration from ``scheduler.yaml``.

    The scheduler config contains:
    - Global retry policy (SRC-144)
    - Cron trigger times for all cadences (SRC-052)
    - Registry of all agent configurations (SRC-072)
    - Manual override API settings (SRC-147)

    Returns:
        A validated :class:`SchedulerConfig` instance.

    Raises:
        :class:`ConfigError`: On file-not-found, YAML errors, or schema violations.

    Traces: SRC-052, SRC-072, SRC-144, SRC-147
    """
    resolved = Path(path)

    if not resolved.exists():
        raise ConfigError(
            f"Scheduler config not found: {resolved.absolute()}\n"
            "Create configs/scheduler.yaml — see configs/scheduler.yaml for the template."
        )

    raw_text = resolved.read_text(encoding="utf-8")
    validate_no_secrets_in_yaml(raw_text, source_name=str(resolved))

    try:
        data: dict[str, Any] = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error in {resolved}:\n{exc}") from exc

    try:
        config = SchedulerConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid scheduler configuration at {resolved}:\n{exc}") from exc

    enabled_count = sum(1 for a in config.agents if a.enabled)
    log.debug(
        "config_scheduler_loaded",
        agent_count=len(config.agents),
        enabled_count=enabled_count,
        max_retries=config.scheduler.max_retries,
    )

    return config


def load_all_enabled_agents(
    scheduler_config: SchedulerConfig,
    base_dir: str | Path | None = None,
) -> dict[str, AgentConfig]:
    """
    Load all enabled agent configurations listed in a :class:`SchedulerConfig`.

    This is the primary entry point for the scheduler's multi-agent discovery
    loop (SRC-072). Failures in individual configs are logged and skipped so
    that one bad config doesn't prevent other agents from running.

    Args:
        scheduler_config: Already-loaded :class:`SchedulerConfig`.
        base_dir:         Optional base directory to resolve relative config paths.
                          Defaults to the current working directory.

    Returns:
        A dict mapping agent ID → :class:`AgentConfig` for every successfully
        loaded enabled agent.  Disabled or failed agents are excluded.

    Traces: SRC-072 (multi-agent discovery; independent scheduling per agent)
    """
    results: dict[str, AgentConfig] = {}
    cwd = Path(base_dir) if base_dir else Path.cwd()

    for registration in scheduler_config.agents:
        if not registration.enabled:
            log.info(
                "config_agent_skipped_disabled",
                agent_id=registration.id,
                config=registration.config,
            )
            continue

        config_path = Path(registration.config)
        if not config_path.is_absolute():
            config_path = cwd / config_path

        try:
            agent_cfg = load_agent_config(config_path)

            # Cross-check: agent_id in file should match the id in the registry
            if agent_cfg.agent_id != registration.id:
                log.warning(
                    "config_agent_id_mismatch",
                    registry_id=registration.id,
                    file_agent_id=agent_cfg.agent_id,
                    config=str(config_path),
                    hint=(
                        "The 'agent_id' field in the YAML file does not match the "
                        "'id' field in scheduler.yaml. Using the file's agent_id."
                    ),
                )

            results[agent_cfg.agent_id] = agent_cfg
            log.info(
                "config_agent_registered",
                agent_id=agent_cfg.agent_id,
                config=str(config_path),
                description=registration.description,
            )

        except ConfigError as exc:
            log.error(
                "config_agent_load_failed",
                agent_id=registration.id,
                config=str(config_path),
                error=str(exc),
            )
            # Do not re-raise — other agents continue loading (SRC-072)

    return results


def discover_agents_from_scheduler(
    scheduler_yaml_path: str | Path = "configs/scheduler.yaml",
) -> dict[str, AgentConfig]:
    """
    Convenience wrapper: load scheduler config then load all enabled agents.

    Combines :func:`load_scheduler_config` and :func:`load_all_enabled_agents`
    into a single call for the common case.

    Agent config paths listed inside ``scheduler.yaml`` are resolved relative to
    the **current working directory** (not relative to the scheduler file's own
    directory), because that is the convention used in the shipped ``configs/``
    layout where ``scheduler.yaml`` and the agent YAMLs share the same folder
    but are referenced as ``configs/*.yaml`` from the project root.

    Returns:
        Dict mapping agent_id → AgentConfig for all enabled, valid agents.

    Raises:
        :class:`ConfigError`: If the scheduler YAML is missing or invalid.

    Traces: SRC-072 (scheduler discovers all agents without code changes)
    """
    sched = load_scheduler_config(scheduler_yaml_path)
    # Use CWD so that agent config paths (e.g. "configs/default-agent.yaml")
    # resolve correctly regardless of where the scheduler YAML itself lives.
    return load_all_enabled_agents(sched, base_dir=None)

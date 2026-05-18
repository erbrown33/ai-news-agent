"""
config — Configuration loading, Pydantic validation, and JSON Schema generation.

Public API:

    Loaders (config/loader.py)
        load_agent_config(path)                   → AgentConfig
        load_scheduler_config(path)               → SchedulerConfig
        load_all_enabled_agents(sched_cfg)        → dict[str, AgentConfig]
        discover_agents_from_scheduler(path)      → dict[str, AgentConfig]
        validate_no_secrets_in_yaml(raw, name)    → None (raises ConfigError)
        ConfigError                               exception class

    Models (config/models.py)
        AgentConfig, SchedulerConfig, RuntimeSecrets
        LLMConfig, LLMCadenceOverride
        SourcesConfig, TwitterConfig, TwitterHandleConfig
        LimitsConfig, RetryConfig, TriggersConfig
        APIConfig, AgentRegistration

    Schema (config/schema.py)
        generate_agent_schema()                   → dict
        generate_scheduler_schema()               → dict
        export_schemas(dir)                       → None
        validate_agent_yaml(path)                 → AgentConfig
        validate_scheduler_yaml(path)             → SchedulerConfig
        SchemaValidationError                     exception class

Traces: SRC-071–SRC-073 (config system), SRC-105–SRC-111 (secrets from env vars only)
"""

from ai_news_agent.config.loader import (
    ConfigError,
    discover_agents_from_scheduler,
    load_agent_config,
    load_all_enabled_agents,
    load_scheduler_config,
    validate_no_secrets_in_yaml,
)
from ai_news_agent.config.models import (
    AgentConfig,
    AgentRegistration,
    APIConfig,
    LimitsConfig,
    LLMCadenceOverride,
    LLMConfig,
    RetryConfig,
    RuntimeSecrets,
    SchedulerConfig,
    SourcesConfig,
    TriggersConfig,
    TwitterConfig,
    TwitterHandleConfig,
)
from ai_news_agent.config.schema import (
    SchemaValidationError,
    export_schemas,
    generate_agent_schema,
    generate_scheduler_schema,
    validate_agent_yaml,
    validate_scheduler_yaml,
)

__all__ = [
    # Loaders
    "ConfigError",
    "discover_agents_from_scheduler",
    "load_agent_config",
    "load_all_enabled_agents",
    "load_scheduler_config",
    "validate_no_secrets_in_yaml",
    # Models
    "AgentConfig",
    "AgentRegistration",
    "APIConfig",
    "LimitsConfig",
    "LLMCadenceOverride",
    "LLMConfig",
    "RetryConfig",
    "RuntimeSecrets",
    "SchedulerConfig",
    "SourcesConfig",
    "TriggersConfig",
    "TwitterConfig",
    "TwitterHandleConfig",
    # Schema
    "SchemaValidationError",
    "export_schemas",
    "generate_agent_schema",
    "generate_scheduler_schema",
    "validate_agent_yaml",
    "validate_scheduler_yaml",
]

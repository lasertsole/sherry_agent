from .media_pipeline.mixins import BeforeAgentHooksMixin as BeforeAgentHooksMixin
from .media_pipeline.mixins import AfterAgentHooksMixin as AfterAgentHooksMixin
from .base import require_session_id as require_session_id
from .base import args_hash as args_hash

from .summarization import Summarization as Summarization
from .llm_retry import LLMRetryMiddleware as LLMRetryMiddleware
from .llm_retry.core import (
    ContentFilterError as ContentFilterError,
    FallbackCandidate as FallbackCandidate,
    LLMRetryConfig as LLMRetryConfig,
)
from .output_repetition_guard import OutputRepetitionGuard as OutputRepetitionGuard
from .max_tokens_boost import MaxTokensBoostMiddleware as MaxTokensBoostMiddleware
from .tool_guardrails import ToolGuardrails as ToolGuardrails
from .context_eviction import ContextEvictionMiddleware as ContextEvictionMiddleware
from .iteration_budget import IterationBudget as IterationBudget
from .system_prompt import system_prompt_injection as system_prompt_injection
from .message_persistence import MessagePersistenceMiddleware as MessagePersistenceMiddleware
from .tool_call_normalize import ToolCallNormalize as ToolCallNormalize
from .path_guard import PathGuard as PathGuard
from .heartbeat_staleness import HeartbeatStaleness as HeartbeatStaleness
from .media_pipeline import MultimodalProcessor as MultimodalProcessor
from .humanInTheLoop import HumanInTheLoop as HumanInTheLoop, HITLConfig as HITLConfig

from . import llm_capability_cache as llm_capability_cache

# Middleware scaffolding protection (P1-6). Imported LAST so every required
# middleware class above is already bound: scaffolding.py imports the classes
# from their concrete submodules, and re-exporting it here (rather than having
# it import this package) keeps the dependency one-way.
from .scaffolding import (
    RequiredMiddlewareEntry as RequiredMiddlewareEntry,
    ScaffoldingViolationError as ScaffoldingViolationError,
    MAIN_REQUIRED_CLASSES as MAIN_REQUIRED_CLASSES,
    MAIN_REQUIRED_NAMES as MAIN_REQUIRED_NAMES,
    SUBAGENT_REQUIRED_CLASSES as SUBAGENT_REQUIRED_CLASSES,
    SUBAGENT_REQUIRED_NAMES as SUBAGENT_REQUIRED_NAMES,
    _MAIN_REQUIRED as _MAIN_REQUIRED,
    _SUBAGENT_REQUIRED as _SUBAGENT_REQUIRED,
    validate_required_middleware as validate_required_middleware,
    verify_required_names_coverage as verify_required_names_coverage,
)

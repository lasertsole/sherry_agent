"""Compat shim — values live in config/features (single source of truth).

Every name below is an alias bound to a ``config.features`` registry field, so
existing ``from config.num import ...`` consumers keep working unchanged while
the registry owns the values.
"""

from .features import SUMMARIZATION, TOKEN_ESTIMATION
from .features.infra_side import BUS, HTTP_UPLOAD

# Compression and RAG thresholds
ARCHIVE_THRESHOLD = SUMMARIZATION["archive_threshold"]
MEMORY_THRESHOLD = SUMMARIZATION["memory_threshold"]
COMPRESS_RATIO = SUMMARIZATION["compress_ratio"]

# === Trigger Thresholds ===
PREEMPTIVE_TRUNCATE_RATIO = SUMMARIZATION["preemptive_truncate_ratio"]
COMPRESSION_TRIGGER_RATIO = SUMMARIZATION["compression_trigger_ratio"]
COMPRESSION_RESERVE_TOKENS = SUMMARIZATION["compression_reserve_tokens"]

# === Budget-based Tail ===
MIN_PRESERVE_TOKENS = SUMMARIZATION["min_preserve_tokens"]
MAX_PRESERVE_TOKENS = SUMMARIZATION["max_preserve_tokens"]
PRESERVE_RATIO = SUMMARIZATION["preserve_ratio"]

# === Multi-strategy Pipeline ===
PRUNE_PROTECT_TOKENS = SUMMARIZATION["prune_protect_tokens"]
PRUNE_MIN_REDUCTION_TOKENS = SUMMARIZATION["prune_min_reduction_tokens"]
TARGET_TRUNCATE_RATIO = SUMMARIZATION["target_truncate_ratio"]
MIN_OUTPUT_CHARS_TO_TRUNCATE = SUMMARIZATION["min_output_chars_to_truncate"]
MAX_TOOL_OUTPUT_CHARS = SUMMARIZATION["max_tool_output_chars"]
MIN_ARGS_CHARS_TO_TRUNCATE = SUMMARIZATION["min_args_chars_to_truncate"]
MAX_TOOL_ARGS_CHARS = SUMMARIZATION["max_tool_args_chars"]
AGGRESSIVE_TRUNCATE_CHARS = SUMMARIZATION["aggressive_truncate_chars"]

# === LLM Summary Improvement ===
SUMMARY_TRIM_TOKENS = SUMMARIZATION["summary_trim_tokens"]
SUMMARY_TOTAL_MAX_CHARS = SUMMARIZATION["summary_total_max_chars"]
CONTENT_HEAD_RATIO = SUMMARIZATION["content_head_ratio"]
CONTENT_TAIL_RATIO = SUMMARIZATION["content_tail_ratio"]

# === Degradation Monitoring ===
DEGRADATION_MONITOR_COUNT = SUMMARIZATION["degradation_monitor_count"]
DEGRADATION_NO_TEXT_THRESHOLD = SUMMARIZATION["degradation_no_text_threshold"]
MAX_RECOVERY_ATTEMPTS = SUMMARIZATION["max_recovery_attempts"]

# === Anti-thrashing (progressive escalation) ===
MAX_TOTAL_COMPRESSION_ATTEMPTS = SUMMARIZATION["max_total_compression_attempts"]
INEFFECTIVE_THRESHOLD = SUMMARIZATION["ineffective_threshold"]
MIN_EFFECTIVENESS_PCT = SUMMARIZATION["min_effectiveness_pct"]

# === Protected Tools ===
PROTECTED_TOOLS = SUMMARIZATION["protected_tools"]

# === Last Turn Detection ===
LAST_TURN_RATIO_THRESHOLD = SUMMARIZATION["last_turn_ratio_threshold"]

# === FIFO Section Limits ===
COMPLETED_MAX_ITEMS = SUMMARIZATION["completed_max_items"]
KEY_DECISIONS_MAX_ITEMS = SUMMARIZATION["key_decisions_max_items"]
CRITICAL_CONTEXT_MAX_ITEMS = SUMMARIZATION["critical_context_max_items"]

# === File Operations Ratchet ===
FILE_OPS_LIST_MAX_CHARS = SUMMARIZATION["file_ops_list_max_chars"]
FILE_OPS_SECTION_MAX_CHARS = SUMMARIZATION["file_ops_section_max_chars"]

# === Latest User Request ===
LATEST_USER_REQUEST_MAX_CHARS = SUMMARIZATION["latest_user_request_max_chars"]

# === Auto-continue ===
AUTO_CONTINUE_PROMPT = SUMMARIZATION["auto_continue_prompt"]

# === Token estimation ===
CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]

# === Upload size limits (bytes) ===
MAX_IMAGE_UPLOAD_BYTES = HTTP_UPLOAD["max_image_bytes"]
MAX_AUDIO_UPLOAD_BYTES = HTTP_UPLOAD["max_audio_bytes"]
MAX_VIDEO_UPLOAD_BYTES = HTTP_UPLOAD["max_video_bytes"]

# Message bus bounded-queue size (bus/core.py MessageBus). When a queue is
# full the producer awaits free space (backpressure) — messages are delayed,
# never dropped, and memory stays bounded (audit #11).
BUS_QUEUE_MAXSIZE = BUS["queue_maxsize"]

# === Context compression: multi-trigger + dual-track (T1-T5) ===
# T4/T5 overflow-handling retry cap
MAX_OVERFLOW_RETRIES = SUMMARIZATION["max_overflow_retries"]
# Max compression attempts per turn
MAX_COMPRESS_ATTEMPTS_PER_TURN = SUMMARIZATION["max_compress_attempts_per_turn"]
# Cooldown model-call rounds after compaction
COMPACTION_COOLDOWN_ROUNDS = SUMMARIZATION["compaction_cooldown_rounds"]
# Tool-result TTL (5 minutes)
PRUNE_TTL_SECONDS = SUMMARIZATION["prune_ttl_seconds"]
# Tool-result truncation budget as a share of usable_budget
TRUNCATE_BUDGET_RATIO = SUMMARIZATION["truncate_budget_ratio"]
# Below this token count truncation isn't worth it
MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE = SUMMARIZATION["min_tool_result_tokens_to_truncate"]
# Skip the 6 most recent messages (~2 turns)
TRUNCATABLE_RECENT_SKIP = SUMMARIZATION["truncatable_recent_skip"]
# TTL registry capacity cap, prevents unbounded growth
TTL_REGISTRY_MAX_ENTRIES = SUMMARIZATION["ttl_registry_max_entries"]

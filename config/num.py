# Compression and RAG thresholds
ARCHIVE_THRESHOLD = 8_000
MEMORY_THRESHOLD = 10_000
COMPRESS_RATIO = 0.5

# === Trigger Thresholds ===
PREEMPTIVE_TRUNCATE_RATIO = 0.70
COMPRESSION_TRIGGER_RATIO = 0.80
COMPRESSION_RESERVE_TOKENS = 16_000

# === Budget-based Tail ===
MIN_PRESERVE_TOKENS = 2_000
MAX_PRESERVE_TOKENS = 15_000
PRESERVE_RATIO = 0.25

# === Multi-strategy Pipeline ===
PRUNE_PROTECT_TOKENS = 40_000
PRUNE_MIN_REDUCTION_TOKENS = 5_000
TARGET_TRUNCATE_RATIO = 0.5
MIN_OUTPUT_CHARS_TO_TRUNCATE = 500
MAX_TOOL_OUTPUT_CHARS = 2_000
AGGRESSIVE_TRUNCATE_CHARS = 1_000

# === LLM Summary Improvement ===
SUMMARY_TRIM_TOKENS = 12_000
SUMMARY_TOTAL_MAX_CHARS = 16_000
CONTENT_HEAD_RATIO = 0.3
CONTENT_TAIL_RATIO = 0.3

# === Degradation Monitoring ===
DEGRADATION_MONITOR_COUNT = 5
DEGRADATION_NO_TEXT_THRESHOLD = 3
MAX_RECOVERY_ATTEMPTS = 2

# === Anti-thrashing (progressive escalation) ===
MAX_TOTAL_COMPRESSION_ATTEMPTS = 5
INEFFECTIVE_THRESHOLD = 2
MIN_EFFECTIVENESS_PCT = 0.05

# === Protected Tools ===
PROTECTED_TOOLS = frozenset({"memory", "skill_view", "skill_list"})

# === Last Turn Detection ===
LAST_TURN_RATIO_THRESHOLD = 0.5

# === FIFO Section Limits ===
COMPLETED_MAX_ITEMS = 5
KEY_DECISIONS_MAX_ITEMS = 5
CRITICAL_CONTEXT_MAX_ITEMS = 3

# === File Operations Ratchet ===
FILE_OPS_LIST_MAX_CHARS = 900
FILE_OPS_SECTION_MAX_CHARS = 2_000

# === Latest User Request ===
LATEST_USER_REQUEST_MAX_CHARS = 800

# === Auto-continue ===
AUTO_CONTINUE_PROMPT = (
    "Continue if you have next steps, or stop and ask for clarification "
    "if you are unsure how to proceed."
)

# === Token estimation ===
CHARS_PER_TOKEN = 4

# Message bus bounded-queue size (bus/core.py MessageBus). When a queue is
# full the producer awaits free space (backpressure) — messages are delayed,
# never dropped, and memory stays bounded (audit #11).
BUS_QUEUE_MAXSIZE = 1000

# === Context compression: multi-trigger + dual-track (T1-T5) ===
MAX_OVERFLOW_RETRIES = 3  # T4/T5 溢出处理重试上限
MAX_COMPRESS_ATTEMPTS_PER_TURN = 3  # 单轮内最大压缩尝试次数
COMPACTION_COOLDOWN_ROUNDS = 3  # 压缩后冷却的模型调用轮数
PRUNE_TTL_SECONDS = 300  # 工具结果 TTL（5 分钟）
TRUNCATE_BUDGET_RATIO = 0.6  # 工具结果截断预算占 usable_budget 比例
MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE = 200  # 低于此 token 数不值得截断
TRUNCATABLE_RECENT_SKIP = 6  # 跳过最近 6 条消息（约 2 轮）
TTL_REGISTRY_MAX_ENTRIES = 512  # TTL 注册表容量上限，防无界增长

"""Infrastructure-side feature registry package.

Re-exports every infra-side ``TypedDict`` and its module-level default
instance so consumers can bind aliases from one place."""

from .gateway import (
    GatewayConfig as GatewayConfig,
    GATEWAY as GATEWAY,
)
from .bus import (
    BusConfig as BusConfig,
    BUS as BUS,
)
from .http_upload import (
    HttpUploadConfig as HttpUploadConfig,
    HTTP_UPLOAD as HTTP_UPLOAD,
)
from .retry_backoff import (
    RetryBackoffConfig as RetryBackoffConfig,
    RETRY_BACKOFF as RETRY_BACKOFF,
)
from .server_http import (
    ServerHttpConfig as ServerHttpConfig,
    SERVER_HTTP as SERVER_HTTP,
)
from .ws_stream import (
    WsStreamConfig as WsStreamConfig,
    WS_STREAM as WS_STREAM,
)
from .input_queue import (
    InputQueueConfig as InputQueueConfig,
    INPUT_QUEUE as INPUT_QUEUE,
)
from .heartbeat_service import (
    HeartbeatServiceConfig as HeartbeatServiceConfig,
    HEARTBEAT_SERVICE as HEARTBEAT_SERVICE,
)
from .cron_service import (
    CronServiceConfig as CronServiceConfig,
    CRON_SERVICE as CRON_SERVICE,
)
from .skill_scanner import (
    SkillScannerConfig as SkillScannerConfig,
    SKILL_SCANNER as SKILL_SCANNER,
)
from .mes_memory import (
    MesMemoryConfig as MesMemoryConfig,
    MES_MEMORY as MES_MEMORY,
)
from .curator_defaults import (
    CuratorDefaultsConfig as CuratorDefaultsConfig,
    CURATOR_DEFAULTS as CURATOR_DEFAULTS,
)
from .message_pipeline import (
    MessagePipelineConfig as MessagePipelineConfig,
    MESSAGE_PIPELINE as MESSAGE_PIPELINE,
)
from .periodic_backoff import (
    PeriodicBackoffConfig as PeriodicBackoffConfig,
    PERIODIC_BACKOFF as PERIODIC_BACKOFF,
)
from .crash_loop import (
    CrashLoopConfig as CrashLoopConfig,
    CRASH_LOOP as CRASH_LOOP,
)
from .skills_tooling import (
    SkillsToolingConfig as SkillsToolingConfig,
    SKILLS_TOOLING as SKILLS_TOOLING,
)
from .channels import (
    ChannelsConfig as ChannelsConfig,
    CHANNELS as CHANNELS,
)
from .model_pricing import (
    ModelPricingConfig as ModelPricingConfig,
    MODEL_PRICING as MODEL_PRICING,
)

from .session.core import SessionRegister, clear_all_register_sessions
from .session.state_register import (
    StateRegisterMeM,
    state_register_mem,
    StateRegisterDB,
    state_register_db,
)
from .session.state_keys import StateKey, TypedState, hitl_tool_approved_key
from .session.count_call_register import CountCallRegister, count_call_register
from .session.relation_register import RelationManager, relation_register
from .session.timer_call_register import TimerCallRegister, timer_call_register
from .process.crash_loop_breaker import (
    record_boot,
    is_tripped,
    clear,
    mark_clean_exit,
    was_last_exit_clean,
)
from .process.periodic_backoff import PeriodicBackoff
from .lane.core import (
    Lane,
    LaneManager,
    LaneType,
    get_lane_manager,
    lane_slot,
    set_drain_check,
)

# 向后兼容别名：旧代码引用 ``runtime.Register``
Register = SessionRegister

__all__ = [
    "SessionRegister",
    "Register",
    "clear_all_register_sessions",
    "StateRegisterMeM",
    "state_register_mem",
    "StateRegisterDB",
    "state_register_db",
    "StateKey",
    "TypedState",
    "hitl_tool_approved_key",
    "CountCallRegister",
    "count_call_register",
    "RelationManager",
    "relation_register",
    "TimerCallRegister",
    "timer_call_register",
    "record_boot",
    "is_tripped",
    "clear",
    "mark_clean_exit",
    "was_last_exit_clean",
    "PeriodicBackoff",
    "Lane",
    "LaneManager",
    "LaneType",
    "get_lane_manager",
    "lane_slot",
    "set_drain_check",
]

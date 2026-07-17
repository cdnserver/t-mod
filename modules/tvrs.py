"""Backward-compatible facade for the modular TVRS implementation."""

from modules.consensus_runtime import (
    active_consensus_snapshot as active_consensus_snapshot,
    active_sessions as _active_sessions,
    coordinator as _consensus,
    registry as _consensus_registry,
    repository as _consensus_repository,
    restored_guilds as _restored_consensus_guilds,
    session_lock as consensus_session_lock,
)
from modules.tvrs_embeds import (
    build_final_summary_embed as build_final_summary_embed,
    build_result_embed as build_result_embed,
)
from modules.tvrs_formatting import (
    format_bill_number as format_bill_number,
    format_timer as format_timer,
)
from modules import tvrs_presentation as _tvrs_presentation
from modules import tvrs_hub_views as _tvrs_hub_views
from modules import tvrs_consensus_views as _tvrs_consensus_views
from modules import tvrs_discussion as _tvrs_discussion
from modules import tvrs_control as _tvrs_control
from modules import tvrs_decision as _tvrs_decision
from modules import tvrs_recovery as _tvrs_recovery
from modules import tvrs_setup as _tvrs_setup

_TVRS_MODULES = (
    _tvrs_presentation,
    _tvrs_hub_views,
    _tvrs_consensus_views,
    _tvrs_discussion,
    _tvrs_control,
    _tvrs_decision,
    _tvrs_recovery,
    _tvrs_setup,
)

for _tvrs_module in _TVRS_MODULES:
    for _exported_name in _tvrs_module.__all__:
        globals()[_exported_name] = getattr(_tvrs_module, _exported_name)

__all__ = [
    "_active_sessions",
    "_consensus",
    "_consensus_registry",
    "_consensus_repository",
    "_restored_consensus_guilds",
    "consensus_session_lock",
    "active_consensus_snapshot",
    "build_final_summary_embed",
    "build_result_embed",
    "format_bill_number",
    "format_timer",
    *(_name for _module in _TVRS_MODULES for _name in _module.__all__),
]

del _exported_name
del _tvrs_module

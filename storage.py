"""Backward-compatible facade for the bounded persistence repositories."""

from __future__ import annotations

import sys
import types

from persistence import core as _core
from persistence import activity_repository as _activity_repository
from persistence import outbox_repository as _outbox_repository
from persistence import sgl_repository as _sgl_repository
from persistence import sgl_archive_repository as _sgl_archive_repository
from persistence import admin_repository as _admin_repository
from persistence import tvrs_repository as _tvrs_repository
from persistence import finance_repository as _finance_repository
from persistence import craft_repository as _craft_repository
from persistence import audit_repository as _audit_repository
from persistence import market_repository as _market_repository
from persistence import profile_repository as _profile_repository
from persistence import schema as _schema

_PERSISTENCE_MODULES = (
    _core,
    _activity_repository,
    _outbox_repository,
    _sgl_repository,
    _sgl_archive_repository,
    _admin_repository,
    _tvrs_repository,
    _finance_repository,
    _craft_repository,
    _audit_repository,
    _market_repository,
    _profile_repository,
    _schema,
)

for _persistence_module in _PERSISTENCE_MODULES:
    for _exported_name in _persistence_module.__all__:
        globals()[_exported_name] = getattr(_persistence_module, _exported_name)

_MUTABLE_CORE_NAMES = frozenset({
    "DATA_DIR",
    "DATABASE_FILE",
    "LEGACY_ACTIVITY_FILE",
    "CONSENSUS_V2_RESET_ID",
    "CONSENSUS_RESULT_DEDUP_ID",
})


class _StorageFacade(types.ModuleType):
    """Keep legacy runtime configuration assignments connected to core."""

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name in _MUTABLE_CORE_NAMES:
            setattr(_core, name, value)


sys.modules[__name__].__class__ = _StorageFacade

del _exported_name
del _persistence_module

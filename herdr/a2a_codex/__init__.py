"""Herdr CLI client and A2A routing framework."""
from .errors import *
from .herdr_client import HerdrClient
from .models import Agent, ResourceRef
from .launcher import LauncherError, build_launch_command, preflight_launcher
from .topology import Edge, RoleSpec, Topology, TopologyError, TopologyStore
from .identity import AgentIdentity, IdentityError, IdentityMismatchError
from .registry import (
    AgentAlreadyRegisteredError,
    AgentNotRegisteredError,
    AgentRecord,
    AmbiguousPaneError,
    Registry,
    RegistryError,
    RuntimeAddressConflictError,
)
from .messages import (
    ALLOWED_TRANSITIONS,
    ALL_STATES,
    DELIVERY_UNCERTAIN,
    NON_TERMINAL_STATES,
    TERMINAL_STATES,
    Message,
    can_transition,
)
from .spool import MessageNotFoundError, Spool, SpoolError
from .audit import AuditLog
from .router import Router, SendReceipt, SendRejected
from .broker import DeliveryBroker, DeliveryResult, PromptDisposition, classify_prompt_result
from .runtime import BrokerAlreadyRunningError, BrokerRuntime, DispatchHaltedError
from .rulings import RulingError, RulingManager

__all__ = ["HerdrClient", "Agent", "ResourceRef", "LauncherError",
           "build_launch_command", "preflight_launcher", "Edge", "RoleSpec", "Topology",
           "TopologyError", "TopologyStore", "AgentIdentity", "IdentityError", "IdentityMismatchError",
           "AgentAlreadyRegisteredError", "AgentNotRegisteredError", "AgentRecord",
           "AmbiguousPaneError", "Registry", "RegistryError", "RuntimeAddressConflictError",
           "Message", "ALLOWED_TRANSITIONS", "ALL_STATES", "NON_TERMINAL_STATES",
           "TERMINAL_STATES", "DELIVERY_UNCERTAIN", "can_transition",
           "MessageNotFoundError", "Spool", "SpoolError", "AuditLog",
           "Router", "SendReceipt", "SendRejected", "HerdrPromptOutcomeUnknown",
           "HerdrAgentPromptFailed", "HerdrAgentNameTaken", "HerdrInvalidAgentName",
           "DeliveryBroker", "DeliveryResult", "PromptDisposition", "classify_prompt_result"]
__all__ += ["BrokerRuntime", "BrokerAlreadyRunningError", "DispatchHaltedError"]
__all__ += ["RulingManager", "RulingError"]

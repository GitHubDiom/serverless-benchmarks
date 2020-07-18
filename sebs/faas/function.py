from abc import ABC
from abc import abstractmethod
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional  # noqa


"""
    Times are reported in microseconds.
"""


class ExecutionTimes:

    client: int
    provider: int
    benchmark: int

    def __init__(self):
        self.client = 0
        self.provider = 0
        self.benchmark = 0


class ExecutionStats:

    memory_used: Optional[float]
    init_time_reported: Optional[int]
    init_time_measured: int
    cold_start: bool
    failure: bool

    def __init__(self):
        self.memory_used = None
        self.init_time_reported = None
        self.init_time_measured = 0
        self.cold_start = False
        self.failure = False


class ExecutionBilling:

    _memory: Optional[int]
    _billed_time: Optional[int]
    _gb_seconds: int

    def __init__(self):
        self.memory = None
        self.billed_time = None
        self.gb_seconds = 0

    @property
    def memory(self) -> Optional[int]:
        return self._memory

    @memory.setter
    def memory(self, val: int):
        self._memory = val

    @property
    def billed_time(self) -> Optional[int]:
        return self._billed_time

    @billed_time.setter
    def billed_time(self, val: int):
        self._billed_time = val

    @property
    def gb_seconds(self) -> int:
        return self._gb_seconds

    @gb_seconds.setter
    def gb_seconds(self, val: int):
        self._gb_seconds = val


class ExecutionResult:

    output: dict
    request_id: str
    times: ExecutionTimes
    stats: ExecutionStats
    billing: ExecutionBilling

    def __init__(self, client_time_begin: datetime, client_time_end: datetime):
        self.output = {}
        self.request_id = ""
        self.times = ExecutionTimes()
        self.times.client = int(
            (client_time_end - client_time_begin) / timedelta(microseconds=1)
        )
        self.stats = ExecutionStats()
        self.billing = ExecutionBilling()

    def parse_benchmark_output(self, output: dict):
        self.output = output
        self.stats.cold_start = self.output["is_cold"]
        self.times.benchmark = int(
            (
                datetime.fromtimestamp(float(self.output["end"]))
                - datetime.fromtimestamp(float(self.output["begin"]))
            )
            / timedelta(microseconds=1)
        )


"""
    Function trigger and implementation of invocation.

    FIXME: implement a generic HTTP invocation and specialize input and output
    processing in classes.
"""


class Trigger(ABC):
    class TriggerType(Enum):
        HTTP = 0
        LIBRARY = 1
        STORAGE = 2
        TIMER = 3
        DB = 4

    # FIXME: 3.7+, future annotations
    @staticmethod
    @abstractmethod
    def trigger_type() -> "Trigger.TriggerType":
        pass

    @abstractmethod
    def sync_invoke(self, payload: dict) -> ExecutionResult:
        pass

    @abstractmethod
    def async_invoke(self, payload: dict) -> ExecutionResult:
        pass

    @abstractmethod
    def serialize(self) -> dict:
        pass

    @staticmethod
    def deserialize(cached_config: dict) -> "Trigger":
        pass


"""
    Abstraction base class for FaaS function. Contains a list of associated triggers
    and might implement non-trigger execution if supported by the SDK.
    Example: direct function invocation through AWS boto3 SDK.
"""


class Function:
    def __init__(self, name: str, code_hash: str):
        self._name = name
        self._code_package_hash = code_hash
        self._updated_code = False
        self._triggers: List[Trigger] = []

    @property
    def name(self):
        return self._name

    @property
    def code_package_hash(self):
        return self._code_package_hash

    @code_package_hash.setter
    def code_package_hash(self, new_hash: str):
        self._code_package_hash = new_hash

    @property
    def updated_code(self) -> bool:
        return self._updated_code

    @updated_code.setter
    def updated_code(self, val: bool):
        self._updated_code = val

    @property
    def triggers(self) -> List[Trigger]:
        return self._triggers

    def add_trigger(self, trigger: Trigger):
        self._triggers.append(trigger)

    def serialize(self) -> dict:
        return {
            "name": self._name,
            "hash": self._code_package_hash,
            "triggers": [x.serialize() for x in self._triggers],
        }

    @staticmethod
    @abstractmethod
    def deserialize(cached_config: dict) -> "Function":
        pass

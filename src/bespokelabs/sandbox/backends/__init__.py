from __future__ import annotations

from bespokelabs.sandbox.backends.daytona import DaytonaAdapter
from bespokelabs.sandbox.backends.e2b import E2BAdapter
from bespokelabs.sandbox.backends.modal import ModalAdapter
from bespokelabs.sandbox.backends.tensorlake import TensorlakeAdapter

BACKENDS: dict[str, type] = {
    "daytona": DaytonaAdapter,
    "tensorlake": TensorlakeAdapter,
    "modal": ModalAdapter,
    "e2b": E2BAdapter,
}

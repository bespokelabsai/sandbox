from __future__ import annotations

from bespokelabs.sandbox.backends.daytona import DaytonaAdapter
from bespokelabs.sandbox.backends.docker import DockerAdapter
from bespokelabs.sandbox.backends.e2b import E2BAdapter
from bespokelabs.sandbox.backends.local import LocalAdapter
from bespokelabs.sandbox.backends.modal import ModalAdapter
from bespokelabs.sandbox.backends.ray import RayAdapter
from bespokelabs.sandbox.backends.safehouse import SafehouseAdapter
from bespokelabs.sandbox.backends.tensorlake import TensorlakeAdapter

BACKENDS: dict[str, type] = {
    "daytona": DaytonaAdapter,
    "tensorlake": TensorlakeAdapter,
    "modal": ModalAdapter,
    "e2b": E2BAdapter,
    "docker": DockerAdapter,
    "local": LocalAdapter,
    "ray": RayAdapter,
    "safehouse": SafehouseAdapter,
}

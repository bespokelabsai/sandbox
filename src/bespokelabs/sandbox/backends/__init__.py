"""Backend client registry."""

from __future__ import annotations

from bespokelabs.sandbox.backends.boxd import BoxdClient
from bespokelabs.sandbox.backends.daytona import DaytonaClient
from bespokelabs.sandbox.backends.docker import DockerClient
from bespokelabs.sandbox.backends.e2b import E2BClient
from bespokelabs.sandbox.backends.gcp_batch import GcpBatchClient
from bespokelabs.sandbox.backends.local import LocalClient
from bespokelabs.sandbox.backends.modal import ModalClient
from bespokelabs.sandbox.backends.ray import RayClient
from bespokelabs.sandbox.backends.runpod import RunpodClient
from bespokelabs.sandbox.backends.safehouse import SafehouseClient
from bespokelabs.sandbox.backends.tensorlake import TensorlakeClient

# Maps backend name -> backend client class. A client is the per-provider
# factory: instantiating it verifies the SDK is importable; its create()
# method produces live sandbox sessions. A class with
# ``supports_sessions = False`` (gcp_batch) only runs detached jobs.
BACKENDS: dict[str, type] = {
    "daytona": DaytonaClient,
    "tensorlake": TensorlakeClient,
    "modal": ModalClient,
    "e2b": E2BClient,
    "docker": DockerClient,
    "local": LocalClient,
    "ray": RayClient,
    "runpod": RunpodClient,
    "safehouse": SafehouseClient,
    "boxd": BoxdClient,
    "gcp_batch": GcpBatchClient,
}

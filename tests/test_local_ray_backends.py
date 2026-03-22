from __future__ import annotations

import types
import unittest
from unittest import mock

from bespokelabs.sandbox import Sandbox


class _FakeObjectRef:
    def __init__(self, value: object) -> None:
        self.value = value


class _FakeRemoteMethod:
    def __init__(self, method: object) -> None:
        self._method = method

    def remote(self, *args: object, **kwargs: object) -> _FakeObjectRef:
        return _FakeObjectRef(self._method(*args, **kwargs))


class _FakeActorHandle:
    def __init__(self, instance: object) -> None:
        self._instance = instance

    def __getattr__(self, name: str) -> _FakeRemoteMethod:
        return _FakeRemoteMethod(getattr(self._instance, name))


class _FakeRemoteActorClass:
    def __init__(self, cls: type, fake_ray: "_FakeRayModule") -> None:
        self._cls = cls
        self._fake_ray = fake_ray

    def options(self, *, num_cpus: float | None = None) -> "_FakeRemoteActorClass":
        self._fake_ray.last_actor_num_cpus = num_cpus
        return self

    def remote(self, *args: object, **kwargs: object) -> _FakeActorHandle:
        return _FakeActorHandle(self._cls(*args, **kwargs))


class _FakeRayModule(types.SimpleNamespace):
    def __init__(self) -> None:
        super().__init__()
        self._initialized = False
        self.last_actor_num_cpus: float | None = None
        self.last_init_address: str | None = None

    def is_initialized(self) -> bool:
        return self._initialized

    def init(self, address: str | None = None) -> None:
        self._initialized = True
        self.last_init_address = address

    def get(self, value: object) -> object:
        if isinstance(value, _FakeObjectRef):
            return value.value
        return value

    def remote(self, cls: type) -> _FakeRemoteActorClass:
        return _FakeRemoteActorClass(cls, self)


class LocalBackendTests(unittest.TestCase):
    def test_execute_code_falls_back_to_python3(self) -> None:
        sb = Sandbox("local")
        self.addCleanup(sb.destroy)

        result = sb.execute_code('print("hello")')

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_absolute_paths_are_consistent_across_helpers_and_execution(self) -> None:
        sb = Sandbox("local")
        self.addCleanup(sb.destroy)

        sb.write_file("/hello.txt", "hi")

        code_result = sb.execute_code("print(open('/hello.txt').read())")
        command_result = sb.execute_command("sh", ["-c", "echo shell >/hello2.txt"])

        self.assertEqual(code_result.exit_code, 0)
        self.assertEqual(code_result.stdout.strip(), "hi")
        self.assertEqual(command_result.exit_code, 0)
        self.assertEqual(sb.read_file("/hello2.txt"), b"shell\n")


class RayBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_ray = _FakeRayModule()
        self.patch = mock.patch.dict("sys.modules", {"ray": self.fake_ray})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_create_applies_actor_cpu_limit(self) -> None:
        sb = Sandbox("ray", cpu=3.0)
        self.addCleanup(sb.destroy)

        self.assertEqual(self.fake_ray.last_actor_num_cpus, 3.0)

    def test_execute_code_falls_back_to_python3(self) -> None:
        sb = Sandbox("ray")
        self.addCleanup(sb.destroy)

        result = sb.execute_code('print("hello")')

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_absolute_paths_are_consistent_across_helpers_and_execution(self) -> None:
        sb = Sandbox("ray")
        self.addCleanup(sb.destroy)

        sb.write_file("/hello.txt", "hi")

        code_result = sb.execute_code("print(open('/hello.txt').read())")
        command_result = sb.execute_command("sh", ["-c", "echo shell >/hello2.txt"])

        self.assertEqual(code_result.exit_code, 0)
        self.assertEqual(code_result.stdout.strip(), "hi")
        self.assertEqual(command_result.exit_code, 0)
        self.assertEqual(sb.read_file("/hello2.txt"), b"shell\n")


if __name__ == "__main__":
    unittest.main()

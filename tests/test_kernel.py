"""kernel.py 的测试。运行：python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import threading
import unittest

from kernel import Kernel, RouteConflictError, ServiceError


class TestEvents(unittest.TestCase):
    def test_emit_basic(self):
        k = Kernel()
        got = []
        k.on("e", lambda **kw: got.append(kw))
        self.assertTrue(k.emit("e", a=1))
        self.assertEqual(got, [{"a": 1}])

    def test_emit_no_listener(self):
        self.assertFalse(Kernel().emit("nobody"))

    def test_once(self):
        k = Kernel()
        got = []
        k.once("e", lambda **kw: got.append(kw))
        k.emit("e")
        k.emit("e")
        self.assertEqual(len(got), 1)

    def test_disposer_from_on(self):
        k = Kernel()
        got = []
        undo = k.on("e", lambda **kw: got.append(kw))
        undo()
        self.assertFalse(k.emit("e"))

    def test_listener_error_isolated(self):
        k = Kernel()
        got = []
        def boom(**kw):
            raise RuntimeError("订阅者坏了")
        k.on("e", boom)
        k.on("e", lambda **kw: got.append(kw))
        self.assertTrue(k.emit("e", x=1))
        self.assertEqual(got, [{"x": 1}])

    def test_scope_dispose_removes_listeners(self):
        k = Kernel()
        scope = k.plugin(type("M", (), {"register": staticmethod(lambda s: None)})())
        got = []
        scope.on("e", got.append)
        scope.dispose()
        self.assertFalse(k.emit("e"))


class TestServices(unittest.TestCase):
    def test_lazy_and_singleton(self):
        k = Kernel()
        calls = []
        k.service("db", lambda kern: calls.append("build") or {"n": len(calls)})
        self.assertEqual(calls, [])           # 未访问不构造
        a = k.use("db")
        b = k.use("db")
        self.assertEqual(calls, ["build"])    # 只构造一次
        self.assertIs(a, b)

    def test_start_hook_once(self):
        class Svc:
            def start(self): self._count = getattr(self, "_count", 0) + 1
        k = Kernel()
        k.service("s", lambda kern: Svc())
        k.use("s"); k.use("s")
        self.assertEqual(k.use("s")._count, 1)

    def test_reentrancy_detected(self):
        k = Kernel()
        def factory(kern):
            kern.use("s")  # 自己依赖自己
            return {}
        k.service("s", factory)
        with self.assertRaises(ServiceError):
            k.use("s")

    def test_stop_on_dispose(self):
        k = Kernel()
        stopped = []
        class Svc:
            def stop(self): stopped.append(1)
        scope = k.plugin(type("M", (), {"register": staticmethod(lambda s: None)})())
        scope.service("s", lambda kern: Svc())
        k.use("s")
        scope.dispose()
        self.assertEqual(stopped, [1])
        with self.assertRaises(KeyError):
            k.use("s")

    def test_duplicate_service_rejected(self):
        k = Kernel()
        k.service("s", lambda kern: 1)
        with self.assertRaises(ServiceError):
            k.service("s", lambda kern: 2)


class TestPluginsAndRoutes(unittest.TestCase):
    def _module(self, log, value="v"):
        class M:
            @staticmethod
            def register(scope):
                scope.route("POST", "/api/demo")(lambda: value)
                scope.on("evt", lambda **kw: log.append(("evt", kw)))
        M.__name__ = "demo"
        return M

    def test_plugin_registers_routes_and_listeners(self):
        k, log = Kernel(), []
        scope = k.plugin(self._module(log))
        self.assertTrue(callable(k.resolve("POST", "/api/demo")))
        self.assertTrue(k.emit("evt", a=1))
        scope.dispose()
        self.assertIsNone(k.resolve("POST", "/api/demo"))
        self.assertFalse(k.emit("evt"))

    def test_route_conflict(self):
        k = Kernel()
        m1, m2 = self._module([]), self._module([])
        k.plugin(m1)
        with self.assertRaises(RouteConflictError):
            k.plugin(m2)

    def test_inject_defers_until_service_ready(self):
        k, log = Kernel(), []
        scope = k.plugin(self._module(log), inject=("store",))
        self.assertFalse(k.emit("evt"))                    # 挂起中，未激活
        self.assertIsNone(k.resolve("POST", "/api/demo"))
        k.service("store", lambda kern: object())
        self.assertTrue(k.emit("evt", a=1))                # 服务就位 → 自动激活
        self.assertTrue(callable(k.resolve("POST", "/api/demo")))

    def test_callable_module(self):
        k = Kernel()
        k.route("GET", "/x")(lambda: 1) if False else None
        def module(scope):
            scope.on("e", lambda **kw: None)
        scope = k.plugin(module)
        self.assertTrue(k.emit("e"))
        scope.dispose()


class TestEffect(unittest.TestCase):
    def test_effect_collects_cleanup_in_reverse(self):
        k = Kernel()
        order = []
        scope = k.plugin(type("M", (), {"register": staticmethod(lambda s: None)})())
        scope.effect(lambda: (order.append("setup1"), lambda: order.append("clean1"))[1])
        scope.effect(lambda: (order.append("setup2"), lambda: order.append("clean2"))[1])
        scope.dispose()
        self.assertEqual(order, ["setup1", "setup2", "clean2", "clean1"])


class TestConcurrency(unittest.TestCase):
    def test_service_factory_called_once_across_threads(self):
        k = Kernel()
        calls, barrier = [], threading.Event()
        def factory(kern):
            calls.append(1)
            barrier.wait(2)          # 逼两个线程挤进同一次初始化窗口
            return {}
        k.service("s", factory)
        results = []
        def grab():
            results.append(k.use("s"))
        t1, t2 = threading.Thread(target=grab), threading.Thread(target=grab)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(calls), 1)
        self.assertIs(results[0], results[1])


if __name__ == "__main__":
    unittest.main()

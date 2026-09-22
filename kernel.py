"""问底 · 内核：事件总线 + 服务注册 + 作用域生命周期。

抽象出处：cordis (@cordisjs/core → cordiverse/cordis, MIT, https://github.com/cordiverse/cordis)。
只抄四个抽象，全部标准库实现，语义对照：
  EventMap  -> on/once/emit      on() 返回注销函数；emit 返回是否有人接收
  Registry  -> plugin(module)    模块 = register(scope)；inject 声明依赖，
                                 服务未就位则挂起，就位后自动激活（cordis registry 的延迟应用）
  Fiber     -> Scope             资源跟踪：scope 内的监听/路由/服务在 dispose 时逆序注销
  Service   -> service/use       按名提供、惰性构造、首访自动 start()、重入检测

与 cordis 的故意偏离：
  - emit 隔离订阅者异常（cordis 同步 emit 会向上抛）。订阅者（如图谱模块）出 bug
    不允许拖垮解释主干，只记日志。

立场：
  - 内核薄：树/划词/配额/auth 留在 server.py，不进内核
  - 模块厚：graph / notes / share 各自 register(scope)，订阅事件、注册路由
"""
from __future__ import annotations

import logging
import secrets
import threading
from typing import Any, Callable

log = logging.getLogger("wendi.kernel")

Disposer = Callable[[], None]
Factory = Callable[["Kernel"], Any]

# 事件目录：既有语义文档，也是订阅方的契约（cordis 的 Events interface）
EVENTS = {
    "explain.done":  "(mode, user_id, term, parent_term, style, ms, stream, chunks)",
    "explain.error": "(mode, user_id, term, error)",
}


class ServiceError(RuntimeError):
    pass


class RouteConflictError(ServiceError):
    pass


class Scope:
    """cordis 的 Context/Fiber：一个可整体注销的资源作用域。"""

    def __init__(self, kernel: "Kernel", name: str = ""):
        self.kernel = kernel
        self.name = name or "scope-" + secrets.token_hex(3)
        self._undo: list[Disposer] = []
        self._disposed = False

    # —— 资源跟踪（cordis fiber.effect：setup 返回 cleanup，自动收集） ——
    def effect(self, setup: Callable) -> Any:
        """执行 setup()；若其返回可调用对象，则作为清理函数在 dispose 时逆序执行。"""
        result = setup()
        if callable(result):
            self._undo.append(result)
        return result

    # —— 事件（EventMap） ——
    def on(self, event: str, handler: Callable[..., None]) -> Disposer:
        """订阅事件。返回注销函数（cordis 语义）。"""
        return self.kernel._add_listener(event, handler, self)

    def once(self, event: str, handler: Callable[..., None]) -> Disposer:
        kernel = self.kernel

        def h(**kw):
            kernel._remove_listener(event, h)
            handler(**kw)
        return self.on(event, h)

    # —— 服务（Service） ——
    def service(self, name: str, factory: Factory) -> None:
        """按名注册服务提供者。factory(kernel) 惰性调用，构造单例。"""
        self.kernel._add_service(name, factory, self)

    def use(self, name: str) -> Any:
        """解析服务：首次访问时构造；若定义了 start() 则自动调用（仅一次）。"""
        return self.kernel._get_service(name)

    # —— 路由 ——
    def route(self, method: str, path: str) -> Callable:
        """注册 HTTP 端点。重复注册同一 (method, path) 报错。"""
        def deco(fn):
            self.kernel._add_route(method, path, fn, self)
            return fn
        return deco

    # —— 插件（Registry） ——
    def plugin(self, module: Any, *, name: str = "", inject: tuple[str, ...] = ()) -> "Scope":
        """应用模块：module.register(scope)（无 register 则把模块本身当函数调用）。

        inject 声明的服务未全部就位时挂起，之后任一服务注册即重新检查
        （cordis registry 的延迟应用——模块加载顺序无关化）。
        返回模块作用域，dispose 它即可整体卸载。
        """
        return self.kernel._apply_plugin(module, name=name, inject=inject)

    # —— 生命周期 ——
    def dispose(self) -> None:
        """逆序注销本作用域的全部资源（cordis fiber.dispose 语义）。"""
        if self._disposed:
            return
        self._disposed = True
        for undo in reversed(self._undo):
            try:
                undo()
            except Exception:  # noqa: BLE001
                log.exception("dispose(%s)：清理函数出错（继续其余清理）", self.name)
        self._undo.clear()


class Kernel(Scope):
    """根作用域 + 全局注册表。"""

    def __init__(self):
        self._listeners: dict[str, list[tuple[Callable[..., None], Scope]]] = {}
        self._services: dict[str, dict] = {}
        self._routes: dict[tuple[str, str], tuple[Callable, Scope]] = {}
        self._pending: list[tuple[Any, str, tuple[str, ...], Scope]] = []
        self._lock = threading.RLock()
        super().__init__(self, "kernel")

    # —— 事件 ——
    def _add_listener(self, event: str, handler: Callable[..., None], scope: Scope) -> Disposer:
        entry = (handler, scope)
        self._listeners.setdefault(event, []).append(entry)

        def undo():
            try:
                self._listeners[event].remove(entry)
            except (KeyError, ValueError):
                pass
        scope._undo.append(undo)
        return undo

    def _remove_listener(self, event: str, handler: Callable[..., None]) -> None:
        for entry in list(self._listeners.get(event, ())):
            if entry[0] is handler:
                self._listeners[event].remove(entry)

    def emit(self, event: str, **payload) -> bool:
        """广播事件。返回是否有订阅者。订阅者异常被隔离（见模块 docstring 的偏离说明）。"""
        entries = list(self._listeners.get(event, ()))
        if not entries:
            return False
        for handler, _scope in entries:
            try:
                handler(**payload)
            except Exception:  # noqa: BLE001
                log.exception("事件 %s 的订阅者出错（已隔离）", event)
        return True

    # —— 服务 ——
    def _add_service(self, name: str, factory: Factory, scope: Scope) -> None:
        with self._lock:
            if name in self._services:
                raise ServiceError(f"服务重复注册: {name}")
            self._services[name] = {"factory": factory, "scope": scope,
                                    "instance": None, "starting": False}

            def undo():
                entry = self._services.pop(name, None)
                if entry and entry["instance"] is not None:
                    stop = getattr(entry["instance"], "stop", None)
                    if callable(stop):
                        try:
                            stop()
                        except Exception:  # noqa: BLE001
                            log.exception("服务 %s 的 stop() 出错", name)
            scope._undo.append(undo)
        self._flush_pending()

    def _get_service(self, name: str) -> Any:
        with self._lock:
            entry = self._services.get(name)
            if entry is None:
                raise KeyError(f"服务未注册: {name}（已注册: {sorted(self._services)}）")
            if entry["instance"] is None:
                if entry["starting"]:
                    raise ServiceError(f"服务 {name} 重入：factory 内又依赖自身")
                entry["starting"] = True
                try:
                    entry["instance"] = entry["factory"](self)
                finally:
                    entry["starting"] = False
            inst = entry["instance"]
        start = getattr(inst, "start", None)
        if callable(start) and not getattr(inst, "_started", False):
            # start 在锁外：允许 start() 里再 use 其他服务
            start()
            inst._started = True
        return inst

    # —— 路由 ——
    def _add_route(self, method: str, path: str, fn: Callable, scope: Scope) -> None:
        key = (method, path)
        with self._lock:
            if key in self._routes:
                raise RouteConflictError(f"路由冲突: {method} {path}")
            self._routes[key] = (fn, scope)

            def undo():
                self._routes.pop(key, None)
            scope._undo.append(undo)

    def resolve(self, method: str, path: str) -> Callable | None:
        fn = self._routes.get((method, path))
        return fn[0] if fn else None

    # —— 插件 ——
    def _apply_plugin(self, module: Any, *, name: str, inject: tuple[str, ...]) -> Scope:
        name = name or getattr(module, "__name__", None) or "plugin-" + secrets.token_hex(3)
        with self._lock:
            missing = [d for d in inject if d not in self._services]
            scope = Scope(self, name)
            if missing:
                self._pending.append((module, name, inject, scope))
                return scope
        register = getattr(module, "register", None) or module
        register(scope)
        return scope

    def _flush_pending(self) -> None:
        while True:
            with self._lock:
                ready = [(m, n, i, s) for m, n, i, s in self._pending
                         if all(d in self._services for d in i)]
                for item in ready:
                    self._pending.remove(item)
            if not ready:
                return
            for module, name, _inject, scope in ready:
                try:
                    register = getattr(module, "register", None) or module
                    register(scope)
                except Exception:  # noqa: BLE001
                    log.exception("模块 %s 激活失败（依赖已就位）", name)
                    scope.dispose()

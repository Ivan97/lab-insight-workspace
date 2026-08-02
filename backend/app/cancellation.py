import threading
from collections.abc import Callable
from contextlib import suppress


class AnalysisCancelled(RuntimeError):
    """Raised when a user stops an in-flight analysis."""


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            with suppress(Exception):
                callback()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise AnalysisCancelled("Analysis was stopped by the user")

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self.cancelled:
                callback()
                return lambda: None
            self._callbacks.append(callback)

        def remove() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return remove


_tokens: dict[str, tuple[str, CancellationToken]] = {}
_tokens_lock = threading.Lock()


def register_cancellation(
    conversation_id: str, message_id: str
) -> CancellationToken:
    token = CancellationToken()
    with _tokens_lock:
        _tokens[message_id] = (conversation_id, token)
    return token


def cancel_active(conversation_id: str, message_id: str) -> bool:
    with _tokens_lock:
        active = _tokens.get(message_id)
    if not active or active[0] != conversation_id:
        return False
    token = active[1]
    token.cancel()
    return True


def unregister_cancellation(message_id: str, token: CancellationToken) -> None:
    with _tokens_lock:
        active = _tokens.get(message_id)
        if active and active[1] is token:
            _tokens.pop(message_id, None)

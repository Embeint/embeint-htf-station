"""Keep received MQTT messages until the application has handled them."""
from __future__ import annotations

import asyncio

from aiomqtt import Message


class MessageInbox:
    """An in-memory inbox shared by successive MQTT connections.

    aiomqtt's iterator can consume its queue before its caller resumes. Retain
    messages at enqueue time, so cancelling that iterator or disconnecting does
    not discard commands already acknowledged by the MQTT transport.
    """

    def __init__(self) -> None:
        self._pending: dict[int, Message] = {}
        pending = self._pending

        class IncomingQueue(asyncio.Queue[Message]):
            def __init__(self, maxsize: int = 0) -> None:
                super().__init__(maxsize)
                for message in pending.values():
                    super().put_nowait(message)

            def put_nowait(self, message: Message) -> None:
                super().put_nowait(message)
                pending[id(message)] = message

        # aiomqtt exposes queue_type specifically for incoming queue ownership.
        self.queue_type = IncomingQueue

    def handled(self, message: Message) -> None:
        self._pending.pop(id(message), None)

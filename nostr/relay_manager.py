import json
import time
import threading
from dataclasses import dataclass
from threading import Lock

from .event import Event
from .filter import Filters
from .message_pool import MessagePool
from .message_type import ClientMessageType
from .relay import Relay, RelayPolicy, RelayProxyConnectionConfig
from .request import Request
from typing import Optional

class RelayException(Exception):
    pass



@dataclass
class RelayManager:
    connection_monitor_interval_secs: int = 5

    on_relay_open: Optional[callable] = None
    on_relay_close: Optional[callable] = None

    stop_threads: bool = False

    def __post_init__(self):
        self.relays: dict[str, Relay] = {}
        self.message_pool: MessagePool = MessagePool()
        self.lock: Lock = Lock()
        self.stop_threads = False

        threading.Thread(
            target=self._relay_connection_monitor,
            name="relay-connection-monitor",
            daemon=True
        ).start()


    def add_relay(
            self, 
            url: str, 
            policy: RelayPolicy = RelayPolicy(),
            ssl_options: dict = None,
            proxy_config: RelayProxyConnectionConfig = None):

        relay = Relay(url, self.message_pool, policy, ssl_options, proxy_config)
        relay.on_open_callback = self.on_relay_open
        relay.on_close_callback = self.on_relay_close

        with self.lock:
            self.relays[url] = relay
            relay.connect()

    def remove_relay(self, url: str):
        with self.lock:
            if url in self.relays:
                relay = self.relays.pop(url)
                relay.close()

    def add_subscription_on_relay(self, url: str, id: str, filters: Filters):
        with self.lock:
            if url in self.relays:
                relay = self.relays[url]
                if not relay.policy.should_read:
                    raise RelayException(f"Could not send request: {url} is not configured to read from")
                relay.add_subscription(id, filters)
                request = Request(id, filters)
                relay.publish(request.to_message())
            else:
                raise RelayException(f"Invalid relay url: no connection to {url}") 

    def add_subscription_on_all_relays(self, id: str, filters: Filters):
        with self.lock:
            for relay in self.relays.values():
                if relay.policy.should_read:
                    relay.add_subscription(id, filters)
                    request = Request(id, filters)
                    relay.publish(request.to_message())

    def close_subscription_on_relay(self, url: str, id: str):
        with self.lock:
            if url in self.relays:
                relay = self.relays[url]
                relay.close_subscription(id)
                relay.publish(json.dumps(["CLOSE", id]))
            else:
                raise RelayException(f"Invalid relay url: no connection to {url}")

    def close_subscription_on_all_relays(self, id: str):
        with self.lock:
            for relay in self.relays.values():
                relay.close_subscription(id)
                relay.publish(json.dumps(["CLOSE", id]))

    def close_all_relay_connections(self):
        with self.lock:
            for url in self.relays:
                relay = self.relays[url]
                relay.close()

    def publish_event(self, event: Event):
        """ Verifies that the Event is publishable before submitting it to relays """
        if event.signature is None:
            raise RelayException(f"Could not publish {event.id}: must be signed")

        if not event.verify():
            raise RelayException(f"Could not publish {event.id}: failed to verify signature {event.signature}")

        with self.lock:
            for relay in self.relays.values():
                if relay.policy.should_write:
                    relay.publish(event.to_message())

    def _relay_connection_monitor(self):
        while not self.stop_threads:
            with self.lock:
                if len(self.relays) > 0:
                    for relay in self.relays.values():
                        if not relay.is_connected():
                            relay.connect(True)

            time.sleep(self.connection_monitor_interval_secs)
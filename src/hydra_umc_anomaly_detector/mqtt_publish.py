# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - src/hydra_umc_anomaly_detector/mqtt_publish.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The smallest MQTT 3.1.1 publisher that does the job: connect, publish one message at QoS 0, disconnect.

The follower raises an alarm only when something changes (a series becomes anomalous, or healthy again) and once per anomalous window, so
a connection per message is cheap and means there is no connection to keep alive, lose or reconnect. No new dependency: the packets are
a few bytes each. TLS is optional, and a broker that is not on this machine is refused without it unless the configuration says on
purpose that plain text is fine (`allowPlaintext`).
"""
from __future__ import annotations

import socket
import ssl
import struct
from dataclasses import dataclass


class MqttError(RuntimeError):
    """The broker could not be reached or refused the message."""


def encode_length(length: int) -> bytes:
    if length < 0 or length > 268_435_455:
        raise MqttError("an MQTT packet cannot be this long")
    out = bytearray()
    while True:
        digit, length = length % 128, length // 128
        out.append(digit | (0x80 if length else 0))
        if not length:
            return bytes(out)


def encode_string(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) > 65535:
        raise MqttError("an MQTT string cannot be this long")
    return struct.pack(">H", len(data)) + data


def connect_packet(client_id: str, username: str | None, password: str | None, keepalive: int = 30) -> bytes:
    flags = 0x02  # clean session
    payload = encode_string(client_id)
    if username is not None:
        flags |= 0x80
        payload += encode_string(username)
        if password is not None:
            flags |= 0x40
            payload += encode_string(password)
    variable = encode_string("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive)
    body = variable + payload
    return b"\x10" + encode_length(len(body)) + body


def publish_packet(topic: str, payload: bytes, retain: bool = False) -> bytes:
    if not topic or "#" in topic or "+" in topic or "\x00" in topic:
        raise MqttError("a topic to publish to has no wildcards and is not empty")
    body = encode_string(topic) + payload
    return bytes([0x30 | (0x01 if retain else 0x00)]) + encode_length(len(body)) + body


DISCONNECT = b"\xe0\x00"


@dataclass(frozen=True)
class MqttTarget:
    host: str
    port: int = 1883
    client_id: str = "hydra-umc-anomaly-detector"
    username: str | None = None
    password: str | None = None
    tls: bool = False
    ca_file: str | None = None
    timeout_s: float = 5.0


class MqttPublisher:
    def __init__(self, target: MqttTarget) -> None:
        self.target = target

    def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        packet = publish_packet(topic, payload, retain)
        target = self.target
        try:
            raw = socket.create_connection((target.host, target.port), timeout=target.timeout_s)
        except OSError as error:
            raise MqttError(f"cannot reach the broker at {target.host}:{target.port}: {error}") from error
        connection = raw
        try:
            if target.tls:
                context = ssl.create_default_context(cafile=target.ca_file)
                connection = context.wrap_socket(raw, server_hostname=target.host)
            connection.settimeout(target.timeout_s)
            connection.sendall(connect_packet(target.client_id, target.username, target.password))
            reply = _read_exact(connection, 4)
            if reply[0] != 0x20 or reply[1] != 0x02:
                raise MqttError("the broker did not answer CONNECT with CONNACK")
            if reply[3] != 0:
                raise MqttError(f"the broker refused the connection (return code {reply[3]})")
            connection.sendall(packet + DISCONNECT)
        except (OSError, ssl.SSLError) as error:
            raise MqttError(f"MQTT exchange with {target.host}:{target.port} failed: {error}") from error
        finally:
            try:
                connection.close()
            except OSError:
                pass


def _read_exact(connection: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise MqttError("the broker closed the connection")
        data += chunk
    return data

# =============================================================================
# HYDRA-UMC-ANOMALY-DETECTOR - tests/test_mqtt_and_metrics_edges.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The edges of the MQTT publisher and of the metrics text: oversize packets, a broker that is not there, answers that are not CONNACK, a
broker that hangs up half way, and names that must never carry free text into a label."""
from __future__ import annotations

import socket
import threading

import pytest

from hydra_umc_anomaly_detector.mqtt_publish import (
    MqttError,
    MqttPublisher,
    MqttTarget,
    connect_packet,
    encode_length,
    encode_string,
    publish_packet,
)
from hydra_umc_anomaly_detector.service_metrics import ServiceMetrics, check_name, installed_version


def _one_shot_broker(reply: bytes, *, read_first: bool = True) -> tuple[int, threading.Thread]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection, listener:
            connection.settimeout(2)
            try:
                if read_first:
                    connection.recv(256)
                if reply:
                    connection.sendall(reply)
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener.getsockname()[1], thread


def test_a_length_outside_what_mqtt_can_carry_is_refused():
    with pytest.raises(MqttError):
        encode_length(-1)
    with pytest.raises(MqttError):
        encode_length(268_435_456)
    assert encode_length(268_435_455) == b"\xff\xff\xff\x7f"


def test_a_string_over_65535_bytes_is_refused_and_counts_bytes_not_characters():
    with pytest.raises(MqttError):
        encode_string("x" * 65536)
    assert encode_string("e" * 65535)[:2] == b"\xff\xff"
    assert encode_string("é")[:2] == b"\x00\x02"  # one character, two bytes


def test_connect_flags_follow_the_credentials_that_are_given():
    anonymous = connect_packet("cid", None, None)
    user_only = connect_packet("cid", "user", None)
    with_password = connect_packet("cid", "user", "secret")
    flag = lambda packet: packet[packet.index(b"MQTT") + 5]  # noqa: E731
    assert flag(anonymous) == 0x02 and flag(user_only) == 0x82 and flag(with_password) == 0xC2
    assert b"secret" not in user_only and b"secret" in with_password
    # a password without a user name is dropped: the standard does not allow it with this flag layout
    assert flag(connect_packet("cid", None, "secret")) == 0x02


@pytest.mark.parametrize("topic", ["", "a/#", "a/+/b", "a\x00b"])
def test_a_topic_that_is_empty_or_has_a_wildcard_or_a_nul_is_never_built(topic):
    with pytest.raises(MqttError):
        publish_packet(topic, b"x")


def test_a_broker_that_is_not_there_is_an_mqtt_error_and_not_an_os_error():
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
    publisher = MqttPublisher(MqttTarget(host="127.0.0.1", port=port, timeout_s=0.5))
    with pytest.raises(MqttError, match="cannot reach the broker"):
        publisher.publish("a/b", b"x")


def test_an_answer_that_is_not_a_connack_is_refused():
    port, thread = _one_shot_broker(b"\x30\x02\x00\x00")
    with pytest.raises(MqttError, match="did not answer CONNECT"):
        MqttPublisher(MqttTarget(host="127.0.0.1", port=port, timeout_s=1.0)).publish("a/b", b"x")
    thread.join(timeout=3)


@pytest.mark.parametrize("code", [1, 4, 5])
def test_every_refusal_code_of_the_broker_is_reported_with_its_number(code):
    port, thread = _one_shot_broker(bytes([0x20, 0x02, 0x00, code]))
    with pytest.raises(MqttError, match=f"return code {code}"):
        MqttPublisher(MqttTarget(host="127.0.0.1", port=port, timeout_s=1.0)).publish("a/b", b"x")
    thread.join(timeout=3)


def test_a_broker_that_hangs_up_before_it_answers_is_an_mqtt_error():
    port, thread = _one_shot_broker(b"")
    with pytest.raises(MqttError, match="closed the connection"):
        MqttPublisher(MqttTarget(host="127.0.0.1", port=port, timeout_s=1.0)).publish("a/b", b"x")
    thread.join(timeout=3)


def test_a_broker_that_answers_half_a_connack_and_stops_is_an_mqtt_error():
    port, thread = _one_shot_broker(b"\x20\x02")
    with pytest.raises(MqttError):
        MqttPublisher(MqttTarget(host="127.0.0.1", port=port, timeout_s=0.5)).publish("a/b", b"x")
    thread.join(timeout=3)


def test_tls_against_a_broker_that_speaks_plain_text_fails_as_an_mqtt_error():
    port, thread = _one_shot_broker(b"not tls at all", read_first=True)
    with pytest.raises(MqttError):
        MqttPublisher(MqttTarget(host="127.0.0.1", port=port, tls=True, timeout_s=1.0)).publish("a/b", b"x")
    thread.join(timeout=3)


def test_check_names_can_never_carry_free_text_into_a_label():
    assert check_name("Data Lake/OK") == "data_lake_ok"
    assert check_name('"}\n injected{') == "injected"
    assert check_name("___") == "check"
    assert check_name("") == "check"


def test_the_installed_version_of_something_unknown_is_unknown():
    assert installed_version("a-distribution-that-does-not-exist-hyt2") == "unknown"
    assert installed_version("pytest") not in ("", "unknown")


def test_the_metrics_text_escapes_the_labels_and_says_unhealthy_when_a_check_fails():
    text = ServiceMetrics('svc"x\\y\nz', "1.0").render({"datalake": True, "broker": False})
    assert 'svc\\"x\\\\y z' in text
    assert 'hydra_umc_service_health{service="svc\\"x\\\\y z"} 0' in text
    assert text.count("check=") == 2 and 'check="broker"} 0' in text and 'check="datalake"} 1' in text
    assert ServiceMetrics("svc", "1.0").render({}).count("hydra_umc_service_health_check") == 0
    assert 'hydra_umc_service_health{service="svc"} 1' in ServiceMetrics("svc", "1.0").render({})

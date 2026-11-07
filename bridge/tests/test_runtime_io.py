import asyncio
import shutil
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contrainer.decode.fitness import BikeSample
from contrainer.decode.zwift_ride import KeypadStatus
from contrainer.mapping.profiles import ProfileStore
from contrainer.output.chiaki import PACKET, ChiakiSender, ControllerState, pack, unpack
from contrainer.runtime import Runtime
from contrainer.signals import SignalHub
from contrainer.web.app import create_app

SHIPPED = Path(__file__).resolve().parent.parent / "profiles"


def test_packet_layout():
    state = ControllerState(buttons=0b101, l2=10, r2=255, left_x=-32767, left_y=32767, right_x=1, right_y=-1)
    data = pack(state, 7)
    assert len(data) == PACKET.size == 23
    assert data[:5] == b"CTRN\x01"
    assert data[5:9] == (7).to_bytes(4, "little")
    assert unpack(data) == (7, state)


def test_sender_delivers_over_udp_with_increasing_seq():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(1)
    sender = ChiakiSender(port=rx.getsockname()[1])
    sender.send(ControllerState(r2=1))
    sender.send(ControllerState(r2=2))
    seqs = [unpack(rx.recv(64))[0] for _ in range(2)]
    assert seqs == [1, 2]
    sender.close()
    rx.close()


def test_sender_without_listener_does_not_raise():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    sender = ChiakiSender(port=port)
    for _ in range(5):
        sender.send(ControllerState())
    sender.close()


def test_hub_power_relative_to_ftp():
    hub = SignalHub(ftp_w=250)
    hub.update_bike(BikeSample(power_w=125))
    assert hub.snapshot().power_ftp == pytest.approx(0.5)
    hub.update_bike(BikeSample(power_w=300))
    assert hub.snapshot().power_ftp == pytest.approx(1.2)


def test_rider_settings_roundtrip_and_validation(tmp_path):
    from contrainer.settings import RiderSettings, SettingsError, load_rider, save_rider

    path = tmp_path / "rider.yaml"
    assert load_rider(path).ftp_w == 200  # default when missing
    save_rider(path, RiderSettings(ftp_w=265))
    assert load_rider(path).ftp_w == 265
    path.write_text("ftp_w: 5\n")
    with pytest.raises(SettingsError):
        load_rider(path)


def test_hub_zeroes_stale_bike_data_and_disconnected_ride():
    now = [0.0]
    hub = SignalHub(clock=lambda: now[0], bike_stale_s=3)
    hub.update_bike(BikeSample(power_w=250, cadence_rpm=90))
    hub.update_ride(KeypadStatus(pressed=frozenset({"A"})))
    assert hub.snapshot().power_w == 250
    now[0] = 3.5
    snap = hub.snapshot()
    assert snap.power_w == 0 and snap.cadence_rpm == 0
    hub.set_connected("ride", False)
    assert hub.snapshot().buttons == frozenset()


@pytest.fixture
def runtime(tmp_path) -> Runtime:
    for p in SHIPPED.glob("*.yaml"):
        shutil.copy(p, tmp_path / p.name)
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rt = Runtime(
        store=ProfileStore(tmp_path),
        hub=SignalHub(),
        sender=ChiakiSender(port=rx.getsockname()[1]),
        active_name="gt7",
        sim=True,
        rider_path=tmp_path / "rider.yaml",
    )
    rt._rx = rx  # keep the receiver alive for the test
    yield rt
    rt.sender.close()
    rx.close()


def test_engine_loop_sends_mapped_state_and_idle_on_stop(runtime):
    runtime._rx.settimeout(1)

    async def scenario():
        runtime.hub.update_bike(BikeSample(power_w=runtime.rider.ftp_w))  # 100% FTP
        task = asyncio.create_task(runtime.run_engine())
        await asyncio.sleep(0.6)  # smoothing (200 ms) settles
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    packets = []
    runtime._rx.settimeout(0.2)
    try:
        while True:
            packets.append(unpack(runtime._rx.recv(64))[1])
    except TimeoutError:
        pass
    assert max(p.r2 for p in packets) >= 250
    assert packets[-1] == ControllerState()  # idle after shutdown


def test_reload_keeps_last_valid_profile(runtime):
    (runtime.store.dir / "gt7.yaml").write_text("name: broken\nbuttons: {cross: NOPE}\n")
    runtime.reload_active()
    assert "NOPE" in runtime.error
    assert runtime.profile.name == "GT7"


def test_web_api(runtime):
    client = TestClient(create_app(runtime))
    assert "A" in client.get("/api/meta").json()["ride_buttons"]
    assert client.get("/api/profiles").json() == {"profiles": ["gt7", "menu", "walking"], "active": "gt7"}
    prof = client.get("/api/profiles/gt7").json()
    assert prof["doc"]["extends"] == "menu" and "cross" in prof["resolved"]["buttons"]

    bad = client.put("/api/profiles/gt7", json={"yaml": "name: x\nbuttons: {cross: NOPE}\n"})
    assert bad.status_code == 400 and "NOPE" in bad.json()["detail"]
    assert client.post("/api/profiles/gt7/validate", json={"yaml": "name: x\n"}).json() == {"ok": True}

    ok = client.put("/api/profiles/mine", json={"yaml": "name: Mine\nextends: menu\n"})
    assert ok.status_code == 200
    assert client.post("/api/profiles/mine/activate").json()["active"] == "mine"
    assert runtime.active_name == "mine"

    doc = client.get("/api/profiles/mine").json()["doc"]
    doc["buttons"] = {"cross": "B"}
    assert client.put("/api/profiles/mine/doc", json={"doc": doc}).status_code == 200
    assert runtime.profile.buttons["cross"].button == "B"  # active profile reloaded

    assert client.post("/api/sim", json={"buttons": ["A", "bogus"]}).status_code == 200
    assert runtime.hub.snapshot().buttons == frozenset({"A"})

    assert client.get("/api/rider").json() == {"ftp_w": 200}
    assert client.put("/api/rider", json={"ftp_w": 280}).json() == {"ftp_w": 280}
    assert runtime.hub.ftp_w == 280 and "280" in (runtime.store.dir / "rider.yaml").read_text()
    assert client.put("/api/rider", json={"ftp_w": 9999}).status_code == 422

    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["profile"] == "mine" and msg["sim"] is True and msg["rider"] == {"ftp_w": 280}

import asyncio
import shutil
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gtrainer.decode.fitness import BikeSample
from gtrainer.decode.zwift_ride import KeypadStatus
from gtrainer.mapping.profiles import ProfileStore
from gtrainer.output.chiaki import PACKET, ChiakiSender, ControllerState, pack, unpack
from gtrainer.runtime import Runtime
from gtrainer.signals import SignalHub
from gtrainer.web.app import LOCAL_HOSTS, create_app

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
    from gtrainer.settings import RiderSettings, SettingsError, load_rider, save_rider

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


def local_client(runtime) -> TestClient:
    # TestClient always uses ws://testserver for WebSockets, so allow that name too
    return TestClient(create_app(runtime, allowed_hosts=LOCAL_HOSTS | {"testserver"}), base_url="http://127.0.0.1:8765")


def test_web_api(runtime):
    client = local_client(runtime)
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

    assert client.get("/api/rider").json() == {"ftp_w": 200, "first_gear_ftp": 0.5, "target_cadence_rpm": 90}
    # partial updates keep the other fields
    assert client.put("/api/rider", json={"ftp_w": 280}).json()["first_gear_ftp"] == 0.5
    assert client.put("/api/rider", json={"first_gear_ftp": 0.4}).json() == {
        "ftp_w": 280, "first_gear_ftp": 0.4, "target_cadence_rpm": 90,
    }
    assert runtime.hub.ftp_w == 280 and runtime.hub.first_gear_ftp == 0.4
    assert "280" in (runtime.store.dir / "rider.yaml").read_text()
    assert client.put("/api/rider", json={"ftp_w": 9999}).status_code == 422
    assert client.put("/api/rider", json={"bogus": 1}).status_code == 422
    assert client.post("/api/sim/gear", json={"gear": 3}).json() == {"ok": True, "gear": 3}
    assert runtime.hub.snapshot().gear == 3

    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["profile"] == "mine" and msg["sim"] is True and msg["rider"]["ftp_w"] == 280


def test_live_websocket_over_a_real_uvicorn_server(runtime):
    """The in-process TestClient doesn't need a WebSocket library; a real server does."""
    import json

    import uvicorn
    import websockets

    async def scenario():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server = uvicorn.Server(uvicorn.Config(create_app(runtime), host="127.0.0.1", port=port, log_level="error"))
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(50):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert msg["profile"] == "gt7" and "signals" in msg and "output" in msg
        finally:
            server.should_exit = True
            await task

    asyncio.run(scenario())


def test_web_rejects_foreign_origins_and_hosts(runtime):
    from starlette.websockets import WebSocketDisconnect

    client = local_client(runtime)
    same = {"Origin": "http://127.0.0.1:8765"}
    assert client.post("/api/profiles/menu/activate", headers=same).status_code == 200
    # another web page (CSRF) or a DNS-rebound hostname must not reach the API
    evil = client.post("/api/profiles/gt7/activate", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403 and runtime.active_name == "menu"
    assert client.get("/api/meta", headers={"Host": "evil.example:8765"}).status_code == 403
    assert client.get("/api/meta", headers={"Origin": "null"}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_json()
    with client.websocket_connect("/ws", headers=same) as ws:
        assert ws.receive_json()["profile"] == "menu"


def test_create_profile_never_overwrites(runtime):
    client = local_client(runtime)
    before = (runtime.store.dir / "walking.yaml").read_text()
    r = client.post("/api/profiles/walking", json={"yaml": "name: walking\nextends: gt7\n"})
    assert r.status_code == 409
    assert (runtime.store.dir / "walking.yaml").read_text() == before
    assert client.post("/api/profiles/fresh", json={"yaml": "name: Fresh\nextends: menu\n"}).status_code == 200
    assert "fresh" in runtime.store.names()


def test_reload_of_unchanged_profile_keeps_engine_state(runtime):
    runtime.engine._toggles["base:x"] = True
    runtime.reload_active()  # e.g. an unrelated file in the profiles dir changed
    assert runtime.engine._toggles == {"base:x": True}
    text = (runtime.store.dir / "gt7.yaml").read_text().replace("smooth_ms: 400", "smooth_ms: 300")
    (runtime.store.dir / "gt7.yaml").write_text(text)
    runtime.reload_active()  # a real change resets profile state
    assert runtime.engine._toggles == {}


def test_supervise_logs_and_restarts_crashed_task(runtime):
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    asyncio.run(runtime.supervise("flaky", flaky, restart_after_s=0))
    assert len(calls) == 2
    assert any("boom" in m for m in runtime.recent_task_errors())
    assert "boom" in runtime.status_json()["error"]

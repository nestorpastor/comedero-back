"""
Prueba que el relay recupere la vinculación que recuerda la Pi (por
ejemplo, después de un deploy en Render, que arranca con la memoria vacía):
1. Pi registrada sin "emparejado": los comandos dan 403.
2. Pi registrada con "emparejado": true: los comandos funcionan.
3. Alguien con el secreto equivocado no puede registrarse ni vincular.

Uso: python3 test_vinculo.py  (necesita `pip install httpx websockets`)
"""
import asyncio
import json
import os
import subprocess
import sys

import httpx
import websockets

PUERTO = 8094
BASE = f"http://127.0.0.1:{PUERTO}"
WS_BASE = f"ws://127.0.0.1:{PUERTO}"
SECRETO = "secreto-de-prueba"


async def contestar(ws):
    async for crudo in ws:
        msg = json.loads(crudo)
        if msg.get("type") == "command":
            await ws.send(json.dumps({"type": "resultado", "request_id": msg["request_id"],
                                      "ok": True, "duracion": 3}))


async def pi_conectada(client, device_id, emparejado):
    r = await client.post(f"{BASE}/devices/register", json={
        "device_id": device_id, "device_secret": SECRETO, "emparejado": emparejado})
    assert r.status_code == 200, r.text
    ws = await websockets.connect(f"{WS_BASE}/devices/{device_id}/ws?device_secret={SECRETO}")
    return ws, asyncio.create_task(contestar(ws))


async def main():
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(50):
            try:
                await client.get(BASE)
                break
            except httpx.TransportError:
                await asyncio.sleep(0.2)

        comando = {"accion": "alimentar_ahora"}

        ws, tarea = await pi_conectada(client, "pi-sin-vincular", False)
        r = await client.post(f"{BASE}/devices/pi-sin-vincular/command", json=comando)
        assert r.status_code == 403, r.text
        print("✔ sin vincular: 403")
        tarea.cancel(); await ws.close()

        ws, tarea = await pi_conectada(client, "pi-vinculada", True)
        r = await client.post(f"{BASE}/devices/pi-vinculada/command", json=comando)
        assert r.status_code == 200 and r.json()["ok"], r.text
        print("✔ la Pi recordaba la vinculación: el comando funciona")
        tarea.cancel(); await ws.close()

        r = await client.post(f"{BASE}/devices/register", json={
            "device_id": "pi-sin-vincular", "device_secret": "otro", "emparejado": True})
        assert r.status_code == 403, r.text
        estado = (await client.get(f"{BASE}/devices/pi-sin-vincular/status")).json()
        assert not estado["emparejado"], estado
        print("✔ con otro secreto no se puede vincular")
    print("\n✅ VINCULO OK")


if __name__ == "__main__":
    relay = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PUERTO)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env={k: v for k, v in os.environ.items() if k != "NTFY_TOPIC"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        asyncio.run(main())
    finally:
        relay.terminate()
        relay.wait()

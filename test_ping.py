"""
Prueba el ping de punta a punta (/devices/{id}/ping):
1. Con la "Pi" contestando: responde ok.
2. Con una "Pi zombie" (WebSocket abierto pero que no contesta, como
   cuando se corta la luz y la conexión queda colgada): responde 504 y el
   relay pasa a considerarla desconectada.
3. Sin Pi conectada: 503.

Uso: python3 test_ping.py  (necesita `pip install httpx websockets`)
"""
import asyncio
import json
import os
import subprocess
import sys

import httpx
import websockets

PUERTO = 8093
BASE = f"http://127.0.0.1:{PUERTO}"
WS_BASE = f"ws://127.0.0.1:{PUERTO}"
DEVICE_ID = "pi-test-ping"
DEVICE_SECRET = "secreto-de-prueba"


async def conectar_pi():
    async with httpx.AsyncClient() as client:
        await client.post(f"{BASE}/devices/register",
                          json={"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET})
    return await websockets.connect(
        f"{WS_BASE}/devices/{DEVICE_ID}/ws?device_secret={DEVICE_SECRET}")


async def contestar(ws):
    async for crudo in ws:
        msg = json.loads(crudo)
        if msg.get("type") == "command":
            await ws.send(json.dumps({"type": "resultado", "request_id": msg["request_id"], "ok": True}))


async def main():
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(50):
            try:
                await client.get(BASE)
                break
            except httpx.TransportError:
                await asyncio.sleep(0.2)

        r = await client.get(f"{BASE}/devices/{DEVICE_ID}/ping")
        assert r.status_code == 404, r.text
        print("✔ Pi desconocida: 404")

        ws = await conectar_pi()
        tarea = asyncio.create_task(contestar(ws))
        r = await client.get(f"{BASE}/devices/{DEVICE_ID}/ping")
        assert r.status_code == 200 and r.json()["ok"], r.text
        print("✔ Pi contestando:", r.json())
        tarea.cancel()

        # Zombie: el WebSocket sigue abierto pero nadie contesta.
        r = await client.get(f"{BASE}/devices/{DEVICE_ID}/ping")
        assert r.status_code == 504, r.text
        estado = (await client.get(f"{BASE}/devices/{DEVICE_ID}/status")).json()
        assert not estado["conectado"] and estado["desconectado_desde"], estado
        print("✔ Pi zombie: 504 y el relay la da por desconectada")

        r = await client.get(f"{BASE}/devices/{DEVICE_ID}/ping")
        assert r.status_code == 503, r.text
        print("✔ Sin conexión: 503")
        await ws.close()
    print("\n✅ PING OK")


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

"""
Prueba los avisos de ntfy (offline/online) sin mandar nada a ntfy.sh de
verdad:
1. Levanta un "ntfy" falso local que anota lo que recibe.
2. Levanta el relay apuntando a ese ntfy, con tiempos cortos
   (offline a los 3 s en vez de 2 min).
3. Una "Pi" se conecta, se corta un instante (no debe avisar nada), se
   corta de verdad (debe avisar "Comedero offline" una sola vez) y
   vuelve (debe avisar "Comedero online").

Uso: python3 test_avisos.py  (necesita `pip install httpx websockets`)
"""
import asyncio
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import websockets

PUERTO_RELAY = 8091
PUERTO_NTFY = 8092
BASE = f"http://127.0.0.1:{PUERTO_RELAY}"
WS_BASE = f"ws://127.0.0.1:{PUERTO_RELAY}"
DEVICE_ID = "pi-test-avisos"
DEVICE_SECRET = "secreto-de-prueba"

RECIBIDOS = []


class NtfyFalso(BaseHTTPRequestHandler):
    def do_POST(self):
        cuerpo = self.rfile.read(int(self.headers["Content-Length"])).decode()
        RECIBIDOS.append((self.path, self.headers["Title"], cuerpo))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def titulos():
    return [t for _, t, _ in RECIBIDOS]


async def conectar_pi():
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BASE}/devices/register",
                              json={"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET})
        assert r.status_code == 200, r.text
    uri = f"{WS_BASE}/devices/{DEVICE_ID}/ws?device_secret={DEVICE_SECRET}"
    return await websockets.connect(uri)


async def esperar_relay():
    async with httpx.AsyncClient() as client:
        for _ in range(50):
            try:
                await client.get(BASE)
                return
            except httpx.TransportError:
                await asyncio.sleep(0.2)
    raise RuntimeError("el relay no arrancó")


async def main():
    await esperar_relay()

    ws = await conectar_pi()
    print("[Pi] conectada")

    # Microcorte: se cae y vuelve antes del umbral -> nada.
    await ws.close()
    await asyncio.sleep(1)
    ws = await conectar_pi()
    await asyncio.sleep(4)
    assert titulos() == [], titulos()
    print("✔ microcorte sin avisos")

    # Corte largo -> "Comedero offline", una sola vez.
    await ws.close()
    await asyncio.sleep(6)
    assert titulos() == ["Comedero offline"], titulos()
    assert RECIBIDOS[0][0] == "/topic-de-prueba", RECIBIDOS[0]
    print("✔ aviso offline:", RECIBIDOS[0][2])

    # Vuelve -> "Comedero online".
    ws = await conectar_pi()
    await asyncio.sleep(1)
    assert titulos() == ["Comedero offline", "Comedero online"], titulos()
    print("✔ aviso online:", RECIBIDOS[1][2])

    # Sigue conectada -> no se repite nada.
    await asyncio.sleep(3)
    assert len(RECIBIDOS) == 2, titulos()
    await ws.close()
    print("\n✅ AVISOS OK")


if __name__ == "__main__":
    ntfy = HTTPServer(("127.0.0.1", PUERTO_NTFY), NtfyFalso)
    threading.Thread(target=ntfy.serve_forever, daemon=True).start()

    entorno = dict(
        os.environ,
        NTFY_TOPIC="topic-de-prueba",
        NTFY_URL=f"http://127.0.0.1:{PUERTO_NTFY}",
        OFFLINE_TRAS_SEGUNDOS="3",
        CHEQUEO_CADA_SEGUNDOS="0.5",
    )
    relay = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PUERTO_RELAY)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=entorno,
    )
    try:
        asyncio.run(main())
    finally:
        relay.terminate()
        relay.wait()
        ntfy.shutdown()

"""
Simula el flujo completo contra el backend local:
1. Una "Pi" se registra y abre su WebSocket.
2. Pide un código de emparejamiento.
3. Una "app" escanea (simulado) y llama /pair con ese código.
4. La "app" manda un comando /command.
5. La "Pi" (del lado del WS) responde, y verificamos que la app recibe la
   respuesta correcta.
"""
import asyncio
import json
import httpx
import websockets

BASE = "http://127.0.0.1:8090"
WS_BASE = "ws://127.0.0.1:8090"
DEVICE_ID = "pi-test-001"
DEVICE_SECRET = "secreto-de-prueba"


async def lado_pi():
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BASE}/devices/register",
                               json={"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET})
        assert r.status_code == 200, r.text
        print("[Pi] registrada:", r.json())

        r = await client.post(
            f"{BASE}/devices/{DEVICE_ID}/pairing-code",
            params={"device_secret": DEVICE_SECRET},
        )
        assert r.status_code == 200, r.text
        codigo = r.json()["code"]
        print("[Pi] código de emparejamiento (esto va en el QR):", codigo)

    uri = f"{WS_BASE}/devices/{DEVICE_ID}/ws?device_secret={DEVICE_SECRET}"
    async with websockets.connect(uri) as ws:
        print("[Pi] WebSocket conectado, esperando mensajes...")
        while True:
            crudo = await ws.recv()
            msg = json.loads(crudo)
            print("[Pi] recibido:", msg)
            if msg.get("type") == "emparejado":
                print("[Pi] --> la app se emparejó con éxito")
            elif msg.get("type") == "command":
                # Acá en la Pi real llamaríamos a la función de alimentar.
                await ws.send(json.dumps({
                    "type": "resultado",
                    "request_id": msg["request_id"],
                    "ok": True,
                    "duracion": msg.get("duracion", 3),
                }))
                print("[Pi] comando ejecutado, respuesta enviada")
                return codigo  # cortamos la simulación acá


async def lado_app(codigo_holder):
    await asyncio.sleep(1)  # darle tiempo a que la Pi se registre y conecte
    async with httpx.AsyncClient() as client:
        # En la vida real esto sale de escanear el QR.
        codigo = codigo_holder["codigo"]
        r = await client.post(f"{BASE}/pair", json={"device_id": DEVICE_ID, "code": codigo})
        print("[App] resultado de /pair:", r.status_code, r.json())
        assert r.status_code == 200

        await asyncio.sleep(0.5)
        r = await client.post(f"{BASE}/devices/{DEVICE_ID}/command",
                               json={"accion": "alimentar_ahora", "duracion": 4})
        print("[App] resultado de /command:", r.status_code, r.json())
        assert r.status_code == 200
        assert r.json()["ok"] is True


async def main():
    codigo_holder = {"codigo": None}

    async def lado_pi_wrapper():
        codigo = None
        async with httpx.AsyncClient() as client:
            await client.post(f"{BASE}/devices/register",
                               json={"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET})
            r = await client.post(f"{BASE}/devices/{DEVICE_ID}/pairing-code",
                                   params={"device_secret": DEVICE_SECRET})
            codigo = r.json()["code"]
            codigo_holder["codigo"] = codigo
            print("[Pi] código:", codigo)

        uri = f"{WS_BASE}/devices/{DEVICE_ID}/ws?device_secret={DEVICE_SECRET}"
        async with websockets.connect(uri) as ws:
            print("[Pi] conectado por WS")
            while True:
                crudo = await ws.recv()
                msg = json.loads(crudo)
                print("[Pi] recibido:", msg)
                if msg.get("type") == "command":
                    await ws.send(json.dumps({
                        "type": "resultado",
                        "request_id": msg["request_id"],
                        "ok": True,
                        "duracion": msg.get("duracion", 3),
                    }))
                    print("[Pi] respondido")
                    return

    await asyncio.gather(lado_pi_wrapper(), lado_app(codigo_holder))
    print("\n✅ FLUJO COMPLETO OK")


asyncio.run(main())

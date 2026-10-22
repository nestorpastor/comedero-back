"""
Backend "relay" del comedero -- el intermediario entre la app del celular
y cada Raspberry Pi registrada, para que la app pueda pegarle a "su" Pi
sin importar en qué red esté cada una.

Cómo funciona (versión prototipo, sin cuentas de usuario todavía):

1. La Pi se registra una vez (registrar_dispositivo) con un device_id y un
   secreto que ella misma genera y guarda localmente.
2. La Pi abre una conexión WebSocket permanente hacia acá (/devices/{id}/ws)
   -- la abre ELLA, así que no importa si está detrás de un router/CGNAT/
   wifi ajeno: no hace falta que nadie le "toque la puerta" desde afuera.
3. Cuando el usuario quiere vincular su celular, la Pi pide acá un código
   de emparendamiento de un solo uso (pairing-code) y lo muestra como QR
   en su propia pantalla táctil.
4. La app escanea el QR y llama a /pair con ese código -- eso marca el
   dispositivo como emparejado.
5. De ahí en más, la app manda comandos a /devices/{id}/command, y este
   backend se los reenvía a la Pi por el WebSocket que ya tiene abierto,
   espera la respuesta, y se la devuelve a la app.

Para un prototipo esto guarda todo en memoria (se pierde si el server se
reinicia) -- suficiente para probar el mecanismo. Si funciona bien, el
siguiente paso natural es sumar una base de datos y cuentas de usuario
para que sea multi-tenant de verdad.
"""

import asyncio
import os
import secrets
import string
import time
import urllib.request
from contextlib import asynccontextmanager
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

CODIGO_TTL_SEGUNDOS = 10 * 60
COMANDO_TIMEOUT_SEGUNDOS = 15

# Avisos por ntfy.sh: si la Pi pasa más de OFFLINE_TRAS_SEGUNDOS sin el
# WebSocket conectado, mandamos "Comedero offline"; cuando vuelve,
# "Comedero online". Solo al cambiar de estado, no en cada chequeo. Si
# NTFY_TOPIC no está definida, no se manda nada.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
OFFLINE_TRAS_SEGUNDOS = float(os.environ.get("OFFLINE_TRAS_SEGUNDOS", 2 * 60))
CHEQUEO_CADA_SEGUNDOS = float(os.environ.get("CHEQUEO_CADA_SEGUNDOS", 15))

INICIO_RELAY = time.time()


class Dispositivo:
    def __init__(self, device_id: str, secreto: str):
        self.device_id = device_id
        self.secreto = secreto
        self.emparejado = False
        self.websocket: Optional[WebSocket] = None
        self.codigo_actual: Optional[str] = None
        self.codigo_expira: float = 0.0
        # Momento en que se cortó el WebSocket (None mientras está
        # conectado o si nunca se conectó desde que arrancó el relay).
        self.desconectado_desde: Optional[float] = None
        self.avisado_offline = False


# Estado en memoria -- device_id -> Dispositivo
DISPOSITIVOS: Dict[str, Dispositivo] = {}
# request_id -> Future, para casar la respuesta de la Pi con el pedido HTTP
PEDIDOS_PENDIENTES: Dict[str, "asyncio.Future"] = {}


def _enviar_ntfy_sync(titulo: str, mensaje: str, tags: str):
    req = urllib.request.Request(
        f"{NTFY_URL}/{NTFY_TOPIC}",
        data=mensaje.encode("utf-8"),
        headers={"Title": titulo, "Tags": tags},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10).close()


async def avisar_ntfy(titulo: str, mensaje: str, tags: str = ""):
    """Manda una notificación a ntfy. Nunca levanta excepción: si ntfy
    falla, el relay tiene que seguir funcionando igual."""
    if not NTFY_TOPIC:
        return
    try:
        await asyncio.to_thread(_enviar_ntfy_sync, titulo, mensaje, tags)
    except Exception as e:
        print(f"No se pudo avisar por ntfy ({e})")


# Referencias a las tareas de aviso en curso (asyncio solo guarda una
# referencia débil y sin esto podrían cancelarse a mitad de camino).
_TAREAS_AVISO: set = set()


async def _vigilar_dispositivos():
    while True:
        await asyncio.sleep(CHEQUEO_CADA_SEGUNDOS)
        ahora = time.time()
        for disp in list(DISPOSITIVOS.values()):
            if (
                disp.websocket is None
                and disp.desconectado_desde is not None
                and not disp.avisado_offline
                and ahora - disp.desconectado_desde >= OFFLINE_TRAS_SEGUNDOS
            ):
                disp.avisado_offline = True
                minutos = int((ahora - disp.desconectado_desde) // 60)
                await avisar_ntfy(
                    "Comedero offline",
                    f"{disp.device_id} lleva {minutos} min sin conexión con el relay.",
                    "warning",
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    tarea = asyncio.create_task(_vigilar_dispositivos())
    yield
    tarea.cancel()


app = FastAPI(title="Comedero Relay", lifespan=lifespan)


def _generar_codigo(largo=6) -> str:
    alfabeto = string.ascii_uppercase + string.digits
    # Sin caracteres ambiguos (0/O, 1/I) para que sea más fácil si algún
    # día hace falta escribirlo a mano en vez de escanear el QR.
    alfabeto = alfabeto.translate(str.maketrans("", "", "01OI"))
    return "".join(secrets.choice(alfabeto) for _ in range(largo))


class RegistroIn(BaseModel):
    device_id: str
    device_secret: str
    nombre: Optional[str] = None


@app.post("/devices/register")
def registrar_dispositivo(body: RegistroIn):
    existente = DISPOSITIVOS.get(body.device_id)
    if existente is not None and existente.secreto != body.device_secret:
        raise HTTPException(403, "device_id ya registrado con otro secreto")
    if existente is None:
        DISPOSITIVOS[body.device_id] = Dispositivo(body.device_id, body.device_secret)
    return {"ok": True}


def _autenticar(device_id: str, device_secret: str) -> Dispositivo:
    disp = DISPOSITIVOS.get(device_id)
    if disp is None or disp.secreto != device_secret:
        raise HTTPException(401, "device_id/secret inválido")
    return disp


@app.post("/devices/{device_id}/pairing-code")
def pedir_codigo(device_id: str, device_secret: str):
    disp = _autenticar(device_id, device_secret)
    disp.codigo_actual = _generar_codigo()
    disp.codigo_expira = time.time() + CODIGO_TTL_SEGUNDOS
    return {"code": disp.codigo_actual, "expira_en": CODIGO_TTL_SEGUNDOS}


class PairIn(BaseModel):
    device_id: str
    code: str


@app.post("/pair")
async def emparejar(body: PairIn):
    disp = DISPOSITIVOS.get(body.device_id)
    if disp is None:
        raise HTTPException(404, "Dispositivo no encontrado")
    if not disp.codigo_actual or time.time() > disp.codigo_expira:
        raise HTTPException(400, "Código vencido, pedí uno nuevo desde la Pi")
    if body.code.strip().upper() != disp.codigo_actual:
        raise HTTPException(400, "Código incorrecto")

    disp.emparejado = True
    disp.codigo_actual = None

    # Avisamos a la Pi en vivo (si su WS está conectado) para que la
    # pantalla pase de "mostrando QR" a "vinculado" sin que tenga que
    # estar preguntando todo el tiempo.
    if disp.websocket is not None:
        try:
            await disp.websocket.send_json({"type": "emparejado"})
        except Exception:
            pass

    return {"ok": True, "device_id": disp.device_id}


@app.get("/devices/{device_id}/status")
def estado_dispositivo(device_id: str):
    """La app lo consulta para mostrar el cartel de "Comedero offline".
    offline = más de OFFLINE_TRAS_SEGUNDOS sin conexión; desconectado_desde
    en epoch (segundos)."""
    disp = DISPOSITIVOS.get(device_id)
    if disp is None:
        # Si el relay se reinició y la Pi todavía no volvió a conectarse,
        # no la conocemos: la damos por desconectada desde el arranque.
        desde: Optional[float] = INICIO_RELAY
        emparejado = conectado = False
    else:
        conectado = disp.websocket is not None
        desde = None if conectado else (disp.desconectado_desde or INICIO_RELAY)
        emparejado = disp.emparejado
    return {
        "emparejado": emparejado,
        "conectado": conectado,
        "offline": desde is not None and time.time() - desde >= OFFLINE_TRAS_SEGUNDOS,
        "desconectado_desde": desde,
    }


class ComandoIn(BaseModel):
    accion: str
    duracion: Optional[float] = None


@app.post("/devices/{device_id}/command")
async def enviar_comando(device_id: str, body: ComandoIn):
    disp = DISPOSITIVOS.get(device_id)
    if disp is None:
        raise HTTPException(404, "Dispositivo no encontrado")
    if not disp.emparejado:
        raise HTTPException(403, "Este dispositivo todavía no fue vinculado")
    if disp.websocket is None:
        raise HTTPException(503, "La Raspberry no está conectada ahora mismo")

    request_id = secrets.token_hex(8)
    loop = asyncio.get_event_loop()
    futuro = loop.create_future()
    PEDIDOS_PENDIENTES[request_id] = futuro

    mensaje = {"type": "command", "request_id": request_id, "accion": body.accion}
    if body.duracion is not None:
        mensaje["duracion"] = body.duracion

    try:
        await disp.websocket.send_json(mensaje)
        resultado = await asyncio.wait_for(futuro, timeout=COMANDO_TIMEOUT_SEGUNDOS)
    except asyncio.TimeoutError:
        raise HTTPException(504, "La Raspberry no respondió a tiempo")
    finally:
        PEDIDOS_PENDIENTES.pop(request_id, None)

    return resultado


@app.websocket("/devices/{device_id}/ws")
async def ws_dispositivo(websocket: WebSocket, device_id: str, device_secret: str):
    disp = DISPOSITIVOS.get(device_id)
    if disp is None or disp.secreto != device_secret:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    disp.websocket = websocket
    disp.desconectado_desde = None
    if disp.avisado_offline:
        disp.avisado_offline = False
        # En una tarea aparte para no demorar los comandos que lleguen
        # justo al reconectar mientras esperamos a ntfy.
        tarea = asyncio.create_task(avisar_ntfy(
            "Comedero online",
            f"{disp.device_id} volvió a conectarse.",
            "white_check_mark",
        ))
        _TAREAS_AVISO.add(tarea)
        tarea.add_done_callback(_TAREAS_AVISO.discard)
    try:
        while True:
            mensaje = await websocket.receive_json()
            if mensaje.get("type") == "resultado":
                request_id = mensaje.get("request_id")
                futuro = PEDIDOS_PENDIENTES.get(request_id)
                if futuro is not None and not futuro.done():
                    futuro.set_result(
                        {k: v for k, v in mensaje.items() if k not in ("type", "request_id")}
                    )
    except WebSocketDisconnect:
        pass
    finally:
        if disp.websocket is websocket:
            disp.websocket = None
            disp.desconectado_desde = time.time()


@app.get("/")
def raiz():
    return {"servicio": "comedero-relay", "dispositivos_registrados": len(DISPOSITIVOS)}

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
import base64
import json
import os
import secrets
import string
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

CODIGO_TTL_SEGUNDOS = 10 * 60
COMANDO_TIMEOUT_SEGUNDOS = 15
PING_TIMEOUT_SEGUNDOS = 5

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
    # La Pi recuerda si ya la vincularon (en su device_id.json) y lo manda
    # en cada registro: así, si el relay se reinicia y pierde la memoria,
    # recupera la vinculación sin tener que volver a escanear el QR.
    emparejado: bool = False


@app.post("/devices/register")
def registrar_dispositivo(body: RegistroIn):
    existente = DISPOSITIVOS.get(body.device_id)
    if existente is not None and existente.secreto != body.device_secret:
        raise HTTPException(403, "device_id ya registrado con otro secreto")
    if existente is None:
        existente = DISPOSITIVOS[body.device_id] = Dispositivo(body.device_id, body.device_secret)
    if body.emparejado:
        # Confiable: el pedido trae el secreto que solo tiene esa Pi.
        existente.emparejado = True
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
    n: Optional[int] = None  # cantidad de registros, para accion=historial


async def _pedir_a_la_pi(disp: Dispositivo, mensaje: dict, timeout: float) -> dict:
    """Le manda un comando a la Pi por su WebSocket y espera la respuesta."""
    request_id = secrets.token_hex(8)
    futuro = asyncio.get_event_loop().create_future()
    PEDIDOS_PENDIENTES[request_id] = futuro
    try:
        await disp.websocket.send_json({"type": "command", "request_id": request_id, **mensaje})
        return await asyncio.wait_for(futuro, timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, "La Raspberry no respondió a tiempo")
    finally:
        PEDIDOS_PENDIENTES.pop(request_id, None)


@app.post("/devices/{device_id}/command")
async def enviar_comando(device_id: str, body: ComandoIn):
    disp = DISPOSITIVOS.get(device_id)
    if disp is None:
        raise HTTPException(404, "Dispositivo no encontrado")
    if not disp.emparejado:
        raise HTTPException(403, "Este dispositivo todavía no fue vinculado")
    if disp.websocket is None:
        raise HTTPException(503, "La Raspberry no está conectada ahora mismo")

    mensaje = {"accion": body.accion}
    if body.duracion is not None:
        mensaje["duracion"] = body.duracion
    if body.n is not None:
        mensaje["n"] = body.n
    return await _pedir_a_la_pi(disp, mensaje, COMANDO_TIMEOUT_SEGUNDOS)


@app.get("/devices/{device_id}/ping")
async def ping_dispositivo(device_id: str):
    """Chequeo de punta a punta: el pedido viaja hasta la Pi y vuelve. La
    app lo usa al abrirse para mostrar el cartel de "sin conexión" sin
    esperar a que el relay note que el WebSocket se cortó (si la Pi se
    apaga de golpe, la conexión puede quedar colgada un buen rato)."""
    disp = DISPOSITIVOS.get(device_id)
    if disp is None:
        raise HTTPException(404, "La Raspberry no se conectó desde que arrancó el relay")
    ws = disp.websocket
    if ws is None:
        raise HTTPException(503, "La Raspberry no está conectada ahora mismo")

    inicio = time.time()
    try:
        await _pedir_a_la_pi(disp, {"accion": "ping"}, PING_TIMEOUT_SEGUNDOS)
    except HTTPException:
        # No contestó: damos esa conexión por muerta, así el estado y el
        # aviso de ntfy cuentan desde ahora. Si la Pi sigue viva, reconecta
        # sola en unos segundos.
        if disp.websocket is ws:
            disp.websocket = None
            disp.desconectado_desde = time.time()
            tarea = asyncio.create_task(_cerrar_sin_esperar(ws))
            _TAREAS_AVISO.add(tarea)
            tarea.add_done_callback(_TAREAS_AVISO.discard)
        raise
    return {"ok": True, "latencia_ms": int((time.time() - inicio) * 1000)}


async def _cerrar_sin_esperar(ws: WebSocket):
    try:
        await asyncio.wait_for(ws.close(), timeout=5)
    except Exception:
        pass


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


# ---- Actualizaciones del ejecutable de la Pi ----
# GitHub Actions compila cada versión del repo de la Pi y la publica como
# release (ejecutable + manifest.json + manifest.sig). La Pi no habla con
# GitHub: le pide la versión a este relay con su device_secret, y el relay
# la consulta con un token de solo lectura que vive en las variables de
# entorno de Render. La firma la verifica la Pi: el relay solo la reenvía.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN_ACTUALIZACIONES", "").strip()
REPO_ACTUALIZACIONES = os.environ.get("REPO_ACTUALIZACIONES", "nestorpastor/comedero_raspberry")
GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
CACHE_RELEASE_SEGUNDOS = 300
_cache_release: dict = {"hasta": 0.0, "datos": None}


class _SinRedirecciones(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _github(ruta_o_url: str, binario: bool = False, seguir: bool = True):
    url = ruta_o_url if ruta_o_url.startswith("http") else f"{GITHUB_API}{ruta_o_url}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/octet-stream" if binario else "application/vnd.github+json")
    # "unredirected": si GitHub redirige a su almacenamiento, el token no
    # tiene que viajar (y el almacenamiento rechaza el pedido si lo lleva).
    req.add_unredirected_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    abridor = urllib.request.build_opener() if seguir else urllib.request.build_opener(_SinRedirecciones)
    return abridor.open(req, timeout=30)


def _ultima_version() -> Optional[dict]:
    """Última release publicada (con caché de 5 min): numero, manifiesto y
    firma en base64, e id del ejecutable. None si no hay ninguna."""
    if time.time() < _cache_release["hasta"]:
        return _cache_release["datos"]
    try:
        with _github(f"/repos/{REPO_ACTUALIZACIONES}/releases/latest") as r:
            release = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            datos = None
        else:
            raise HTTPException(502, f"GitHub respondió {e.code}")
    else:
        assets = {a["name"]: a for a in release.get("assets", [])}
        if not {"manifest.json", "manifest.sig", "comedero-aarch64"} <= assets.keys():
            datos = None  # release a medio publicar
        else:
            with _github(assets["manifest.json"]["url"], binario=True) as r:
                manifiesto = r.read()
            with _github(assets["manifest.sig"]["url"], binario=True) as r:
                firma = r.read()
            datos = {
                "numero": json.loads(manifiesto)["numero"],
                "manifiesto": base64.b64encode(manifiesto).decode(),
                "firma": base64.b64encode(firma).decode(),
                "id_ejecutable": assets["comedero-aarch64"]["id"],
            }
    _cache_release.update(hasta=time.time() + CACHE_RELEASE_SEGUNDOS, datos=datos)
    return datos


@app.get("/devices/{device_id}/actualizacion")
def ver_actualizacion(device_id: str, device_secret: str):
    _autenticar(device_id, device_secret)
    if not GITHUB_TOKEN:
        raise HTTPException(503, "Las actualizaciones no están configuradas en el relay")
    datos = _ultima_version()
    if datos is None:
        raise HTTPException(404, "Todavía no hay ninguna versión publicada")
    return {"manifiesto": datos["manifiesto"], "firma": datos["firma"]}


@app.get("/devices/{device_id}/actualizacion/ejecutable")
def bajar_ejecutable(device_id: str, device_secret: str, numero: int):
    _autenticar(device_id, device_secret)
    if not GITHUB_TOKEN:
        raise HTTPException(503, "Las actualizaciones no están configuradas en el relay")
    datos = _ultima_version()
    if datos is not None and datos["numero"] != numero:
        _cache_release["hasta"] = 0.0  # puede que haya salido una más nueva
        datos = _ultima_version()
    if datos is None or datos["numero"] != numero:
        raise HTTPException(409, "Esa versión ya no es la última publicada")
    # GitHub contesta con una redirección a una URL firmada y temporal de
    # su almacenamiento: se la pasamos a la Pi, que descarga directo de ahí
    # (el ejecutable no pasa por Render).
    url = f"{GITHUB_API}/repos/{REPO_ACTUALIZACIONES}/releases/assets/{datos['id_ejecutable']}"
    try:
        _github(url, binario=True, seguir=False).close()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return RedirectResponse(e.headers["Location"], status_code=302)
        raise HTTPException(502, f"GitHub respondió {e.code}")
    raise HTTPException(502, "GitHub no devolvió la descarga")


@app.get("/")
def raiz():
    return {"servicio": "comedero-relay", "dispositivos_registrados": len(DISPOSITIVOS)}

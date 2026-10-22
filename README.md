# Comedero Relay (backend en la nube)

Este es el "casamentero" entre la app del celular y cada Raspberry Pi: la
Pi abre una conexión permanente hacia acá, y este servicio le reenvía los
comandos que manda la app ("alimentar ahora"), sin importar en qué red
esté cada uno.

Es un prototipo: guarda todo en memoria (device_id, secretos, códigos de
emparejamiento) — si el servicio se reinicia, las Pis se vuelven a
registrar solas (no hace falta hacer nada), pero un dispositivo que
estaba emparejado va a tener que volver a vincularse escaneando un QR
nuevo. Para producción de verdad, el siguiente paso natural es sumar una
base de datos (Postgres) y cuentas de usuario.

## Probarlo en tu computadora

```bash
cd comedero_relay
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8090 --reload
```

Con eso corriendo, `python3 test_flujo.py` (necesita también `pip install
httpx websockets`) simula una Pi y una app hablando entre sí — es una
buena forma de confirmar que todo funciona antes de desplegarlo.

## Desplegarlo en Render (gratis)

1. Subí esta carpeta (`comedero_relay/`) a un repositorio de GitHub —
   Render despliega leyendo un repo, no aceptando un zip subido a mano.
2. Entrá a https://render.com, creá una cuenta gratis (podés loguearte
   con GitHub directamente).
3. **New +** → **Web Service** → elegí el repo donde subiste esta carpeta.
4. Configurá:
   - **Root Directory**: `comedero_relay` (si el repo tiene más cosas
     además de esta carpeta).
   - **Runtime**: Python 3.
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free.
5. Creá el servicio. Al terminar el deploy te da una URL pública fija,
   algo como `https://comedero-relay.onrender.com` — **esa es la URL que
   vas a cargar** en `config.json` de la Raspberry (`relay_url`) y en
   Ajustes de la app (URL del backend).

**Importante sobre el plan gratis de Render:** el servicio se "duerme"
después de unos 15 minutos sin tráfico, y tarda unos segundos en
despertarse con el primer pedido después de eso. Para este prototipo no
es un problema grave (la Pi se reconecta sola apenas el servicio
despierta), pero si en algún momento esto pasa a ser algo que la gente
usa todos los días, conviene pasar a un plan pago (no se duerme) o a
otro proveedor (Railway/Fly.io tienen planes similares).

## Avisos al celular (ntfy.sh)

El relay manda una notificación "Comedero offline" cuando una Pi pasa
más de 2 minutos sin conexión, y "Comedero online" cuando vuelve (solo
al cambiar de estado; un corte de unos segundos no avisa nada).

1. Instalá la app **ntfy** en el iPhone y suscribite a un tema con un
   nombre difícil de adivinar (cualquiera que sepa el nombre puede leer
   los avisos), por ejemplo `comedero-k7f2m9x4`.
2. En Render → tu servicio → **Environment**, agregá la variable
   `NTFY_TOPIC` con ese mismo nombre y guardá (Render redespliega solo).

Si `NTFY_TOPIC` no está definida, el relay funciona igual pero sin
avisos. Opcionales: `OFFLINE_TRAS_SEGUNDOS` (por defecto 120) y
`NTFY_URL` (por defecto `https://ntfy.sh`, por si usás un servidor ntfy
propio).

Limitación: el estado vive en memoria, así que si el relay se reinicia
mientras la Pi está apagada, no se entera de que existía y no avisa
hasta que la Pi vuelva a conectarse. Además, el relay tiene que estar
despierto para medir los 2 minutos (en el plan gratis, un monitor tipo
UptimeRobot evita que se duerma).

Para probarlo sin mandar nada a ntfy.sh: `python3 test_avisos.py`.

## Cómo se conecta cada lado

- **La Pi**: usa `cloud_relay.py` (en el proyecto `comedero_mascotas/`).
  Para activarlo, en `config.json` poné:
  ```json
  "relay_habilitado": true,
  "relay_url": "https://tu-backend.onrender.com"
  ```
  Al arrancar la app, un botón nuevo "Vincular app" en el encabezado
  muestra el QR para emparejar.

- **La app del celular**: en Ajustes, sección "Modo nube", cargá la
  misma URL del backend y tocá "Escanear QR para vincular". Después de
  escanear, la app usa este backend para todo (ya no hace falta la IP
  local ni Tailscale).

## Escalar a "muchos usuarios" de verdad

Este prototipo ya prueba el mecanismo (QR, emparejamiento, comandos de
punta a punta) pero es de un solo "espacio compartido": cualquiera que
tenga el código correcto puede emparejar con un dispositivo, y no hay
noción de "usuarios". Los pasos siguientes, en orden, serían:

1. Agregar una base de datos (Postgres, que Render también ofrece gratis
   en un plan chico) en vez de los diccionarios en memoria, para que los
   dispositivos y emparejamientos sobrevivan a un reinicio.
2. Agregar cuentas de usuario (login) y asociar cada dispositivo a un
   usuario dueño, en vez del `emparejado: bool` actual.
3. Permitir varios dispositivos por usuario y varios usuarios por
   dispositivo (por ejemplo, toda la familia con acceso al mismo
   comedero).
4. Sumar HTTPS "de verdad" con certificado propio si en algún momento se
   deja de depender de la URL que da Render (que ya viene con HTTPS
   incluido, así que esto no es urgente).

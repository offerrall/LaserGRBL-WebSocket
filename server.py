
import asyncio
import collections
import platform
import socket
import sys
from contextlib import suppress
from typing import Deque, Optional

import serial
import websockets

# ---------- Configuración por defecto ----------
GRBL_BUFFER_BYTES = 128          # búfer interno de GRBL
SAFETY_MARGIN = 4                # deja 4 bytes libres
MAX_PENDING = GRBL_BUFFER_BYTES - SAFETY_MARGIN
BAUDRATE = 115_200
SERIAL_TIMEOUT = 0               # lectura no bloqueante
RETRY_INTERVAL = 5               # s para reintentar el puerto serie
PING_SECONDS = 20                # keep-alive WebSocket
# ------------------------------------------------


class Bridge:
    """Mantiene el enlace WebSocket ⇆ GRBL con control de flujo."""

    def __init__(self, port: str, ws_port: int):
        self.port_name = port
        self.ws_port = ws_port
        self.serial: Optional[serial.Serial] = None
        self.client: Optional[websockets.WebSocketServerProtocol] = None
        self.pending: Deque[int] = collections.deque()   # bytes aún no confirmados
        self.recv_buf = bytearray()                      # ensamblador de líneas RX

    # ---------- utilidades de red ----------
    @staticmethod
    def local_ip() -> str:
        """Devuelve la IP local más probable (no 127.0.0.1)."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    # ---------- Serial ----------
    async def ensure_serial(self) -> bool:
        """Abre el puerto si está cerrado. Devuelve True en caso de éxito."""
        if self.serial and self.serial.is_open:
            return True

        with suppress(Exception):
            if self.serial:
                self.serial.close()

        try:
            self.serial = serial.Serial(
                self.port_name,
                BAUDRATE,
                timeout=SERIAL_TIMEOUT,
                write_timeout=0,
            )
            self.serial.reset_input_buffer()
            print(f"[SERIAL] Abierto {self.port_name}")
            return True
        except Exception as e:
            print(f"[SERIAL] No se pudo abrir {self.port_name}: {e}")
            self.serial = None
            return False

    def serial_write(self, line: bytes) -> None:
        """Envía una línea a GRBL respetando la ventana de flujo."""
        self.serial.write(line)
        self.pending.append(len(line))

    # ---------- WebSocket handler ----------
    async def ws_handler(self, websocket: websockets.WebSocketServerProtocol) -> None:
        """Gestiona un cliente WebSocket de principio a fin."""
        if self.client:
            await websocket.close(code=1000, reason="Server is busy")
            print("[WS] Conexión rechazada: otro cliente activo")
            return

        self.client = websocket
        print("[WS] Cliente conectado")

        try:
            async for raw in websocket:
                if isinstance(raw, str):
                    raw = raw.encode()

                # Asegura que termina en LF
                if not raw.endswith(b"\n"):
                    raw += b"\n"

                # Asegura serial abierto
                if not await self.ensure_serial():
                    await websocket.send(b'{"error":"Serial port not connected"}')
                    continue

                # Ventana de emisión: espera hueco
                while sum(self.pending) + len(raw) >= MAX_PENDING:
                    await asyncio.sleep(0)

                self.serial_write(raw)
                if raw.strip():
                    print(f"-> GRBL: {raw.decode(errors='replace').strip()}")

        except websockets.exceptions.ConnectionClosedOK:
            pass
        except Exception as e:
            print(f"[WS] Error: {e}")
        finally:
            print("[WS] Cliente desconectado")
            self.client = None
            # Vacía la cola pendiente por seguridad
            self.pending.clear()

    # ---------- Lector de GRBL ----------
    async def serial_reader(self) -> None:
        """Lee continuamente del puerto serie y reenvía al WebSocket."""
        while True:
            if not await self.ensure_serial():
                await asyncio.sleep(RETRY_INTERVAL)
                continue

            try:
                # Lee todo lo disponible
                in_waiting = self.serial.in_waiting
                if in_waiting:
                    self.recv_buf += self.serial.read(in_waiting)

                # Extrae líneas completas
                while b"\n" in self.recv_buf:
                    line, _, self.recv_buf = self.recv_buf.partition(b"\n")
                    if not line:
                        continue  # ignora líneas vacías

                    decoded = line.decode(errors="replace")
                    print(f"<- GRBL: {decoded}")

                    # Control de flujo
                    if line.startswith(b"ok") and self.pending:
                        self.pending.popleft()

                    # Reenvía al cliente
                    if self.client:
                        with suppress(Exception):
                            await self.client.send(line + b"\n")

            except Exception as e:
                print(f"[SERIAL] Error de lectura: {e}")
                with suppress(Exception):
                    self.serial.close()
                self.serial = None

            await asyncio.sleep(0)  # cede control inmediatamente

    # ---------- Watchdog ----------
    async def serial_watchdog(self) -> None:
        while True:
            if not await self.ensure_serial():
                await asyncio.sleep(RETRY_INTERVAL)
            else:
                await asyncio.sleep(60)

    # ---------- Servidor principal ----------
    async def run(self) -> None:
        await self.ensure_serial()

        ws_server = await websockets.serve(
            self.ws_handler,
            host="0.0.0.0",
            port=self.ws_port,
            ping_interval=PING_SECONDS,
            ping_timeout=PING_SECONDS * 2,
        )

        ip = self.local_ip()
        print(f"[INFO] WebSocket en ws://{ip}:{self.ws_port}")
        print(f"[INFO] Pulsa Ctrl-C para salir\n")

        await asyncio.gather(
            self.serial_reader(),
            self.serial_watchdog(),
            ws_server.wait_closed(),
        )


# ---------- CLI ----------
def parse_args() -> tuple[str, int]:
    if len(sys.argv) != 3:
        print(
            "Uso:\n"
            "  Windows: bridge.py <COMn> <puerto WS>\n"
            "  Linux/Mac: bridge.py </dev/ttyUSB0> <puerto WS>"
        )
        sys.exit(1)

    port_arg = sys.argv[1]
    if platform.system() == "Windows" and port_arg.isdigit():
        port_arg = "COM" + port_arg
    elif not port_arg.startswith(("COM", "/dev/")):
        port_arg = "/dev/tty" + port_arg  # fallback

    return port_arg, int(sys.argv[2])


if __name__ == "__main__":
    serial_port, ws_port = parse_args()
    bridge = Bridge(serial_port, ws_port)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n[INFO] Fin por Ctrl-C")
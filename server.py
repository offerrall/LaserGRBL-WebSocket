import asyncio
import websockets
import serial
import sys
import platform
import socket

state = {
    "active_client": None,
    "serial_port": None,
    "command_queue": asyncio.Queue(),
    "waiting_for_ok": asyncio.Event()
}

CONFIG = {
    "port": None,
    "baud_rate": 115200,
    "timeout": 0.1,
    "ws_port": None
}

RETRY_INTERVAL = 5

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

async def handle_websocket(websocket):
    if state["active_client"]:
        await websocket.close(1000, "Server busy")
        return

    state["active_client"] = websocket
    print("Client connected")

    try:
        async for message in websocket:
            if isinstance(message, str):
                message = message.encode('utf-8')

            # Add command to the queue
            await state["command_queue"].put(message)
    except:
        pass
    finally:
        print("Client disconnected")
        state["active_client"] = None

async def serial_writer():
    while True:
        cmd = await state["command_queue"].get()

        if state["serial_port"] and state["serial_port"].is_open:
            try:
                state["waiting_for_ok"].clear()
                state["serial_port"].write(cmd)
                print(f"-> GRBL: {cmd.decode('utf-8').strip()}")
                await state["waiting_for_ok"].wait()
            except Exception as e:
                print(f"Serial write error: {e}")
        else:
            print("Serial port not ready, skipping command")

async def read_from_serial():
    while True:
        if state["serial_port"] and state["serial_port"].is_open:
            try:
                if state["serial_port"].in_waiting:
                    line = state["serial_port"].readline()
                    decoded = line.decode('utf-8', errors='replace').strip()
                    print(f"<- GRBL: {decoded}")

                    if state["active_client"]:
                        await state["active_client"].send(line)

                    if decoded == "ok":
                        state["waiting_for_ok"].set()

            except Exception as e:
                print(f"Serial read error: {e}")
        await asyncio.sleep(0.01)

async def try_open_serial():
    if state["serial_port"] and state["serial_port"].is_open:
        return True
    try:
        state["serial_port"] = serial.Serial(
            CONFIG["port"], CONFIG["baud_rate"], timeout=CONFIG["timeout"]
        )
        print(f"Opened serial port: {CONFIG['port']}")
        return True
    except Exception as e:
        print(f"Failed to open serial port: {e}")
        state["serial_port"] = None
        return False

async def watchdog():
    while True:
        if not state["serial_port"] or not state["serial_port"].is_open:
            await try_open_serial()
        await asyncio.sleep(RETRY_INTERVAL)

async def main():
    if len(sys.argv) < 3:
        print("Usage: server.py <serial> <ws_port>")
        return

    CONFIG["port"] = sys.argv[1]
    CONFIG["ws_port"] = int(sys.argv[2])

    await try_open_serial()

    print(f"Server on ws://{get_local_ip()}:{CONFIG['ws_port']}")

    async with websockets.serve(handle_websocket, "0.0.0.0", CONFIG["ws_port"]):
        await asyncio.gather(
            read_from_serial(),
            serial_writer(),
            watchdog()
        )

if __name__ == "__main__":
    asyncio.run(main())

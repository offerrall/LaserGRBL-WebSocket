import asyncio
import websockets
import serial
import sys
import platform
import socket


state = {
    "active_client": None,
    "serial_port": None
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
    if state["active_client"] is not None:
        await websocket.close(1000, "Server is busy with another client")
        print(f"Connection rejected: server already has an active client")
        return
    
    state["active_client"] = websocket
    print(f"New client connected")
    
    try:
        async for message in websocket:
            if not state["serial_port"] or not state["serial_port"].is_open:
                await try_open_serial()
                
            if not state["serial_port"] or not state["serial_port"].is_open:
                try:
                    await websocket.send(b'{"error": "Serial port not connected"}')
                except:
                    pass
                continue
                
            if isinstance(message, str):
                message = message.encode('utf-8')
            
            try:
                state["serial_port"].write(message)
                
                if message.endswith(b'\n'):
                    print(f"-> GRBL: {message.decode('utf-8', errors='replace').strip()}")
            except Exception as e:
                print(f"Error writing to serial: {e}")
                close_serial()
                
    except Exception as e:
        print(f"Websocket error: {e}")
    finally:
        if state["active_client"] == websocket:
            state["active_client"] = None
        print(f"Client disconnected")

def close_serial():
    if state["serial_port"]:
        try:
            if state["serial_port"].is_open:
                state["serial_port"].close()
                print("Closed serial port")
        except Exception as e:
            print(f"Error closing serial port: {e}")
        state["serial_port"] = None

async def try_open_serial():
    if state["serial_port"] and state["serial_port"].is_open:
        return True 
        
    try:
        state["serial_port"] = serial.Serial(
            CONFIG["port"], 
            CONFIG["baud_rate"], 
            timeout=CONFIG["timeout"]
        )
        print(f"Opened serial port: {CONFIG['port']}")
        return True
    except Exception as e:
        print(f"Failed to open serial port: {e}")
        state["serial_port"] = None
        return False

async def read_from_serial():
    while True:
        if not state["serial_port"] or not state["serial_port"].is_open:
            if await try_open_serial():
                print(f"Serial port reconnected")
            else:
                await asyncio.sleep(RETRY_INTERVAL)
                continue
                
        try:
            if state["serial_port"].in_waiting > 0:
                data = state["serial_port"].readline()
                if data:
                    message = data.decode('utf-8', errors='replace').strip()
                    if message:
                        print(f"<- GRBL: {message}")
                        
                        if state["active_client"]:
                            try:
                                await state["active_client"].send(data)
                            except websockets.exceptions.ConnectionClosed:
                                state["active_client"] = None
                                print("Client connection closed during send")
        except Exception as e:
            print(f"Error reading from serial: {e}")
            close_serial()
            
        await asyncio.sleep(0.01)

async def serial_watchdog():
    while True:
        if not state["serial_port"] or not state["serial_port"].is_open:
            await try_open_serial()
        await asyncio.sleep(RETRY_INTERVAL)

async def main():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  Windows: server.py <COM port number> <WebSocket port>")
        print("  Example: server.py 3 8765 (for COM3)")
        print("  Linux/Mac: server.py <serial device path> <WebSocket port>")
        print("  Example: server.py /dev/ttyUSB0 8765")
        return

    system = platform.system()
    if system == "Windows":
        if sys.argv[1].isdigit():
            CONFIG["port"] = "COM" + sys.argv[1]
        else:
            CONFIG["port"] = sys.argv[1]
    else:
        if sys.argv[1].startswith('/dev/'):
            CONFIG["port"] = sys.argv[1]
        else:
            CONFIG["port"] = "/dev/tty" + sys.argv[1]
    
    CONFIG["ws_port"] = int(sys.argv[2])
    
    print(f"Operating System: {system}")
    print(f"Using serial port: {CONFIG['port']}")
    
    local_ip = get_local_ip()
    
    try:
        await try_open_serial()
        
        async with websockets.serve(handle_websocket, "0.0.0.0", CONFIG["ws_port"]):
            print(f"URL: ws://{local_ip}:{CONFIG['ws_port']}")
            
            serial_reader = asyncio.create_task(read_from_serial())
            watchdog = asyncio.create_task(serial_watchdog())
            
            await asyncio.Future()
    
    except Exception as e:
        print(f"Server error: {e}")
    finally:
        close_serial()
        print("Server shutdown")

if __name__ == "__main__":
    asyncio.run(main())
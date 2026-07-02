# Hardware abstraction layer for device communication

from __future__ import annotations

import time

try:
    import pyvisa
except Exception as e:
    pyvisa = None
    print(f"PyVISA unavailable; hardware communication disabled: {e}")

try:
    from pyvisa.constants import Parity, StopBits
    from pyvisa.resources import Resource, USBInstrument, SerialInstrument
except Exception:
    Parity = StopBits = None
    Resource = USBInstrument = SerialInstrument = object


VISA_RM = None

def get_visa_rm():
    global VISA_RM
    if pyvisa is None:
        raise RuntimeError("PyVISA is not available.")
    if VISA_RM is None:
        VISA_RM = pyvisa.highlevel.ResourceManager()
    return VISA_RM

def close_resource_manager():
    global VISA_RM
    if VISA_RM is not None:
        try:
            VISA_RM.close()
        finally:
            VISA_RM = None

# hardware constraints
LCR_MIN_FREQ = 20.0
LCR_MAX_FREQ = 300e3
RELAY_MIN_COMMAND_INTERVAL = 0.05
RELAY_BREAK_BEFORE_MAKE = 0.10
RELAY_TIMEOUT = 1000

# basic send structure
def send(dev, cmd: str = "", read_after_write: bool = False):
    cmd = cmd.strip()
    if not cmd:
        return (-1, "No command entered.")

    try:
        if cmd.endswith("?"):
            reply = dev.query(cmd).strip()
            return (len(cmd), reply)

        write_len = dev.write(cmd)
        if read_after_write:
            reply = dev.read().strip()
            return (write_len, reply)

        return (write_len, "N/A")
    except Exception as e:
        return (-1, f"Error: {type(e).__name__}: {e}")


class Device:
    name: str
    address: str
    device: Resource  # type: ignore

    def send(
        self, cmd: str = "", read_after_write: bool = False
    ) -> tuple[int, str]:
        ...

    def close(self):
        dev = getattr(self, "device", None)
        if dev is not None and hasattr(dev, "close"):
            dev.close()


class KeysightLCR_E4980A(Device):
    min_freq = LCR_MIN_FREQ
    max_freq = LCR_MAX_FREQ
    name = "Keysight LCR Meter, #E4980A"
    address = "USB4::0x2A8D::0x2F01::MY54412454::INSTR"  # verify

    def __init__(self):
        self.device: USBInstrument = get_visa_rm().open_resource(  # type: ignore
            self.address, resource_pyclass=USBInstrument
        )  # type: ignore
        self.device.timeout = 15e3
        self.device.read_termination = self.device.write_termination = "\n"

    def send(self, cmd: str = "", read_after_write: bool = False):
        return send(self.device, cmd, read_after_write=read_after_write)


class DenkoviRelayBoard(Device):
    min_wait = RELAY_MIN_COMMAND_INTERVAL
    name = "Denkovi 16-channel Relay Board"
    address = "ASRL5::INSTR"  # TODO: replace with actual relay COM/VISA address

    def __init__(self):
        self.device: SerialInstrument = get_visa_rm().open_resource(  # type: ignore
            self.address, resource_pyclass=SerialInstrument
        )  # type: ignore
        # requirements by manual
        self.device.baud_rate = 9600
        self.device.data_bits = 8
        if StopBits is not None:
            self.device.stop_bits = StopBits.one
        else:
            self.device.stop_bits = 1
        if Parity is not None:
            self.device.parity = Parity.none
        else:
            self.device.parity = 0
        self.device.timeout = RELAY_TIMEOUT
        self.last_command_time = 0.0

        self.all_off()

    def _wait_for_command_interval(self):
        elapsed = time.time() - self.last_command_time
        remaining = self.min_wait - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def send_raw(self, payload: bytes, expected_len: int) -> bytes:
        try:
            self.device.clear()
        except Exception:
            pass

        self._wait_for_command_interval()
        self.device.write_raw(payload)
        self.last_command_time = time.time()

        if expected_len <= 0:
            return b""

        return self.device.read_bytes(expected_len)

    def all_off(self):
        cmd = b"off//"
        reply = self.send_raw(cmd, expected_len=len(cmd))
        if reply != cmd:
            raise RuntimeError(f"Relay all-off failed: expected {cmd!r}, got {reply!r}")
        return True

    def set_relay(self, relay_index: int, enabled: bool):
        if relay_index < 1 or relay_index > 16:
            raise ValueError("Relay index must be between 1 and 16.")

        sign = b"+" if enabled else b"-"
        cmd = f"{relay_index:02d}".encode("ascii") + sign + b"//"
        reply = self.send_raw(cmd, expected_len=len(cmd))
        if reply != cmd:
            raise RuntimeError(f"Relay command failed: expected {cmd!r}, got {reply!r}")
        return True

    def get_status(self) -> dict[int, bool]:
        reply = self.send_raw(b"ask//", expected_len=2)
        if len(reply) != 2:
            raise RuntimeError(f"Bad relay status reply: {reply!r}")

        byte1, byte2 = reply
        status: dict[int, bool] = {}

        # Denkovi status mapping:
        # byte 1 bit 7 = relay 1 ... byte 1 bit 0 = relay 8
        # byte 2 bit 7 = relay 9 ... byte 2 bit 0 = relay 16
        for relay in range(1, 9):
            status[relay] = bool(byte1 & (1 << (8 - relay)))
        for relay in range(9, 17):
            status[relay] = bool(byte2 & (1 << (16 - relay)))

        return status

    def select_one(self, relay_index: int):
        if relay_index < 1 or relay_index > 16:
            raise ValueError("Relay index must be between 1 and 16.")

        self.all_off()
        time.sleep(RELAY_BREAK_BEFORE_MAKE)
        self.set_relay(relay_index, True)

        status = self.get_status()
        expected = {relay: relay == relay_index for relay in range(1, 17)}
        if status != expected:
            raise RuntimeError(
                f"Relay verification failed after selecting relay {relay_index}: {status}"
            )

        return True

    def send(self, cmd: str = "", read_after_write: bool = False):
        cmd = cmd.strip()
        if not cmd:
            return (-1, "No command entered.")

        try:
            if cmd == "ask//":
                status = self.get_status()
                return (len(cmd), str(status))
            if cmd == "on//":
                return (-1, "Relay all-on command is disabled for RT-BDS safety.")

            payload = cmd.encode("ascii")
            reply = self.send_raw(payload, expected_len=len(payload))
            return (len(payload), reply.decode("ascii", errors="replace"))
        except Exception as e:
            return (-1, f"Error: {type(e).__name__}: {e}")


DEVICE_TYPE_LIST: list[type[Device]] = [
    KeysightLCR_E4980A,
    DenkoviRelayBoard,
]

# Print devices
STARTUP = False

if STARTUP:
    rm = get_visa_rm()
    resources = rm.list_resources()

    print("Connected devices:")

    for device in DEVICE_TYPE_LIST:
        connected = device.address in resources
        symbol = "✓" if connected else "✗"
        print(f"{symbol} {device.name}")
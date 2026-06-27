from __future__ import annotations

try:
    import pyvisa  # PyVISA backend for communicating with devices
except Exception as e:
    pyvisa = None
    print(f"PyVISA unavailable; hardware communication disabled: {e}")
# from pyvisa.errors import (
#    # VisaIOError,
# )  # PyVISA.Constants.StatusCode for discerning error types
try:
    from pyvisa.constants import (
        # StatusCode,
        EventType,
        EventMechanism,
    )  # PyVISA.Constants.StatusCode for discerning error/return codes
    from pyvisa.resources import (
        Resource,
        USBInstrument,
        GPIBInstrument,
    )  # PyVISA.Resources.USBInstrument for type-casting the correct device communication format under VISA
except Exception:
    EventType = EventMechanism = None
    Resource = USBInstrument = GPIBInstrument = object


# RESOURCES ==========================================
VISA_RM = None
NIDAQ_SYSTEM = None


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


# STARTUP DEBUG ======================================
STARTUP = False
if STARTUP:
    print("Seen devices ...")
    for key, value in get_visa_rm().list_resources_info().items():
        print(key, value)
    print("Talking devices ...")
    for val in get_visa_rm().list_resources():
        print(val)
    print("... End of devices.")

# CONSTANTS ==========================================
LCR_MIN_FREQ = 20.0  # Hz, device hardware constant
LCR_MAX_FREQ = 300e3  # Hz, device hardware constant


# VISA SEND HELPER METHOD ================================
def send(dev, cmd: str = "", read_after_write: bool = False):
    cmd = cmd.strip()

    # empty
    if not cmd:
        return (-1, "No command entered.")

    try:
        # if query, send query and return response
        if cmd.endswith("?"):
            reply = dev.query(cmd).strip()
            return (len(cmd), reply)

        # normal command
        write_len = dev.write(cmd)

        # does not have ? but response is expected, so read after write
        if read_after_write:
            reply = dev.read().strip()
            return (write_len, reply)

        return (write_len, "N/A")

    except Exception as e:
        return (-1, f"Error: {type(e).__name__}: {e}")


# BASE DEVICE CLASS =======================================================
class Device:
    name: str
    address: str
    device: Resource

    def send(
        self, cmd: str = "", read_after_write: bool = False
    ) -> tuple[int, str]: ...
    def wait_interrupt(self, max_time: int): ...

    def close(self):
        dev = getattr(self, "device", None)
        if dev is not None and hasattr(dev, "close"):
            dev.close()


# LCR ================================
class KeysightLCR_E4980A(Device):
    min_freq = LCR_MIN_FREQ
    max_freq = LCR_MAX_FREQ
    name = "Keysight LCR Meter, #E4980A"
    address = "USB0::0x2A8D::0x2F01::MY54412453::INSTR"

    def __init__(self):
        self.device: USBInstrument = get_visa_rm().open_resource(self.address, resource_pyclass=USBInstrument)  # type: ignore
        self.device.timeout = 15e3  # ms
        self.device.read_termination = self.device.write_termination = "\n"

    def send(self, cmd: str = "", read_after_write: bool = False):
        return send(self.device, cmd, read_after_write=read_after_write)



# DEVICE REGISTRY =======================
DEVICE_TYPE_LIST: list[type[Device]] = [
    KeysightLCR_E4980A,
    # board control
]

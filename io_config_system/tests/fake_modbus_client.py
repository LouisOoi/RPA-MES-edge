"""
A pymodbus-shaped test double. Same call surface as ModbusSerialClient /
ModbusTcpClient (read_coils/read_holding_registers/read_input_registers/
write_coil, all keyword `count=`/`device_id=`, response objects with
.isError()/.bits/.registers) so engine code under test never knows it isn't
talking to a real client. Coils/registers are keyed by (device_id, address)
so one fake instance can stand in for a shared RTU bus across multiple
device_ids, exactly like the real shared ModbusSerialClient does.
"""
from __future__ import annotations


class FakeResponse:
    def __init__(self, *, bits=None, registers=None, error=False, message="fake error"):
        self._bits = bits
        self._registers = registers
        self._error = error
        self._message = message

    def isError(self) -> bool:
        return self._error

    @property
    def bits(self):
        return self._bits

    @property
    def registers(self):
        return self._registers

    def __str__(self) -> str:
        return self._message


class FakeModbusClient:
    def __init__(self) -> None:
        self.coils: dict[tuple[int, int], bool] = {}
        self.registers: dict[tuple[int, int], int] = {}
        self.calls: list[tuple] = []
        self.fail_addresses: set[tuple[int, int]] = set()  # (device_id, address) -> force error

    def connect(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def read_coils(self, address, *, count=1, device_id=1):
        self.calls.append(("read_coils", address, count, device_id))
        if (device_id, address) in self.fail_addresses:
            return FakeResponse(error=True, message="simulated timeout")
        bits = [self.coils.get((device_id, address + i), False) for i in range(count)]
        return FakeResponse(bits=bits)

    def read_holding_registers(self, address, *, count=1, device_id=1):
        self.calls.append(("read_holding_registers", address, count, device_id))
        if (device_id, address) in self.fail_addresses:
            return FakeResponse(error=True, message="simulated timeout")
        regs = [self.registers.get((device_id, address + i), 0) for i in range(count)]
        return FakeResponse(registers=regs)

    def read_input_registers(self, address, *, count=1, device_id=1):
        self.calls.append(("read_input_registers", address, count, device_id))
        if (device_id, address) in self.fail_addresses:
            return FakeResponse(error=True, message="simulated timeout")
        regs = [self.registers.get((device_id, address + i), 0) for i in range(count)]
        return FakeResponse(registers=regs)

    def write_coil(self, address, value, *, device_id=1):
        self.calls.append(("write_coil", address, value, device_id))
        if (device_id, address) in self.fail_addresses:
            return FakeResponse(error=True, message="simulated timeout")
        self.coils[(device_id, address)] = value
        return FakeResponse(error=False)

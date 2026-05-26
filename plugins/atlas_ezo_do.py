# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from time import sleep

try:
    import busio  # type: ignore
except Exception:  # pragma: no cover
    busio = None  # type: ignore

from pioreactor.config import config


@dataclass(frozen=True)
class EzoResponse:
    status_code: int
    body: str

    @property
    def ok(self) -> bool:
        # Atlas EZO: 1 = success, 2 = failed, 254 = pending, 255 = no data
        return self.status_code == 1


class AtlasEzoDO:
    DEFAULT_READ_BYTES = 31
    DEFAULT_I2C_ADDRESS = 0x61

    def __init__(self, *, i2c, address: int) -> None:
        self.i2c = i2c
        self.address = address

    @classmethod
    def from_config(cls) -> "AtlasEzoDO":
        try:
            address = int(config.get("do_reading.config", "i2c_channel_hex"), base=16)
        except Exception:
            address = cls.DEFAULT_I2C_ADDRESS

        if busio is None:  # pragma: no cover
            raise RuntimeError("busio is not available in this environment.")

        try:
            from pioreactor.hardware import get_scl_pin, get_sda_pin

            i2c = busio.I2C(get_scl_pin(), get_sda_pin())
        except Exception:
            i2c = busio.I2C(3, 2)

        return cls(i2c=i2c, address=address)

    def write(self, cmd: str) -> None:
        self.i2c.writeto(self.address, bytes(cmd + "\x00", "latin-1"))

    def _raw_read(self, num_bytes: int = DEFAULT_READ_BYTES) -> bytearray:
        result = bytearray(num_bytes)
        self.i2c.readfrom_into(self.address, result)
        return result

    @staticmethod
    def _strip_zeros(raw: bytearray) -> list[int]:
        return [b for b in raw if b != 0]

    @staticmethod
    def _handle_raspi_glitch(raw: list[int]) -> list[int]:
        return [b & ~0x80 for b in raw]

    def read_response(self, *, num_bytes: int = DEFAULT_READ_BYTES) -> EzoResponse:
        cleaned = self._strip_zeros(self._raw_read(num_bytes))
        if not cleaned:
            return EzoResponse(status_code=255, body="")

        status = int(cleaned[0])
        body = "".join(chr(b) for b in self._handle_raspi_glitch(cleaned[1:])).strip()
        return EzoResponse(status_code=status, body=body)

    def query(self, cmd: str, *, timeout_s: float = 1.5) -> EzoResponse:
        self.write(cmd)
        sleep(timeout_s)
        return self.read_response()

    def ensure_i2c_mode_and_address(self, *, desired_address: int = DEFAULT_I2C_ADDRESS) -> None:
        """
        Best-effort check for I2C mode / address.

        If already in I2C mode, query current address and set to desired address
        when different. If circuit is in UART mode, this cannot be changed over
        I2C and an exception is raised.
        """
        resp = self.query("I2C,?", timeout_s=0.8)
        if not resp.ok:
            raise RuntimeError(
                "Unable to verify DO circuit I2C mode/address. "
                "If the EZO-DO is in UART mode, switch it to I2C first."
            )

        body = resp.body.strip()
        if body.startswith("?I2C,"):
            try:
                current = int(body.split(",")[1])
            except Exception:
                current = desired_address
            if current != desired_address:
                set_resp = self.query(f"I2C,{desired_address}", timeout_s=0.8)
                if not set_resp.ok:
                    raise RuntimeError(
                        f"Failed to set EZO-DO I2C address to 0x{desired_address:02X}: "
                        f"status={set_resp.status_code} body={set_resp.body!r}"
                    )

    def read_do(self, *, samples: int = 2, inter_sample_delay_s: float = 0.05) -> float:
        if samples < 1:
            raise ValueError("samples must be >= 1")

        values: list[float] = []
        for _ in range(samples):
            resp = self.query("R")
            if not resp.ok:
                raise RuntimeError(
                    f"EZO-DO read failed: status={resp.status_code} body={resp.body!r}"
                )
            values.append(float(resp.body))
            sleep(inter_sample_delay_s)
        return sum(values) / len(values)

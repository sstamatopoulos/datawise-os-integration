"""
datagates.core.binary — turning registers and byte blocks into numbers.

Modbus and S7 both hand a gate raw 16-bit words or a block of bytes and
leave the interpretation to the integrator. Getting it wrong is the most
common reason a legacy point reads 0.0, 65535 or 3.8e-41 instead of a
temperature, so the decoding lives in one tested place.

What a configuration has to say, and why:

- **type** — `int16`, `uint16`, `int32`, `uint32`, `int64`, `uint64`,
  `float32`, `float64`, `bool`, or `string:<n>`. Modbus has no types; the
  device's register map does, and half of them are wrong about signedness
  until a value goes negative in January.
- **word_order** — `big` (the usual, high word first) or `little`. This
  is the infamous "byte swap" of every energy meter manual: the Modbus
  specification fixes the order of bytes within a word but says nothing
  about the order of words within a 32-bit value, so vendors split evenly.
  A 32-bit value that reads plausibly but ~65536 times too large or as
  noise is nearly always this.
- **byte_order** — `big` (Modbus and S7 are big-endian on the wire) or
  `little` for the rare gateway that swaps within the word too.
"""
from __future__ import annotations

import struct

FORMATS: dict[str, tuple[str, int]] = {
    # name: (struct code without endianness, size in bytes)
    "int16": ("h", 2), "uint16": ("H", 2),
    "int32": ("i", 4), "uint32": ("I", 4),
    "int64": ("q", 8), "uint64": ("Q", 8),
    "float32": ("f", 4), "float64": ("d", 8),
    "bool": ("?", 1),
}


def size_of(dtype: str) -> int:
    """Bytes a value of this type occupies."""
    dtype = (dtype or "uint16").lower()
    if dtype.startswith("string:"):
        return int(dtype.split(":", 1)[1])
    if dtype not in FORMATS:
        raise ValueError(f"unknown type {dtype!r}; use one of {', '.join(FORMATS)} or string:<bytes>")
    return FORMATS[dtype][1]


def register_count(dtype: str) -> int:
    """16-bit registers a value of this type occupies (Modbus counts in
    registers, never in bytes)."""
    return max(1, (size_of(dtype) + 1) // 2)


def registers_to_bytes(registers: list[int], word_order: str = "big", byte_order: str = "big") -> bytes:
    """Concatenate 16-bit registers into the byte string to decode."""
    words = list(registers)
    if word_order.lower().startswith("little"):
        words.reverse()
    endian = "<" if byte_order.lower().startswith("little") else ">"
    return b"".join(struct.pack(f"{endian}H", int(w) & 0xFFFF) for w in words)


def decode(data: bytes, dtype: str = "uint16", byte_order: str = "big") -> float | int | bool | str | None:
    """Decode one value out of `data`. None when the block is too short,
    which happens when a device answers a partial read rather than an
    exception — silently, on several PLC gateways."""
    dtype = (dtype or "uint16").lower()
    if dtype.startswith("string:"):
        return data[: size_of(dtype)].decode("latin-1", errors="replace").strip("\x00 ").strip() or None
    code, size = FORMATS[dtype]
    if len(data) < size:
        return None
    endian = "<" if byte_order.lower().startswith("little") else ">"
    (value,) = struct.unpack(f"{endian}{code}", data[:size])
    return value


def decode_registers(registers: list[int], dtype: str = "uint16",
                     word_order: str = "big", byte_order: str = "big") -> float | int | bool | str | None:
    """The Modbus path: registers -> bytes -> value."""
    if not registers:
        return None
    return decode(registers_to_bytes(registers, word_order, byte_order), dtype, byte_order)


def decode_bits(registers: list[int], bit: int) -> int | None:
    """One bit of a status word, for the alarm and mode points that legacy
    controllers pack into a single register."""
    if not registers:
        return None
    word = int(registers[bit // 16]) if bit // 16 < len(registers) else None
    return None if word is None else (word >> (bit % 16)) & 1

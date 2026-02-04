from __future__ import annotations

import io
from typing import Iterable, Sequence

import discord


def file_from_bytes(data: bytes, *, filename: str) -> discord.File:
    return discord.File(fp=io.BytesIO(bytes(data)), filename=str(filename or "file.bin"))


def file_from_png_bytes(png_bytes: bytes, *, filename: str) -> discord.File:
    return file_from_bytes(png_bytes, filename=str(filename or "chart.png"))


def files_from_name_bytes(files: Sequence[tuple[str, bytes]] | Iterable[tuple[str, bytes]]) -> list[discord.File]:
    out: list[discord.File] = []
    for name, blob in files:
        try:
            out.append(file_from_bytes(blob, filename=str(name or "file.bin")))
        except Exception:
            continue
    return out

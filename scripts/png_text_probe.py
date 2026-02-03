import argparse
import os
import struct
import sys
import zlib


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _iter_png_chunks(png: bytes):
    if not png.startswith(_PNG_SIG):
        raise ValueError("not a PNG")
    i = len(_PNG_SIG)
    n = len(png)
    while i + 8 <= n:
        (length,) = struct.unpack(">I", png[i : i + 4])
        ctype = png[i + 4 : i + 8]
        i += 8
        if i + length + 4 > n:
            break
        data = png[i : i + length]
        i += length
        crc = png[i : i + 4]
        i += 4
        yield ctype, data, crc
        if ctype == b"IEND":
            break


def _split_nul(data: bytes):
    idx = data.find(b"\x00")
    if idx < 0:
        return data, b""
    return data[:idx], data[idx + 1 :]


def read_png_text_metadata(png: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for ctype, data, _crc in _iter_png_chunks(png):
        try:
            if ctype == b"tEXt":
                key_b, text_b = _split_nul(data)
                key = key_b.decode("latin-1", errors="replace")
                text = text_b.decode("latin-1", errors="replace")
                out[key] = text
            elif ctype == b"zTXt":
                key_b, rest = _split_nul(data)
                if not rest:
                    continue
                comp_method = rest[0]
                comp_data = rest[1:]
                if comp_method != 0:
                    continue
                key = key_b.decode("latin-1", errors="replace")
                text = zlib.decompress(comp_data).decode("latin-1", errors="replace")
                out[key] = text
            elif ctype == b"iTXt":
                # keyword\0 compression_flag\0 compression_method\0 language_tag\0 translated_keyword\0 text
                key_b, rest = _split_nul(data)
                if len(rest) < 2:
                    continue
                comp_flag = rest[0]
                comp_method = rest[1]
                rest2 = rest[2:]
                lang_b, rest3 = _split_nul(rest2)
                _trans_b, rest4 = _split_nul(rest3)
                text_b = rest4
                if comp_flag == 1:
                    if comp_method != 0:
                        continue
                    text_b = zlib.decompress(text_b)
                key = key_b.decode("utf-8", errors="replace")
                text = text_b.decode("utf-8", errors="replace")
                out[key] = text
        except Exception:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="Path to a PNG to inspect")
    ap.add_argument("--expect-layout", help="Fail non-zero if tnt_oi_iv_layout != this")
    args = ap.parse_args()

    path = (args.file or "").strip()
    if not os.path.exists(path):
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2

    with open(path, "rb") as f:
        png = f.read()

    meta = read_png_text_metadata(png)
    layout = (meta.get("tnt_oi_iv_layout") or "").strip() or None
    build = (meta.get("tnt_build") or "").strip() or None
    tag = (meta.get("tnt_render_tag") or "").strip() or None

    print("tnt_oi_iv_layout", layout)
    print("tnt_build", build)
    print("tnt_render_tag", tag)
    print("meta_keys", sorted(list(meta.keys()))[:50])

    exp = (args.expect_layout or "").strip()
    if exp and (layout != exp):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

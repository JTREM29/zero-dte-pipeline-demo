import socket

def probe(cmd: str) -> str:
    with socket.create_connection(("127.0.0.1", 9100), timeout=3) as sock:
        sock.settimeout(2.0)
        sock.sendall(cmd.encode("ascii") + b"\r\n")
        chunks = []
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if b"!ENDMSG!" in chunk:
                break
    return b"".join(chunks).decode("latin-1", errors="replace")

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "SML"
    try:
        result = probe(cmd)
    except Exception as exc:
        print(f"ERROR: {exc!r}")
    else:
        with open("tmp_smiq_output.txt", "w", encoding="utf-8") as fh:
            fh.write(result)
        print(f"wrote tmp_smiq_output.txt for command {cmd}")

"""Scaffold for real IQFeed option chain & greeks integration.

This module does NOT fully implement the IQFeed derivatives protocol yet; it:
  - Opens a transient socket to the lookup/derivative port.
  - Sends basic protocol negotiation.
  - Provides placeholders for OPTION CHAIN (OC) and GREATS / REQSYMBOLS style requests.

Future work:
  - Implement paging / end-of-message markers (e.g. !ENDMSG!, E,!ERROR! handling).
  - Parse official field layout for greeks feed (requesting specific fieldsets).
  - Introduce async streaming greeks (watch + unwatch) and delta hedging hooks.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Callable, Iterable
import socket
import time

from .iqfeed_client import IQFeedConfig
from .options_chain import parse_occ_symbol, OptionContract
from ..utils.logging import get_logger


@dataclass
class RawOptionRecord:
    line: str
    parsed: Dict[str, Any] | None


class IQFeedOptionChainClient:
    def __init__(self, cfg: IQFeedConfig):
        self.cfg = cfg
        self.log = get_logger("iqfeed.options.real")

    def _open(self, port: int | None = None) -> socket.socket:
        p = port if port is not None else self.cfg.port_lookup
        s = socket.create_connection((self.cfg.host, p), timeout=self.cfg.timeout)
        s.settimeout(self.cfg.timeout)
        try:
            s.sendall(b"S,SET PROTOCOL,6.2\r\n")
        except Exception:  # noqa: BLE001
            pass
        return s

    @staticmethod
    def parse_chain_line(line: str) -> Optional[str]:
        """Heuristic extraction of an OCC symbol from a raw chain line.

        Looks for first token matching the OCC pattern used elsewhere (root+YYMMDD+CP+strike).
        Returns the symbol string if found, else None.
        """
        parts = [p for p in line.replace(" ", ",").split(',') if p]
        for p in parts:
            if len(p) >= 20 and p.isalnum():  # rough filter, final validation via parse_occ_symbol
                if parse_occ_symbol(p):  # reuse existing validator
                    return p
        return None

    def request_chain(self, root: str, month_codes: str = "", year: str = "") -> List[str]:  # pragma: no cover - network
        """Fetch raw chain symbols for a root with multiple fallbacks.

        Attempts the lookup port first (cfg.port_lookup), then falls back to the level1 port
        (cfg.port_level1) in case the IQFeed client is configured with a combined port.
        Uses CRLF line endings and waits for !ENDMSG!.
        """
        attempts: list[tuple[int, list[bytes]]] = []
        try:
            # Build command variants: include explicit pc (both puts/calls) and bare root
            cmd_variants = [
                f"OCH,{root},pc\r\n".encode("utf-8"),
                f"OCH,{root}\r\n".encode("utf-8"),
            ]
            ports = [self.cfg.port_lookup]
            if self.cfg.port_level1 not in ports:
                ports.append(self.cfg.port_level1)

            collected: list[str] = []
            for p in ports:
                try:
                    with self._open(p) as s:
                        for cmd in cmd_variants:
                            try:
                                s.sendall(cmd)
                                raw = self._collect_until_end(s)
                                if raw:
                                    collected = raw
                                    break
                            except Exception:
                                continue
                    if collected:
                        break
                except Exception:  # pragma: no cover - network
                    continue

            symbols: List[str] = []
            for line in collected:
                if line.startswith("!END") or line.startswith("!ENDMSG"):
                    break
                sym = self.parse_chain_line(line)
                if sym:
                    symbols.append(sym)
            # Deduplicate while preserving order
            seen = set()
            deduped: List[str] = []
            for s in symbols:
                if s not in seen:
                    seen.add(s)
                    deduped.append(s)
            return deduped
        except Exception as exc:  # noqa: BLE001
            self.log.warning("request_chain failed: %s", exc)
            return []

    def request_greeks_snapshot(self, symbols: List[str]) -> List[RawOptionRecord]:  # pragma: no cover - network
        """Placeholder for batched greeks snapshot.

        Would issue a series of requests or a multi-symbol request if supported.
        Parsing left minimal; map each line to RawOptionRecord.
        """
        out: List[RawOptionRecord] = []
        if not symbols:
            return out
        try:
            with self._open() as s:
                for sym in symbols:
                    # Placeholder: real protocol might use REQ option symbol command.
                    cmd = f"w{sym}\n".encode("utf-8")
                    s.sendall(cmd)
                    time.sleep(0.05)
                    # collect a short burst
                    lines = self._collect_until_silence(s, silence=0.15, max_lines=200)
                    for ln in lines:
                        out.append(RawOptionRecord(line=ln, parsed=self.parse_greeks_line(ln)))
        except Exception as exc:  # noqa: BLE001
            self.log.warning("request_greeks_snapshot failed: %s", exc)
        return out

    # ---- socket collection helpers (used by both chain & greeks snapshots) ----
    def _collect_until_end(self, sock: socket.socket, end_marker: str = "!ENDMSG!") -> List[str]:
        lines: List[str] = []
        start = time.time()
        buf = b""
        while (time.time() - start) < self.cfg.timeout:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        lines.append(text)
                        if text.startswith(end_marker):
                            return lines
            except socket.timeout:
                break
            except Exception:
                break
        return lines

    def _collect_until_silence(self, sock: socket.socket, silence: float, max_lines: int) -> List[str]:
        lines: List[str] = []
        buf = b""
        last = time.time()
        while (time.time() - last) < silence and len(lines) < max_lines:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    time.sleep(0.02)
                    continue
                last = time.time()
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        lines.append(text)
            except socket.timeout:
                break
            except Exception:
                break
        return lines

    # ---- parsing helpers ----
    @staticmethod
    def parse_greeks_line(line: str) -> Optional[Dict[str, Any]]:
        """Very rough placeholder greeks parser.

        If a line resembles a Level1 'Q,' message and contains an OCC option symbol it will:
          - parse via parse_level1_line
          - attach dummy greeks (delta/gamma/theta/vega) derived from mid & strike heuristic
        This is purely for scaffolding tests; real implementation should request the proper
        option greeks fieldset from IQFeed.
        """
        if not line or not line.startswith("Q,"):
            return None
        try:
            from .iqfeed_client import parse_level1_line  # local import
            parsed = parse_level1_line(line)
            if not parsed:
                return None
            sym = parsed.get("symbol")
            if not isinstance(sym, str) or not parse_occ_symbol(sym or ""):
                return None
            # Derive dummy greeks
            last = parsed.get("last_trade") or parsed.get("last_price") or parsed.get("mid")
            strike = None
            c = parse_occ_symbol(sym)
            if c:
                strike = c.strike
            if isinstance(last, (int, float)) and strike:
                moneyness = (last - strike) / strike if strike else 0.0
            else:
                moneyness = 0.0
            delta = max(-1.0, min(1.0, 0.5 + moneyness * 10))  # crude mapping
            gamma = 0.01 * (1 - abs(delta))
            theta = -0.02 * (1 - abs(moneyness))
            vega = 0.10 * (1 - abs(delta))
            parsed.update({
                "delta": float(delta),
                "gamma": float(gamma),
                "theta": float(theta),
                "vega": float(vega),
            })
            return parsed
        except Exception:
            return None


class IQFeedOptionGreeksStream:
    """Scaffold streaming greeks (wraps Level1-like polling with heuristic greeks).

    This class periodically invokes `request_greeks_snapshot` for a subset of symbols
    and pushes parsed records to a user callback. Not a real streaming greeks feed
    but a bridge until proper protocol implementation.
    """

    def __init__(self, chain_client: IQFeedOptionChainClient, symbols: Iterable[str], interval: float = 5.0,
                 on_record: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.client = chain_client
        self.symbols = list(symbols)
        self.interval = interval
        self.on_record = on_record
        self._stop = False
        self.log = get_logger("iqfeed.greeks.stream")

    def run(self, duration: float = 30.0):  # pragma: no cover - timing
        start = time.time()
        while (time.time() - start) < duration and not self._stop:
            recs = self.client.request_greeks_snapshot(self.symbols)
            for r in recs:
                if r.parsed and self.on_record:
                    try:
                        self.on_record(r.parsed)
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(self.interval)

    def stop(self):  # pragma: no cover
        self._stop = True

    # ---- helpers ----


__all__ = [
    "IQFeedOptionChainClient",
    "RawOptionRecord",
    "IQFeedOptionGreeksStream",
]
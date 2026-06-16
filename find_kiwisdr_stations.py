"""
Narzedzie do znajdowania dostepnych stacji KiwiSDR z wolnymi kanalami.
Uzycie: python find_kiwisdr_stations.py [liczba_stacji] [poczatek_zakresu] [koniec_zakresu]

Domyslnie: szuka 3 stacji wsrod proxy kiwisdr.com (zakres 22100-22400)
           plus kilka znanych adresow bezposrednich.
"""

import asyncio
import socket
import sys
import time


KNOWN_HOSTS = [
    "kiwisdr-dorohoi.ddns.net",
]

PROXY_RANGE_START = 22100
PROXY_RANGE_END   = 22400


async def _check_station(host: str, port: int = 8073, timeout: float = 5.0):
    """Sprawdza czy stacja jest dostepna i ma wolne kanaly.
    Zwraca (host, free_channels) lub None."""
    try:
        socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return None
    try:
        import websockets
        uri = f"ws://{host}:{port}/{int(time.time()*1000)}/SND"
        async with websockets.connect(
            uri,
            additional_headers={"Origin": f"http://{host}:{port}"},
            max_size=2**22,
            ping_interval=None,
            open_timeout=timeout,
        ) as ws:
            await ws.send("SET auth t=kiwi p=")
            info: dict = {}
            async for msg in ws:
                if not isinstance(msg, bytes):
                    continue
                if msg[:3] == b"MSG":
                    text = msg[4:].decode("utf-8", errors="replace")
                    for part in text.split():
                        if "=" in part:
                            k, v = part.split("=", 1)
                            info[k] = v
                    if "cfg_loaded" in text:
                        break
                if len(info) > 30:
                    break
            free = int(info.get("chan_no_pwd", -1))
            if free > 0:
                return (host, free)
            return None
    except Exception:
        return None


async def find_stations(want: int = 3, proxy_start: int = PROXY_RANGE_START,
                        proxy_end: int = PROXY_RANGE_END) -> list[str]:
    found: list[str] = []

    # Najpierw sprawdz znane hosty
    for h in KNOWN_HOSTS:
        r = await _check_station(h)
        if r:
            print(f"  OK  {r[0]}  wolne={r[1]}")
            found.append(r[0])
        if len(found) >= want:
            return found

    # Nastepnie skanuj proxies rownolegle
    proxies = [f"{n}.proxy.kiwisdr.com" for n in range(proxy_start, proxy_end)]
    sem = asyncio.Semaphore(20)

    async def guarded(h: str):
        async with sem:
            return await _check_station(h)

    for coro in asyncio.as_completed([guarded(h) for h in proxies]):
        r = await coro
        if r and r[0] not in found:
            print(f"  OK  {r[0]}  wolne={r[1]}")
            found.append(r[0])
        if len(found) >= want:
            break

    return found


def main():
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    pstart = int(sys.argv[2]) if len(sys.argv) > 2 else PROXY_RANGE_START
    pend   = int(sys.argv[3]) if len(sys.argv) > 3 else PROXY_RANGE_END

    print(f"Szukam {want} dzialajacych stacji KiwiSDR...\n")
    found = asyncio.run(find_stations(want, pstart, pend))

    print(f"\n=== Znaleziono {len(found)} stacji ===")
    for f in found:
        print(f"  {f}")

    if found:
        csv = ",".join(found)
        print(f"\nhosts_csv dla bloku GRC:\n  \"{csv}\"")

    return found


if __name__ == "__main__":
    main()

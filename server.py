#!/usr/bin/env python3
"""
server.py - serves a game folder made by builder.py.

    python server.py <out>                  the menu, then the game: http://localhost:8080/
    python server.py <out> match.json       starts that match at once (<address>/?menu opens the menu)
    --port N        the game server's port (8080)
    --net-port N    the network server's port (8081); the menu can change it
    --share         other devices on the network can open the game too (http://<this PC>:8080/),
                    and the network server opens at once
    python server.py <out> --list weapons [filter]
                    items | weapons | ranged | magic | helms | armors | skeletons | templates | perks |
                    tactics | locations | music | avatars | names | battles
    python server.py <out> --list battles MINIBOSS_CYPHER   one fight found: also prints its match.json

The game server sends the game files, match.js (the match given here, or none) and prints the game's
errors. It listens on this PC only, or on every interface with --share.
The network server joins two players on two devices. The host's menu opens it; the other player enters
the host's address. Only the inputs travel; both devices run the same simulation.
match.json and the controls: see builder.py.
"""
import argparse
import base64
import functools
import glob
import hashlib
import http.server
import json
import mimetypes
import os
import random
import socket
import struct
import sys
import threading

# =============================================================================== --list
ITEM_GROUPS = {"weapons": "Weapon", "ranged": "Ranged", "magic": "Magic", "helms": "Helm",
               "armors": "Armor", "skeletons": "Skeleton"}
LISTS = ("items", *ITEM_GROUPS, "templates", "perks", "tactics", "locations", "music", "avatars", "names", "battles")


def print_list(out, what, flt):
    """prints a list from sf2list.json (written by builder.py), only the rows that contain the filter"""
    data = json.load(open(os.path.join(out, "sf2list.json"), encoding="utf-8"))
    if what not in LISTS:
        sys.exit("--list " + "|".join(LISTS))
    lst = data["items" if what in ITEM_GROUPS else what]
    rows = [r for r in lst["rows"] if what not in ITEM_GROUPS or r[0] == ITEM_GROUPS[what]]
    flt = (flt or "").lower()
    found = [i for i, r in enumerate(rows) if not flt or flt in " ".join(r).lower()]
    table = ([lst["head"]] if lst["head"] else []) + [rows[i] for i in found]
    w = [max(len(r[i]) for r in table) for i in range(len(table[0]) - 1)] if table else []
    for r in table:
        print(("  ".join(f"{c:{w[i]}}" for i, c in enumerate(r[:-1])) + "  " + r[-1]).strip())
    if what != "battles":
        return
    if len(found) != 1:
        if found:
            print(f"\n{len(found)} fights. Give a filter that matches one (e.g. its FIGHT id) to get it as match.json.")
        return
    i = found[0]
    print('\nAs match.json (you are the player, with what you wear in the dojo; "left": {"items": [...]} '
          'puts other items on):\n')
    print(json.dumps(lst["json"][i], indent=2))
    for n in lst["notes"][i]:
        print("note:", n)
    if lst.get("details"):                          # its rules (raw XML) and opponents (template + fight perks)
        print()
        for d in lst["details"][i]:
            print(d)


# =============================================================================== the network server (WebSocket)
class Room:
    """Two seats: 0 is the host (left side), 1 is the player who joined (right side). A message goes to
    the other seat. Before the match the two menus talk in the lobby; the host's last lobby message is
    kept for a player who joins later."""
    def __init__(self):
        self.lock = threading.Lock()
        self.seats = [None, None]
        self.lobby = [None, None]
        self.lobby_last = None
        self.seed = 1

    def join(self, peer, seat):
        """takes a seat; a reconnect to the same seat (after a reload) replaces the old connection"""
        with self.lock:
            if self.seats == [None, None]:
                self.seed = random.randrange(1, 2**31 - 1)
            old, self.seats[seat] = self.seats[seat], peer
            return old, self.seed, self.seats[1 - seat]

    def leave(self, peer, seat):
        with self.lock:
            if self.seats[seat] is not peer:    # already replaced by a reconnect
                return None, False
            self.seats[seat] = None
            return self.seats[1 - seat], True


ROOM = Room()


class WsPeer:
    def __init__(self, sock, rfile, addr):
        self.sock, self.rfile, self.addr = sock, rfile, addr
        self.wlock = threading.Lock()

    def send(self, data, op=1):
        data = data.encode("utf-8") if isinstance(data, str) else data
        n = len(data)
        hdr = bytes([0x80 | op, n]) if n < 126 else bytes([0x80 | op, 126]) + struct.pack(">H", n)
        try:
            with self.wlock:
                self.sock.sendall(hdr + data)
        except OSError:
            pass

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def recv(self):
        h = self.rfile.read(2)
        if len(h) < 2:
            return None, None
        op, ln = h[0] & 15, h[1] & 127
        if ln == 126:
            ln = struct.unpack(">H", self.rfile.read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self.rfile.read(8))[0]
        mask = self.rfile.read(4) if h[1] & 128 else None
        data = self.rfile.read(ln)
        if mask:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        return op, data


# =============================================================================== HTTP server
for ext, mt in {".dds": "application/octet-stream", ".dat": "application/octet-stream", ".m4a": "audio/mp4",
                ".js": "text/javascript", ".json": "application/json", ".xml": "application/xml"}.items():
    mimetypes.add_type(mt, ext)


# names browsers ask for without reading the page (Safari: home screen, share sheet) -> the build's file
DEFAULT_ICONS = {"/apple-touch-icon.png": "apple-touch-icon.*.png",
                 "/apple-touch-icon-precomposed.png": "apple-touch-icon.*.png", "/favicon.ico": "favicon.*.ico"}


class Server(http.server.ThreadingHTTPServer):
    """A device that leaves (a page closed, a phone locked, a download cancelled) drops its connection:
    normal, not an error to print"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


class Handler(http.server.SimpleHTTPRequestHandler):
    match = {"config": None}       # match.json given on the command line (None: the menu first)

    def end_headers(self):
        # the game's files: kept by the browser and asked again each time ("no-cache"): unchanged ones come back
        # as 304 (by date, If-Modified-Since), so a page load no longer downloads everything again (one
        # texture file is 40 MB). What is made per request (the match, the port, POST answers): never kept
        path = self.path.split("?", 1)[0]
        dynamic = self.command != "GET" or path in ("/match.js", "/sf2-netport")
        self.send_header("Cache-Control", "no-store" if dynamic else "no-cache")
        super().end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/match.js":                  # the match for the browser (window.__SF2_CONFIG)
            m = Handler.match
            return self.reply(f"window.__SF2_CONFIG={json.dumps(m['config'])};\n".encode(), "text/javascript")
        if path == "/sf2-netport":               # the port the menu's Host suggests (--net-port)
            return self.reply(json.dumps({"port": NETWORK["port"]}).encode(), "application/json")
        if path in DEFAULT_ICONS:                # asked by iPhones/browsers by these names, whatever the page says
            found = sorted(glob.glob(os.path.join(self.directory, DEFAULT_ICONS[path])))
            if found:
                self.path = "/" + os.path.basename(found[0])
        super().do_GET()

    def reply(self, data, mime):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/sf2-host":     # the menu hosts: open the network server
            n = int(self.headers.get("Content-Length") or 0)
            try:
                port = int(json.loads(self.rfile.read(min(n, 1000)) or b"{}").get("port") or NETWORK["port"])
                port = start_network_server(port)
                r = {"port": port, "lan": f"{lan_ip()}:{port}"}
            except ValueError as e:
                r = {"error": str(e)}
            except OSError as e:
                r = {"error": f"port {port}: {e.strerror} - choose another"}
            return self.reply(json.dumps(r).encode(), "application/json")
        if self.path.split("?", 1)[0] == "/sf2-unhost":   # the host left its screens: close it
            stop_network_server()
            self.send_response(204)
            self.end_headers()
            return
        if self.path.split("?", 1)[0] == "/sf2-log":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                o = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
                print(f"[game-{o.get('kind') or 'error'}] {self.client_address[0]}: {o.get('msg')}"
                      + (f"\n             at {o.get('where')}" if o.get("where") else ""))
            except ValueError:
                pass
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        if " 404 " in (fmt % args):
            sys.stderr.write("[serve] " + (fmt % args) + "\n")


class NetworkHandler(http.server.BaseHTTPRequestHandler):
    """WebSocket /sf2-lan?role=...: host or join for the games, lobby-host or lobby-join for the menus"""
    def do_GET(self):
        if self.path.startswith("/sf2-lan") and self.headers.get("Upgrade", "").lower() == "websocket":
            return self.lan_session()
        self.send_error(404)

    def lan_session(self):
        key = self.headers.get("Sec-WebSocket-Key", "") + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        # The handshake must say HTTP/1.1 (http.server says 1.0). Chrome accepts 1.0, Safari does not.
        self.protocol_version = "HTTP/1.1"
        self.send_response_only(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
        http.server.BaseHTTPRequestHandler.end_headers(self)
        self.wfile.flush()
        self.close_connection = True
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        me = WsPeer(self.connection, self.rfile, self.client_address[0])
        role = (self.path.split("role=", 1) + [""])[1].split("&")[0]
        if role in ("lobby-host", "lobby-join"):     # the menus, before the games
            return self.lobby(me, role == "lobby-join")
        seat = 1 if role == "join" else 0
        old, seed, other = ROOM.join(me, seat)
        if old:
            old.close()
        side = ("LEFT", "RIGHT")[seat]
        print(f"[lan] {me.addr} joined as {side}" + ("" if other else " - waiting for the other player"))
        print(f"[lan]   {side} runs {self.headers.get('User-Agent', '?')}")   # both sides must compute the same results
        me.send(json.dumps({"t": "hello", "side": seat, "seed": seed, "peer": other is not None}))
        if other and not old:
            other.send(json.dumps({"t": "peer", "on": True}))
        try:
            while True:
                op, data = me.recv()
                if op is None or op == 8:
                    break
                if op == 9:
                    me.send(data, op=10)                       # pong
                elif op == 1:
                    if data[:12].startswith(b'{"t":"go"'):            # a player pressed Fight: print the items
                        try:
                            items = json.loads(data)["items"]
                            print(f"[lan] {side} fights with: "
                                  + (" ".join(n for n in items if n) if items else "its champion's own items"))
                        except (ValueError, KeyError, TypeError):
                            pass
                    o = ROOM.seats[1 - seat]
                    if o:
                        o.send(data)
        except (OSError, ValueError):
            pass
        finally:
            o, left = ROOM.leave(me, seat)
            if left:
                print(f"[lan] {me.addr} ({side}) left")
                o and o.send(json.dumps({"t": "peer", "on": False}))

    def lobby(self, me, i):
        """The menus before the match. The host sends "setup" (the match without the other side) and
        later "cfg" (the whole match); the other player sends "pick" (its side)."""
        who = ("the host", "the other player")[i]
        with ROOM.lock:
            old, ROOM.lobby[i] = ROOM.lobby[i], me
        old and old.close()
        print(f"[lan] {me.addr} ({who}) is in the menu" + (", waiting for the host's match" if i else ""))
        if i and ROOM.lobby_last:
            me.send(ROOM.lobby_last)
        try:
            while True:
                op, data = me.recv()
                if op is None or op == 8:
                    break
                if op == 9:
                    me.send(data, op=10)
                    continue
                if op != 1:
                    continue
                try:
                    t = json.loads(data).get("t")
                except ValueError:
                    continue
                if i == 0:                               # kept for a player who joins later
                    ROOM.lobby_last = data if t == "setup" else None   # (only while the match is open)
                print({"setup": "[lan]   the host chose the match; the other player picks a fighter",
                       "pick": "[lan]   the other player picked: " + data[:200].decode("utf-8", "replace"),
                       "cfg": "[lan]   the match: " + data[:300].decode("utf-8", "replace")}.get(t, f"[lan]   {t}"))
                o = ROOM.lobby[1 - i]
                o and o.send(data)
        except OSError:
            pass
        finally:
            with ROOM.lock:
                if ROOM.lobby[i] is me:
                    ROOM.lobby[i] = None

    def log_message(self, fmt, *args):
        pass


NETWORK = {"server": None, "port": 8081}


def start_network_server(port=None):
    """Opens the network server on every interface (the host's menu asks with POST /sf2-host).
    Only one runs: a new port closes the old one. Raises OSError when the port is taken."""
    port = port or NETWORK["port"]
    if NETWORK["server"] is not None and NETWORK["port"] != port:
        NETWORK["server"].shutdown()
        NETWORK["server"].server_close()
        NETWORK["server"] = None
    if NETWORK["server"] is None:
        srv = Server(("0.0.0.0", port), NetworkHandler)
        NETWORK["server"], NETWORK["port"] = srv, port
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[net]   network server open: others join with {lan_ip()}:{NETWORK['port']} "
              "(from outside this network: the public IP, with the port forwarded)")
        print("[net]   Windows may ask to allow Python through the firewall")
    return NETWORK["port"]


def stop_network_server():
    if NETWORK["server"] is not None:
        NETWORK["server"].shutdown()
        NETWORK["server"].server_close()
        NETWORK["server"] = None
        with ROOM.lock:
            ROOM.lobby_last = None
        print("[net]   network server closed (the host left its match screens)")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))                       # no packet is sent; picks the interface
        return s.getsockname()[0]
    except OSError:
        return "<this-pc-ip>"



def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", help="the game folder builder.py made")
    ap.add_argument("config", nargs="?", help="match JSON (only the settings you change)")
    ap.add_argument("--port", type=int, default=8080, help="the local server (this PC: game and menu)")
    ap.add_argument("--net-port", type=int, default=8081, help="the network server (opened by the menu's Host)")
    ap.add_argument("--share", action="store_true", help="the game also for other devices, network server open")
    ap.add_argument("--list", nargs="+", metavar=("WHAT", "FILTER"))
    a = ap.parse_args(argv)
    out = os.path.abspath(a.out)
    if not os.path.exists(os.path.join(out, "sf2list.json")):
        sys.exit(f"{out}: not a game builder.py made (python builder.py <www> -o {a.out})")
    if a.list:
        return print_list(out, a.list[0], a.list[1] if len(a.list) > 1 else None)
    NETWORK["port"] = a.net_port
    user = None                               # None: the menu first
    if a.config:
        try:
            user = json.load(open(a.config, encoding="utf-8"))
        except (OSError, ValueError) as e:
            sys.exit(f"{a.config}: {e}")
        print(f"[match] {a.config}: the browser checks it; an error shows in its menu and here")
    else:
        print("[match] none given: the menu comes first")
    Handler.match = {"config": user}
    try:
        srv = Server(("0.0.0.0" if a.share else "127.0.0.1", a.port),
                                              functools.partial(Handler, directory=out))
    except OSError as e:
        sys.exit(f"port {a.port}: {e.strerror} - choose another with --port")
    print(f"[serve] http://localhost:{a.port}/")
    if a.share:
        try:
            start_network_server()
        except OSError as e:
            sys.exit(f"network port {NETWORK['port']}: {e.strerror} - choose another with --net-port")
        print(f"[serve] other devices open http://{lan_ip()}:{a.port}/ - in its menu: Two players, network")
    else:
        print("[serve] two players on two devices: the menu's Host opens the network server")
    with srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

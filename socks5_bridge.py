"""Minimal local HTTP CONNECT proxy that tunnels through a SOCKS5 upstream.

cpolar (Go) only honors HTTP_PROXY (http/https URLs) and ignores SOCKS5.
This bridge lets cpolar reach the internet via the *stable* SOCKS5 proxy at
127.0.0.1:7892 (Clash), which the HTTP proxy on the same port cannot do reliably.

Usage:
    python socks5_bridge.py            # listens on 127.0.0.1:8899
Then run cpolar with:  HTTP_PROXY=http://127.0.0.1:8899 HTTPS_PROXY=http://127.0.0.1:8899
"""
import socket
import threading

LISTEN = ('127.0.0.1', 8899)
SOCKS5 = ('127.0.0.1', 7892)


def socks5_connect(host: str, port: int) -> socket.socket:
    s = socket.create_connection(SOCKS5, timeout=10)
    s.sendall(b'\x05\x01\x00')            # greeting: SOCKS5, no auth
    if s.recv(2)[0] != 5:
        raise RuntimeError('bad SOCKS5 greeting')
    # CONNECT command, domain name (FQDN) form
    msg = b'\x05\x01\x00\x03' + bytes([len(host)]) + host.encode() + port.to_bytes(2, 'big')
    s.sendall(msg)
    rep = s.recv(10)
    if rep[1] != 0:
        raise RuntimeError('SOCKS5 connect failed, code=%d' % rep[1])
    return s


def tunnel(a: socket.socket, b: socket.socket):
    def pipe(x, y):
        try:
            while True:
                data = x.recv(65536)
                if not data:
                    break
                y.sendall(data)
        except OSError:
            pass
        finally:
            try:
                y.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    t1 = threading.Thread(target=pipe, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pipe, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def handle(c: socket.socket):
    try:
        req = c.recv(8192)
        if not req:
            return
        first_line = req.split(b'\r\n', 1)[0].decode('latin1')
        method, target, _ = first_line.split(' ')
        if method == 'CONNECT':                       # HTTPS tunneling
            host, port = target.rsplit(':', 1)
            upstream = socks5_connect(host, int(port))
            c.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            tunnel(c, upstream)
        else:                                         # plain HTTP (rare for cpolar)
            rest = req
            host = None
            for line in req.split(b'\r\n'):
                if line.lower().startswith(b'host:'):
                    host = line.split(b':', 1)[1].strip().decode()
            if host:
                h, p = (host.rsplit(':', 1) + ['80'])[:2] if ':' in host else (host, '80')
                upstream = socks5_connect(h, int(p))
                upstream.sendall(rest)
                tunnel(c, upstream)
    except Exception as e:
        print('[bridge] handle error:', e)
    finally:
        c.close()


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(128)
    print('[bridge] listening on %s:%d -> socks5 %s:%d' % (LISTEN[0], LISTEN[1], SOCKS5[0], SOCKS5[1]))
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == '__main__':
    main()

"""Runs INSIDE the Assistant's sandbox (see sandbox.py), around every command.

The sandbox has no network of its own. This listens on 127.0.0.1:<port> inside it and passes each connection to the unix
socket of the app's proxy, which is the only way out and lets through only the hosts you approved. Then it runs the
command and exits with its exit code.

usage: sandbox_fwd.py <proxy socket> <port> <command> [args...]"""
import socket
import subprocess
import sys
import threading


def _pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _one(c, path):
    u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        u.connect(path)
    except OSError:
        c.close()
        return
    t = threading.Thread(target=_pipe, args=(u, c), daemon=True)
    t.start()
    _pipe(c, u)
    t.join()
    c.close()
    u.close()


def _serve(s, path):
    while True:
        try:
            c, _ = s.accept()
        except OSError:
            return
        threading.Thread(target=_one, args=(c, path), daemon=True).start()


def main():
    path, port, cmd = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(64)
        threading.Thread(target=_serve, args=(s, path), daemon=True).start()
    except OSError as e:   # the command still runs, with no way out, and its output says why
        print(f"sandbox: the way to the approved hosts could not be set up ({e}); this command has no internet", flush=True)
    r = subprocess.call(cmd)
    sys.exit(128 - r if r < 0 else r)


if __name__ == "__main__":
    main()

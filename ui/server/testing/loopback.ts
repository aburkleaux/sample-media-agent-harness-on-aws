/**
 * Test-only: start an HTTP server on the LOOPBACK address the test then dials.
 *
 * Why not `server.listen(0)`: with no host, Node binds the dual-stack wildcard `[::]`, and the tests
 * dial `http://127.0.0.1:<port>`. On macOS another process's IPv4 wildcard listener (`*:<port>`,
 * e.g. Logitech Options on `*:59869`, bound with port reuse) can hold the same port number, and an
 * IPv4 connection to 127.0.0.1 is delivered to THAT listener, not to the test's IPv6 socket. Seen
 * under three concurrent vitest shards, where port churn walks the allocator across the whole
 * ephemeral range: `fetch failed / SocketError: other side closed` with `remotePort: 59869`, and a
 * stray `400` from whatever answered. Binding `127.0.0.1` explicitly is the most specific match, so
 * the kernel delivers 127.0.0.1 traffic to the test's server even when a wildcard listener shares
 * the port.
 *
 * A host makes `listen` resolve it asynchronously, so `address()` is only valid after `listening`;
 * hence this is async.
 */
import http from "http";
import type { AddressInfo } from "net";

export const LOOPBACK_HOST = "127.0.0.1";

type Listenable = http.RequestListener | http.Server;

/** Listen on 127.0.0.1:0, record the server in `servers` (for afterEach), and return its base URL. */
export async function listenLoopback(target: Listenable, servers?: http.Server[]): Promise<string> {
  const server = target instanceof http.Server ? target : http.createServer(target);
  servers?.push(server);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, LOOPBACK_HOST, () => {
      server.off("error", reject);
      resolve();
    });
  });
  return `http://${LOOPBACK_HOST}:${(server.address() as AddressInfo).port}`;
}

/**
 * Close every recorded server, dropping its open connections so nothing (a keep-alive socket, an
 * unread response body) outlives the test.
 */
export async function closeServers(servers: http.Server[]): Promise<void> {
  const all = servers.splice(0);
  await Promise.all(all.map((s) => new Promise<void>((resolve) => {
    s.closeAllConnections?.();
    if (!s.listening) { resolve(); return; }
    s.close(() => resolve());
  })));
}

#pragma once
// ============================================================
//  net.hpp — tiny localhost TCP transport for the TX/RX demo.
//  This is the "channel wire": TX (client) streams complex
//  baseband symbols to RX (server). The RX then adds AWGN +
//  a carrier frequency/phase offset before running the real
//  receive DSP. POSIX sockets (Linux/macOS). Same-arch,
//  same-endianness assumed (localhost), so bytes are raw.
// ============================================================
#include <vector>
#include <string>
#include <complex>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <fcntl.h>
#include <cerrno>
#include <cstring>

namespace net {

inline void send_all(int fd, const void* buf, size_t n) {
    const char* p = static_cast<const char*>(buf);
    size_t sent = 0;
    while (sent < n) {
        ssize_t r = ::send(fd, p + sent, n - sent, 0);
        if (r <= 0) throw std::runtime_error("send failed / peer closed");
        sent += static_cast<size_t>(r);
    }
}
inline void recv_all(int fd, void* buf, size_t n) {
    char* p = static_cast<char*>(buf);
    size_t got = 0;
    while (got < n) {
        ssize_t r = ::recv(fd, p + got, n - got, 0);
        if (r <= 0) throw std::runtime_error("recv failed / peer closed");
        got += static_cast<size_t>(r);
    }
}

inline void  send_i32(int fd, int32_t v)            { send_all(fd, &v, sizeof(v)); }
inline int32_t recv_i32(int fd) { int32_t v; recv_all(fd, &v, sizeof(v)); return v; }

// Non-blocking read of up to n bytes. Returns the count read (0 if nothing is
// available right now). Used to poll for ACKs without blocking the ARQ loop.
inline int recv_avail(int fd, void* buf, size_t n) {
    ssize_t r = ::recv(fd, buf, n, MSG_DONTWAIT);
    return (r > 0) ? static_cast<int>(r) : 0;
}

inline void send_str(int fd, const std::string& s) {
    send_i32(fd, static_cast<int32_t>(s.size()));
    if (!s.empty()) send_all(fd, s.data(), s.size());
}
inline std::string recv_str(int fd) {
    int32_t n = recv_i32(fd);
    std::string s(static_cast<size_t>(n), '\0');
    if (n > 0) recv_all(fd, &s[0], static_cast<size_t>(n));
    return s;
}

inline void send_symbols(int fd, const std::vector<std::complex<float>>& v) {
    send_i32(fd, static_cast<int32_t>(v.size()));
    if (!v.empty()) send_all(fd, v.data(), v.size() * sizeof(std::complex<float>));
}
inline std::vector<std::complex<float>> recv_symbols(int fd) {
    int32_t n = recv_i32(fd);
    std::vector<std::complex<float>> v(static_cast<size_t>(n));
    if (n > 0) recv_all(fd, v.data(), v.size() * sizeof(std::complex<float>));
    return v;
}

// ── client: connect to host:port, return fd ─────────────────
// A BOUNDED connect. The blocking version waited the OS default — about two minutes on
// Linux — whenever packets were silently dropped, and the caller only prints "waiting
// for the ACK server" once a failure comes back. A wrong address therefore looked like
// a hang with no output at all. It also distinguishes the two failures, because they
// have opposite fixes: refused means nothing is listening there, timed out means the
// packets never arrive (a firewall, or no route between the two networks).
inline int connect_to(const std::string& host, int port, int timeout_ms = 3000) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) throw std::runtime_error("socket() failed");
    int one = 1; ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    sockaddr_in addr{}; addr.sin_family = AF_INET; addr.sin_port = htons(port);
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) <= 0) {
        ::close(fd);
        throw std::runtime_error("bad host: " + host);
    }
    const std::string where = host + ":" + std::to_string(port);
    int flags = ::fcntl(fd, F_GETFL, 0);
    ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    int rc = ::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
    if (rc < 0 && errno == EINPROGRESS) {
        pollfd pfd{fd, POLLOUT, 0};
        int pr = ::poll(&pfd, 1, timeout_ms);
        if (pr == 0) {
            ::close(fd);
            throw std::runtime_error("connect to " + where + " timed out — the packets are "
                                     "being DROPPED, not refused: a firewall, or no route "
                                     "between these two networks");
        }
        if (pr < 0) { ::close(fd); throw std::runtime_error("poll() failed"); }
        int err = 0; socklen_t len = sizeof(err);
        ::getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len);
        if (err) {
            ::close(fd);
            throw std::runtime_error("connect to " + where + " failed: " +
                                     std::string(std::strerror(err)) +
                                     (err == ECONNREFUSED
                                      ? " — the address is reachable but nothing is "
                                        "listening on that port (is the sink started, "
                                        "and is its port exposed on the host?)"
                                      : ""));
        }
    } else if (rc < 0) {
        int err = errno; ::close(fd);
        throw std::runtime_error("connect to " + where + " failed: " +
                                 std::string(std::strerror(err)));
    }
    ::fcntl(fd, F_SETFL, flags);          // back to blocking for normal use
    return fd;
}

// ── server: bind+listen on port, accept one client, return fd ─
inline int accept_one(int port) {
    int srv = ::socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) throw std::runtime_error("socket() failed");
    int one = 1; ::setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{}; addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY); addr.sin_port = htons(port);
    if (::bind(srv, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0)
        throw std::runtime_error("bind() failed (port in use?)");
    if (::listen(srv, 1) < 0) throw std::runtime_error("listen() failed");
    int fd = ::accept(srv, nullptr, nullptr);
    if (fd < 0) throw std::runtime_error("accept() failed");
    ::close(srv);
    return fd;
}

} // namespace net

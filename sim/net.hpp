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
inline int connect_to(const std::string& host, int port) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) throw std::runtime_error("socket() failed");
    int one = 1; ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    sockaddr_in addr{}; addr.sin_family = AF_INET; addr.sin_port = htons(port);
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) <= 0)
        throw std::runtime_error("bad host: " + host);
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0)
        throw std::runtime_error("connect() failed (is the RX listening?)");
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

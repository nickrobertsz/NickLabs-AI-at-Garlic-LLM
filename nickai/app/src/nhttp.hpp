// SPDX-License-Identifier: GPL-3.0-or-later
// Minimal HTTP/1.1 server for the NickLabs native PS4 app (header only, POSIX sockets, no exceptions escape threads).
// Usage: nhttp::Server s(11434, [](const nhttp::Request& q, nhttp::Responder& r){ r.send(200,"text/plain","ok"); });
//        std::string err; if(!s.start(err)) ...; ... s.stop();
#pragma once
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include "nclock.hpp"
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <sys/socket.h>
#include <unistd.h>
namespace nhttp {
// The console's SDK headers carry Linux values for MSG_NOSIGNAL (0x4000) and MSG_DONTWAIT (0x40) while its kernel is FreeBSD 9
// (there 0x4000 = MSG_NBIO and 0x40 = MSG_WAITALL): never pass them on the console. SIGPIPE is ignored process-wide instead.
#if defined(MSG_NOSIGNAL) && !defined(__ORBIS__)
constexpr int SEND_FLAGS = MSG_NOSIGNAL;
#else
constexpr int SEND_FLAGS = 0;
#endif
// Detached thread with an explicit stack (console default thread stacks are small).
inline bool spawn(std::function<void()> fn, size_t stack = 2u << 20) {
    auto* f = new std::function<void()>(std::move(fn));
    pthread_attr_t a; pthread_attr_init(&a); pthread_attr_setstacksize(&a, stack); pthread_attr_setdetachstate(&a, PTHREAD_CREATE_DETACHED);
    pthread_t t;
    int rc = pthread_create(&t, &a, [](void* p) -> void* { auto* g = static_cast<std::function<void()>*>(p); try { (*g)(); } catch (...) {} delete g; return nullptr; }, f);
    pthread_attr_destroy(&a);
    if (rc) { delete f; return false; }
    return true;
}
struct Request {
    std::string method, path, query, body, peer_ip;
    std::map<std::string, std::string> headers; // lowercase keys
    std::string header(const std::string& k) const { auto i = headers.find(k); return i == headers.end() ? std::string() : i->second; }
};
inline const char* reason(int s) {
    switch (s) {
    case 200: return "OK"; case 202: return "Accepted"; case 204: return "No Content"; case 400: return "Bad Request";
    case 401: return "Unauthorized"; case 403: return "Forbidden"; case 404: return "Not Found"; case 405: return "Method Not Allowed";
    case 408: return "Request Timeout"; case 409: return "Conflict"; case 413: return "Payload Too Large"; case 422: return "Unprocessable Entity";
    case 429: return "Too Many Requests"; case 499: return "Client Closed Request"; case 500: return "Internal Server Error"; case 501: return "Not Implemented";
    case 503: return "Service Unavailable"; default: return "Status";
    }
}
class Responder {
    int fd_;
    bool started_ = false, finished_ = false, broken_ = false;
    std::vector<std::pair<std::string, std::string>> extra_;
    bool raw(const char* p, size_t n) {
        if (broken_) return false;
        size_t off = 0;
        while (off < n) {
            pollfd pf{fd_, POLLOUT, 0};
            int r = ::poll(&pf, 1, 30000);
            if (r < 0 && errno == EINTR) continue;
            if (r <= 0 || (pf.revents & (POLLERR | POLLHUP | POLLNVAL))) { broken_ = true; return false; }
            ssize_t w = ::send(fd_, p + off, n - off, SEND_FLAGS);
            if (w < 0 && (errno == EINTR || errno == EAGAIN)) continue;
            if (w <= 0) { broken_ = true; return false; }
            off += size_t(w);
        }
        return true;
    }
    std::string head(int status, const std::string& ct, const std::string& framing) {
        std::string h = "HTTP/1.1 " + std::to_string(status) + " " + reason(status) + "\r\nContent-Type: " + ct + "\r\n" + framing +
                        "Connection: close\r\nAccess-Control-Allow-Origin: *\r\n";
        for (auto& e : extra_) h += e.first + ": " + e.second + "\r\n";
        return h + "\r\n";
    }
public:
    explicit Responder(int fd) : fd_(fd) {}
    void add_header(const std::string& k, const std::string& v) { extra_.emplace_back(k, v); }
    bool started() const { return started_; }
    // The client went away before any reply: nothing is sent and no error is synthesized (the connection is simply closed).
    void abandon() { started_ = finished_ = true; }
    bool send(int status, const std::string& ct, const std::string& body, bool head_only = false) {
        if (started_) return false;
        started_ = finished_ = true;
        std::string h = head(status, ct, "Content-Length: " + std::to_string(body.size()) + "\r\n");
        return raw(h.data(), h.size()) && (head_only || body.empty() || raw(body.data(), body.size()));
    }
    bool begin_stream(int status, const std::string& ct) {
        if (started_) return false;
        started_ = true;
        std::string h = head(status, ct, "Transfer-Encoding: chunked\r\nCache-Control: no-cache\r\n");
        return raw(h.data(), h.size());
    }
    bool write_chunk(const std::string& d) {
        if (!started_ || finished_) return false;
        if (d.empty()) return !broken_;
        char hex[24];
        int n = snprintf(hex, sizeof hex, "%zx\r\n", d.size());
        std::string o(hex, size_t(n));
        o += d; o += "\r\n";
        return raw(o.data(), o.size());
    }
    bool end_stream() {
        if (!started_ || finished_) return false;
        finished_ = true;
        return raw("0\r\n\r\n", 5);
    }
    bool peer_closed() {
        if (broken_) return true;
        pollfd pf{fd_, POLLIN, 0};
        if (::poll(&pf, 1, 0) <= 0) return false;
        if (pf.revents & (POLLERR | POLLHUP | POLLNVAL)) return true;
        char c;
        ssize_t r = ::recv(fd_, &c, 1, MSG_PEEK);   // poll said readable: this cannot block (0 = orderly close)
        return r == 0 || (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR);
    }
};
using Handler = std::function<void(const Request&, Responder&)>;
class Server {
    int port_, lfd_ = -1, bound_ = 0, max_conns_;
    size_t max_body_;
    Handler handler_;
    std::atomic<bool> acc_running_{false};
    std::atomic<bool> stop_{false};
    std::atomic<int> live_{0};
    std::atomic<unsigned long long> accepted_{0}, accept_errors_{0}, spawn_failures_{0}, rejected_{0};
    int last_accept_errno_ = 0;
    std::mutex mu_;
    std::condition_variable cv_;
    static double now_s() { return nclock::mono_s(); }
    static bool find_end(const std::string& b, size_t& pos) { pos = b.find("\r\n\r\n"); return pos != std::string::npos; }
    // Reads until pred satisfied; returns 0 ok, 408 timeout, 400 closed/err.
    static int fill(int fd, std::string& buf, size_t want_min, double deadline) {
        while (buf.size() < want_min) {
            double left = deadline - now_s();
            if (left <= 0) return 408;   // overall request deadline: a slow client cannot hold a connection slot forever
            pollfd pf{fd, POLLIN, 0};
            int r = ::poll(&pf, 1, int(std::min(15000.0, left * 1000.0) + 1));
            if (r < 0 && errno == EINTR) continue;
            if (r == 0) return 408;
            if (r < 0) return 400;
            char tmp[8192];
            ssize_t n = ::recv(fd, tmp, sizeof tmp, 0);
            if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
            if (n <= 0) return 400;
            buf.append(tmp, size_t(n));
        }
        return 0;
    }
    static std::string lower(std::string s) { for (auto& c : s) c = char(tolower((unsigned char)c)); return s; }
    static std::string trim(const std::string& s) {
        size_t a = 0, b = s.size();
        while (a < b && (s[a] == ' ' || s[a] == '\t')) a++;
        while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t')) b--;
        return s.substr(a, b - a);
    }
    // Half-close, then swallow whatever the client already sent: closing with unread bytes would RST and can destroy our reply.
    // (poll+plain recv only: the console SDK's MSG_DONTWAIT value is wrong for its kernel.)
    static void lingering_close(int fd) {
        ::shutdown(fd, SHUT_WR);
        char b[4096];
        for (int i = 0; i < 64; i++) {
            pollfd pf{fd, POLLIN, 0};
            if (::poll(&pf, 1, i < 4 ? 100 : 0) <= 0 || !(pf.revents & POLLIN)) break;
            if (::recv(fd, b, sizeof b, 0) <= 0) break;
        }
    }
    void serve(int fd, std::string ip) {
        Responder rsp(fd);
        auto err = [&](int code, const char* msg) { rsp.send(code, "application/json", std::string("{\"error\":\"") + msg + "\"}"); };
        try {
            std::string buf;
            size_t hend = 0;
            const double deadline = now_s() + 120;
            for (;;) {
                if (find_end(buf, hend)) break;
                if (buf.size() > 32768) { err(400, "header too large"); return; }
                int rc = fill(fd, buf, buf.size() + 1, deadline);
                if (rc) { if (!buf.empty()) err(rc, rc == 408 ? "timeout" : "bad request"); return; }
            }
            Request q;
            q.peer_ip = ip;
            std::string head = buf.substr(0, hend), rest = buf.substr(hend + 4);
            size_t le = head.find("\r\n");
            std::string line = head.substr(0, le);
            size_t s1 = line.find(' '), s2 = line.rfind(' ');
            if (s1 == std::string::npos || s2 == s1 || line.compare(s2 + 1, 5, "HTTP/") != 0) { err(400, "bad request line"); return; }
            q.method = line.substr(0, s1);
            if (q.method.empty() || q.method.size() > 16 || q.method.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZ") != std::string::npos) { err(400, "bad method"); return; }
            std::string target = line.substr(s1 + 1, s2 - s1 - 1);
            size_t qm = target.find('?');
            q.path = target.substr(0, qm);
            if (qm != std::string::npos) q.query = target.substr(qm + 1);
            if (q.path.empty() || q.path[0] != '/') { err(400, "bad target"); return; }
            size_t p = le == std::string::npos ? head.size() : le + 2;
            while (p < head.size()) {
                size_t e = head.find("\r\n", p);
                if (e == std::string::npos) e = head.size();
                std::string h = head.substr(p, e - p);
                p = e + 2;
                size_t c = h.find(':');
                if (c == std::string::npos || c == 0) { err(400, "bad header"); return; }
                std::string key = lower(h.substr(0, c)), val = trim(h.substr(c + 1));
                auto dup = q.headers.find(key);
                if (dup != q.headers.end() && (key == "content-length" || key == "host") && dup->second != val) { err(400, "conflicting duplicate header"); return; }
                q.headers[key] = val;
            }
            if (q.headers.count("transfer-encoding")) { err(501, "request transfer-encoding unsupported"); return; }
            size_t len = 0;
            auto cl = q.headers.find("content-length");
            if (cl != q.headers.end()) {
                if (cl->second.empty() || cl->second.size() > 12 || cl->second.find_first_not_of("0123456789") != std::string::npos) { err(400, "bad content-length"); return; }
                len = size_t(std::stoull(cl->second));
                if (len > max_body_) { err(413, "body too large"); return; }
            }
            if (q.headers.count("expect")) { const char* c100 = "HTTP/1.1 100 Continue\r\n\r\n"; (void)!::send(fd, c100, strlen(c100), 0); }
            if (rest.size() < len) { int rc = fill(fd, rest, len, deadline); if (rc) { err(rc == 408 ? 408 : 400, "incomplete body"); return; } }
            q.body = rest.substr(0, len);
            if (q.method == "OPTIONS") {
                std::string h = "HTTP/1.1 204 No Content\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, DELETE, HEAD, OPTIONS\r\n"
                                "Access-Control-Allow-Headers: *\r\nAccess-Control-Max-Age: 600\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
                (void)!::send(fd, h.data(), h.size(), 0);
                return;
            }
            handler_(q, rsp);
            if (!rsp.started()) err(500, "no response");
        } catch (...) {
            if (!rsp.started()) err(500, "internal");
        }
    }
public:
    std::function<void(const std::string&)> on_event;   // diagnostics (rate-limited): accept errors, thread-start failures, connection-limit rejects
    int live() const { return live_.load(); }
    struct Stats { int live, max_conns; unsigned long long accepted, accept_errors; int last_accept_errno; unsigned long long spawn_failures, rejected; };
    Stats stats() const { return {live_.load(), max_conns_, accepted_, accept_errors_, last_accept_errno_, spawn_failures_, rejected_}; }
    Server(int port, Handler h, size_t max_body = 8u << 20, int max_conns = 32) : port_(port), max_conns_(max_conns), max_body_(max_body), handler_(std::move(h)) {}
    ~Server() { stop(); }
    int port() const { return bound_; }
    bool start(std::string& err) {
        lfd_ = ::socket(AF_INET, SOCK_STREAM, 0);
        if (lfd_ < 0) { err = std::string("socket: ") + strerror(errno); return false; }
        int one = 1;
        setsockopt(lfd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
#ifdef SO_NOSIGPIPE
        setsockopt(lfd_, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
        sockaddr_in a{};
        a.sin_family = AF_INET; a.sin_addr.s_addr = htonl(INADDR_ANY); a.sin_port = htons(uint16_t(port_));
        if (::bind(lfd_, (sockaddr*)&a, sizeof a) < 0) { err = std::string("bind: ") + strerror(errno); ::close(lfd_); lfd_ = -1; return false; }
        if (::listen(lfd_, 16) < 0) { err = std::string("listen: ") + strerror(errno); ::close(lfd_); lfd_ = -1; return false; }
        socklen_t sl = sizeof a;
        getsockname(lfd_, (sockaddr*)&a, &sl);
        bound_ = ntohs(a.sin_port);
        acc_running_ = true;
        if (!spawn([this] { accept_loop(); acc_running_ = false; })) { acc_running_ = false; err = "cannot start accept thread"; ::close(lfd_); lfd_ = -1; return false; }
        return true;
    }
    void event(unsigned long long n, const std::string& m) { if (on_event && (n <= 10 || (n & (n - 1)) == 0)) on_event(m + " [#" + std::to_string(n) + "]"); }   // first 10, then powers of two
    void accept_loop() {
        while (!stop_) {
            pollfd pf{lfd_, POLLIN, 0};
            int r = ::poll(&pf, 1, 250);
            if (r <= 0) continue;
            sockaddr_in pa{};
            socklen_t pl = sizeof pa;
            int fd = ::accept(lfd_, (sockaddr*)&pa, &pl);
            if (fd < 0) { int e = errno; last_accept_errno_ = e; auto n = ++accept_errors_; event(n, "accept() failed: " + std::string(strerror(e)) + " (errno " + std::to_string(e) + ")"); if (e == EMFILE || e == ENFILE || e == ENOBUFS || e == ENOMEM) std::this_thread::sleep_for(std::chrono::milliseconds(100)); continue; }
            accepted_++;
#ifdef SO_NOSIGPIPE
            int one = 1; setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
            char ip[64] = "?";
            inet_ntop(AF_INET, &pa.sin_addr, ip, sizeof ip);
            if (live_.load() >= max_conns_) {
                event(++rejected_, "connection limit reached (" + std::to_string(live_.load()) + " live): rejecting " + ip);
                const char* m = "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 27\r\nConnection: close\r\n\r\n{\"error\":\"too many clients\"}";
                (void)!::send(fd, m, strlen(m), 0);
                lingering_close(fd);
                ::close(fd);
                continue;
            }
            live_++;
            bool ok = spawn([this, fd, ip = std::string(ip)] {
                serve(fd, ip);
                lingering_close(fd);
                ::close(fd);
                std::lock_guard<std::mutex> l(mu_);   // decrement AND notify under the lock: stop() must not destroy cv_ under us
                live_--;
                cv_.notify_all();
            });
            if (!ok) { ::close(fd); live_--; event(++spawn_failures_, "cannot start a thread for the connection from " + std::string(ip) + " (live " + std::to_string(live_.load()) + "): connection dropped"); }
        }
    }
    void stop() {
        if (stop_.exchange(true)) return;
        for (int i = 0; i < 400 && acc_running_; i++) std::this_thread::sleep_for(std::chrono::milliseconds(10));
        if (lfd_ >= 0) { ::close(lfd_); lfd_ = -1; }
        std::unique_lock<std::mutex> l(mu_);
        cv_.wait_for(l, std::chrono::seconds(35), [this] { return live_.load() == 0; });
    }
};
}

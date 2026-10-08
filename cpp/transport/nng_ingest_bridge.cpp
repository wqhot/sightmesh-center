// Standalone NNG REQ/REP -> loopback HTTP bridge. The Python DurableInbox is
// the ONE persistence authority. No ACK is synthesized in the NNG layer.
#include <sightmesh/wire/v1/sightmesh_wire.pb.h>

#include <curl/curl.h>
#include <nng/nng.h>
#include <nng/protocol/reqrep0/rep.h>

#include <atomic>
#include <cctype>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <sys/stat.h>

namespace {
volatile std::sig_atomic_t running = 1;
void onSignal(int) { running = 0; }
constexpr std::size_t kMaxRequestBytes = 20U * 1024U * 1024U;
constexpr std::size_t kMaxReplyBytes = 1U * 1024U * 1024U;
constexpr std::size_t kBatchMax = 4U * 1024U * 1024U;
constexpr std::size_t kBlobMax = 16U * 1024U * 1024U;
const std::string kEvents = "/api/v1/mtmct/events/batch";
const std::string kBlobs = "/api/v1/mtmct/blobs/";

struct Config {
    std::string listen = "tcp://127.0.0.1:19703";
    std::string token;
    int inbox_port = 18081;
};

bool matchesSecret(const std::string& left, const std::string& right) {
    // Compare fixed bytes (length differences do not early-return).
    const std::size_t n = left.size() > right.size() ? left.size() : right.size();
    unsigned diff = static_cast<unsigned>(left.size() ^ right.size());
    for (std::size_t i = 0; i < n; ++i) {
        const unsigned char a = i < left.size() ? static_cast<unsigned char>(left[i]) : 0;
        const unsigned char b = i < right.size() ? static_cast<unsigned char>(right[i]) : 0;
        diff |= static_cast<unsigned>(a ^ b);
    }
    return diff == 0;
}

bool validBlobPath(const std::string& service) {
    if (service.compare(0, kBlobs.size(), kBlobs) != 0 ||
        service.size() != kBlobs.size() + 64)
        return false;
    for (std::size_t i = kBlobs.size(); i < service.size(); ++i)
        if (!((service[i] >= '0' && service[i] <= '9') ||
              (service[i] >= 'a' && service[i] <= 'f')))
            return false;
    return true;
}

struct Output {
    std::string bytes;
    bool overflow = false;
};

std::size_t capture(char* ptr, std::size_t unit, std::size_t count, void* userdata) {
    auto* out = static_cast<Output*>(userdata);
    if (unit != 0 && count > std::numeric_limits<std::size_t>::max() / unit)
        return 0;
    const std::size_t n = unit * count;
    if (n > kMaxReplyBytes - out->bytes.size()) {
        out->overflow = true;
        return 0;
    }
    out->bytes.append(ptr, n);
    return n;
}

void forward(const Config& config,
             const sightmesh::wire::v1::ReliableRpcRequest& input,
             sightmesh::wire::v1::ReliableRpcReply* reply) {
    const bool is_event = input.service() == kEvents;
    const bool is_blob = validBlobPath(input.service());
    if ((!is_event && !is_blob) ||
        input.body().empty() ||
        input.body().size() > (is_event ? kBatchMax : kBlobMax)) {
        reply->set_status_code(400);
        reply->set_error("invalid legacy Inbox service or request size");
        return;
    }
    CURL* curl = curl_easy_init();
    if (!curl) {
        reply->set_status_code(503);
        reply->set_error("local durable Inbox bridge unavailable");
        return;
    }
    const std::string url = "http://127.0.0.1:" +
        std::to_string(config.inbox_port) + input.service();
    Output output;
    struct curl_slist* headers = nullptr;
    headers = curl_slist_append(headers,
        is_event ? "Content-Type: application/json"
                 : "Content-Type: application/octet-stream");
    if (is_blob) {
        headers = curl_slist_append(headers,
            ("X-MTMCT-Blob-SHA256: " + input.service().substr(kBlobs.size())).c_str());
        headers = curl_slist_append(headers,
            ("X-MTMCT-Blob-Size: " + std::to_string(input.body().size())).c_str());
    }
    if (!config.token.empty())
        headers = curl_slist_append(headers,
            ("Authorization: Bearer " + config.token).c_str());
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, is_event ? "POST" : "PUT");
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, input.body().data());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE_LARGE,
                     static_cast<curl_off_t>(input.body().size()));
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 2000L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 15000L);
    curl_easy_setopt(curl, CURLOPT_PROXY, ""); // never proxy localhost.
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, capture);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &output);
    const CURLcode rc = curl_easy_perform(curl);
    long code = 503;
    if (rc == CURLE_OK)
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &code);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    if (rc != CURLE_OK || output.overflow || code < 100 || code > 599) {
        reply->set_status_code(503);
        reply->set_error("local durable Inbox did not return a valid reply");
        return;
    }
    reply->set_status_code(static_cast<std::uint32_t>(code));
    reply->set_body(output.bytes);
    if (code < 200 || code >= 300)
        reply->set_error("local durable Inbox rejected request");
}

std::string tokenFromFile(const std::string& filename) {
    struct stat attributes {};
    if (::stat(filename.c_str(), &attributes) != 0 ||
        !S_ISREG(attributes.st_mode) || (attributes.st_mode & 0077) != 0)
        throw std::runtime_error("token file must be owner-only (chmod 600)");
    std::ifstream in(filename);
    std::string token;
    if (!std::getline(in, token))
        throw std::runtime_error("cannot read token file");
    if (!token.empty() && token.back() == '\r') token.pop_back();
    if (token.size() < 24 || token.size() > 256 ||
        token.find_first_of(" \t\r\n") != std::string::npos)
        throw std::runtime_error("invalid token file content");
    return token;
}
} // namespace

int main(int argc, char** argv) {
    Config cfg;
    std::string token_file;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--listen" && i + 1 < argc) cfg.listen = argv[++i];
        else if (arg == "--inbox-port" && i + 1 < argc)
            cfg.inbox_port = std::atoi(argv[++i]);
        else if (arg == "--token-file" && i + 1 < argc)
            token_file = argv[++i];
        else {
            std::cerr << "Usage: sightmesh-nng-inbox-bridge "
                         "[--listen tcp://127.0.0.1:19703] "
                         "[--inbox-port 18081] [--token-file /private/token]\n";
            return 2;
        }
    }
    if (cfg.inbox_port <= 0 || cfg.inbox_port > 65535) return 2;
    try {
        if (!token_file.empty()) cfg.token = tokenFromFile(token_file);
    } catch (const std::exception& exc) {
        std::cerr << "NNG bridge: " << exc.what() << "\n";
        return 2;
    }
    const bool local_only =
        cfg.listen.compare(0, 16, "tcp://127.0.0.1:") == 0 ||
        cfg.listen.compare(0, 9, "inproc://") == 0;
    if (!local_only && cfg.token.empty()) {
        std::cerr << "NNG bridge: non-loopback requires --token-file; "
                     "also use VPN/TLS tunnel for confidentiality\n";
        return 2;
    }
    if (!local_only)
        std::cerr << "NNG bridge: remote traffic requires a trusted VPN/TLS "
                     "network; token by itself is not encryption\n";

    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) return 1;
    nng_socket socket = NNG_SOCKET_INITIALIZER;
    int rc = nng_rep0_open(&socket);
    if (rc == 0)
        rc = nng_socket_set_size(socket, NNG_OPT_RECVMAXSZ, kMaxRequestBytes);
    if (rc == 0)
        rc = nng_socket_set_ms(socket, NNG_OPT_RECVTIMEO, 250);
    if (rc == 0)
        rc = nng_listen(socket, cfg.listen.c_str(), nullptr, 0);
    if (rc != 0) {
        std::cerr << "NNG bridge: " << nng_strerror(rc) << "\n";
        nng_close(socket);
        curl_global_cleanup();
        return 1;
    }
    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);
    std::cout << "NNG durable Inbox bridge on " << cfg.listen
              << " -> local HTTP 127.0.0.1:" << cfg.inbox_port << std::endl;
    while (running) {
        void* buffer = nullptr;
        std::size_t size = 0;
        rc = nng_recv(socket, &buffer, &size, NNG_FLAG_ALLOC);
        if (rc == NNG_ETIMEDOUT || rc == NNG_EAGAIN) continue;
        if (rc != 0) break;
        sightmesh::wire::v1::ReliableRpcRequest request;
        const bool parsed = size <= kMaxRequestBytes &&
            size <= static_cast<std::size_t>(std::numeric_limits<int>::max()) &&
            request.ParseFromArray(buffer, static_cast<int>(size));
        nng_free(buffer, size);

        sightmesh::wire::v1::ReliableRpcReply reply;
        reply.set_schema_major(1);
        if (!parsed || request.schema_major() != 1 ||
            request.request_id().empty() || request.request_id().size() > 128) {
            reply.set_status_code(400);
            reply.set_error("invalid RPC version/request");
        } else {
            reply.set_request_id(request.request_id());
            if (!matchesSecret(request.bearer_token(), cfg.token)) {
                reply.set_status_code(401);
                reply.set_error("unauthorized");
            } else {
                forward(cfg, request, &reply);
            }
        }

        std::string encoded;
        if (reply.SerializeToString(&encoded))
            (void)nng_send(socket, encoded.data(), encoded.size(), 0);
    }
    nng_close(socket);
    curl_global_cleanup();
    return 0;
}

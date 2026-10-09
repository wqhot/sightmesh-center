// Optional Center read-only HTTP -> typed Protobuf NNG snapshot publisher.
// Only the durable SQLite-backed Center associate process computes identities.
// NNG PUB is best effort; the full WorldState snapshot is authoritative,
// while IdentityMappingChange messages are HINTS (HTTP revision log is durable).
#include <sightmesh/wire/v1/sightmesh_wire.pb.h>
#include <nng/nng.h>
#include <nng/protocol/pubsub0/pub.h>
#include <curl/curl.h>
#include <json/json.h>

#include <atomic>
#include <chrono>
#include <csignal>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <thread>

namespace {
namespace v1 = sightmesh::wire::v1;
volatile std::sig_atomic_t running = 1;
void onSignal(int) { running = 0; }

std::size_t appendBody(char* p, std::size_t size, std::size_t n, void* ptr) {
    std::string& result = *static_cast<std::string*>(ptr);
    if (size && n > std::numeric_limits<std::size_t>::max()/size) return 0;
    const std::size_t bytes = size*n;
    constexpr std::size_t cap = 4 * 1024 * 1024;
    if (result.size() > cap || bytes > cap - result.size()) return 0;
    result.append(p, bytes);
    return bytes;
}

bool fetch(const std::string& url, const std::string& token, Json::Value& out) {
    CURL* c = curl_easy_init();
    if (!c) return false;
    std::string body;
    curl_slist* headers = nullptr;
    if (!token.empty()) headers = curl_slist_append(
        headers, ("Authorization: Bearer " + token).c_str());
    curl_easy_setopt(c, CURLOPT_URL, url.c_str());
    curl_easy_setopt(c, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(c, CURLOPT_FOLLOWLOCATION, 0L);
    curl_easy_setopt(c, CURLOPT_PROXY, "");
    curl_easy_setopt(c, CURLOPT_CONNECTTIMEOUT_MS, 500L);
    curl_easy_setopt(c, CURLOPT_TIMEOUT_MS, 2000L);
    curl_easy_setopt(c, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(c, CURLOPT_WRITEFUNCTION, appendBody);
    curl_easy_setopt(c, CURLOPT_WRITEDATA, &body);
    const CURLcode rc = curl_easy_perform(c);
    long status = 0;
    if (rc == CURLE_OK) curl_easy_getinfo(c, CURLINFO_RESPONSE_CODE, &status);
    curl_slist_free_all(headers);
    curl_easy_cleanup(c);
    if (rc != CURLE_OK || status != 200) return false;
    Json::CharReaderBuilder reader;
    reader["rejectDupKeys"] = true;
    std::string error;
    std::istringstream input(body);
    return Json::parseFromStream(reader, input, &out, &error);
}

std::string readToken(const std::string& path) {
    if (path.empty()) return {};
    struct stat attrs {};
    if (stat(path.c_str(), &attrs) || !S_ISREG(attrs.st_mode) ||
        (attrs.st_mode & 0077))
        throw std::runtime_error("token must be an owner-only regular file");
    std::ifstream input(path);
    std::string token;
    if (!std::getline(input, token) || token.size() < 24 ||
        token.size() > 256 || token.find_first_of(" \t\r\n") != std::string::npos)
        throw std::runtime_error("invalid Center token file");
    return token;
}

bool parseTrackletNode(const std::string& id, std::string& node) {
    Json::CharReaderBuilder reader;
    Json::Value fields;
    std::string error;
    std::istringstream stream(id);
    if (!Json::parseFromStream(reader, stream, &fields, &error) ||
        !fields.isArray() || fields.size() < 5 || !fields[0].isString())
        return false;
    node = fields[0].asString();
    return !node.empty();
}

bool vec3(const Json::Value& p, v1::Vector3* out) {
    if (!p.isArray() || p.size() != 3) return false;
    for (int i=0; i<3; ++i)
        if (!p[i].isNumeric() || !std::isfinite(p[i].asDouble())) return false;
    out->set_x(p[0].asDouble());
    out->set_y(p[1].asDouble());
    out->set_z(p[2].asDouble());
    return true;
}

bool makeState(const Json::Value& world, v1::Envelope& out) {
    if (!world.isObject() || !world["schema_major"].isUInt() ||
        world["schema_major"].asUInt() != 1 ||
        !world["world_revision"].isUInt64() ||
        !world["global_tracks"].isArray() ||
        !world["alignment_policy"].isObject() ||
        !world["last_identity_revision"].isUInt64())
        return false;
    const auto revision = world["world_revision"].asUInt64();
    if (revision == 0) return false;
    out.set_schema_major(1);
    out.set_schema_minor(1);
    out.set_node_id("sightmesh_center");
    out.set_session_id("center-worldstate-v1");
    out.set_stream_id("world_state");
    out.set_stream_sequence(revision);
    out.set_message_id("center/world_state/" + std::to_string(revision));
    const auto now = std::chrono::system_clock::now().time_since_epoch();
    const auto time_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(now).count();
    if (time_ns <= 0) return false;
    out.set_event_time_ns(static_cast<std::uint64_t>(time_ns));
    out.set_record_time_unix_ns(static_cast<std::uint64_t>(time_ns));
    out.set_clock_domain(v1::CLOCK_UNIX_UTC); // Center PUBLICATION time ONLY
    out.set_delivery_class(v1::STATE_LATEST);
    auto* state = out.mutable_world_state();
    state->set_world_revision(revision);
    state->set_identity_revision_watermark(world["last_identity_revision"].asUInt64());
    state->set_association_method(world["solver"].asString());

    for (const auto& entry : world["global_tracks"]) {
        if (!entry.isObject() || !entry["global_id"].isString() ||
            !entry["status"].isString() || !entry["identity_revision"].isUInt64() ||
            !entry["members"].isArray())
            return false;
        v1::GlobalTrack* track = state->add_global_tracks();
        track->set_global_id(entry["global_id"].asString());
        track->set_identity_revision(entry["identity_revision"].asUInt64());
        track->set_status(entry["status"].asString());
        track->set_class_name(entry["class_name"].asString());
        track->set_fusion_status(entry["fusion"].asString());
        for (const auto& member : entry["members"]) {
            if (!member.isString()) return false;
            track->add_contributing_local_track_keys(member.asString());
        }
        const auto& rep = entry["representative"];
        if (rep.isNull()) continue; // unlocalized identity, no false position
        if (!rep.isObject() || rep["source"].asString() !=
                "representative_observation_not_fused" ||
            !rep["source_tracklet"].isString() ||
            !rep["event_time_ns"].isUInt64() ||
            !rep["position_covariance_m2"].isArray() ||
            rep["position_covariance_m2"].size() != 9)
            return false;
        std::string node;
        if (!parseTrackletNode(rep["source_tracklet"].asString(), node))
            return false;
        const auto& align = world["alignment_policy"][node];
        if (!align.isObject() || !align["alignment_id"].isString() ||
            !align["coordinate_frame_id"].isString() ||
            !align["map_revision"].isString() ||
            align["coordinate_frame_id"].asString() !=
                rep["coordinate_frame_id"].asString() ||
            align["map_revision"].asString() != rep["map_revision"].asString())
            return false;
        track->set_alignment_id(align["alignment_id"].asString());
        track->set_source_clock_domain(
            "source-event-domain-not-guaranteed-utc");
        track->set_representative_source_tracklet(
            rep["source_tracklet"].asString());
        track->set_source_event_time_ns(rep["event_time_ns"].asUInt64());
        if (rep["quality"].isNumeric())
            track->set_representative_quality(rep["quality"].asDouble());
        auto* state3 = track->mutable_state();
        if (!vec3(rep["position_map_enu_m"], state3->mutable_position_map_enu_m()))
            return false;
        if (rep["velocity_map_enu_mps"].isArray() &&
            !vec3(rep["velocity_map_enu_mps"],
                  state3->mutable_velocity_map_enu_mps()))
            return false;
        for (const auto& value : rep["position_covariance_m2"]) {
            if (!value.isNumeric() || !std::isfinite(value.asDouble())) return false;
            state3->mutable_position_covariance_m2()->add_row_major(value.asDouble());
        }
        state3->mutable_position_covariance_m2()->set_dimension(3);
        state3->mutable_position_covariance_m2()->set_valid(true);
        state3->set_coordinate_frame_id(rep["coordinate_frame_id"].asString());
        state3->set_map_revision(rep["map_revision"].asString());
        state3->set_source("representative_observation_not_fused");
        state3->set_valid(true);
    }
    return true;
}

bool publish(nng_socket pub, const std::string& topic, const v1::Envelope& msg) {
    std::string wire;
    if (!msg.SerializeToString(&wire)) return false;
    if (wire.size() > 4 * 1024 * 1024) return false;
    std::string frame = topic;
    frame.push_back('\0');
    frame += wire;
    return nng_send(pub, &frame[0], frame.size(), NNG_FLAG_NONBLOCK) == 0;
}
} // namespace

int main(int argc, char** argv) {
    std::string endpoint = "tcp://127.0.0.1:19704", token_file;
    int inbox_port = 18081, period_ms = 500;
    bool allow_network = false;
    for (int i=1; i<argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--listen" && i+1<argc) endpoint = argv[++i];
        else if (arg == "--inbox-port" && i+1<argc) inbox_port = std::atoi(argv[++i]);
        else if (arg == "--token-file" && i+1<argc) token_file = argv[++i];
        else if (arg == "--poll-ms" && i+1<argc) period_ms = std::atoi(argv[++i]);
        else if (arg == "--allow-private-network") allow_network = true;
        else { std::cerr << "invalid Center Wire publisher argument\n"; return 2; }
    }
    if (inbox_port < 1 || inbox_port > 65535 ||
        period_ms < 100 || period_ms > 10000) return 2;
    if (endpoint.find("tcp://127.0.0.1:") != 0 &&
        endpoint.find("inproc://") != 0 && !allow_network) {
        std::cerr << "Non-loopback PUB requires explicit --allow-private-network\n";
        return 2;
    }
    std::string token;
    try { token = readToken(token_file); }
    catch (const std::exception& ex) { std::cerr << ex.what() << '\n'; return 2; }
    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) return 1;
    nng_socket pub = NNG_SOCKET_INITIALIZER;
    int rc = nng_pub0_open(&pub);
    if (rc == 0) rc = nng_listen(pub, endpoint.c_str(), nullptr, 0);
    if (rc != 0) {
        std::cerr << "Center WorldState PUB init: " << nng_strerror(rc) << '\n';
        nng_close(pub);
        curl_global_cleanup();
        return 1;
    }
    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);
    const std::string url = "http://127.0.0.1:" +
        std::to_string(inbox_port) + "/api/v1/mtmct/global-tracks";
    std::uint64_t last_revision = 0;
    while (running) {
        Json::Value data;
        if (fetch(url, token, data)) {
            v1::Envelope packet;
            if (makeState(data, packet) &&
                packet.stream_sequence() > last_revision) {
                if (publish(pub, "/sightmesh/center/world_state", packet))
                    last_revision = packet.stream_sequence();
            }
        }
        for (int elapsed=0; running && elapsed<period_ms; elapsed+=50)
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    nng_close(pub);
    curl_global_cleanup();
    return 0;
}

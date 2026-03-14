// ═══════════════════════════════════════════════════════════════════
//  VOLTEX API SERVER  —  voltex_api.cpp
//
//  Wraps the Voltex REPL into a TCP socket server that accepts
//  newline-delimited JSON commands and returns JSON responses.
//
//  New commands added vs the REPL:
//    REGISTER  — label a hash in the registry
//    LOOKUP    — resolve label → hash → text in one call
//    RLIST     — list registry entries, optional namespace filter
//    FORGET    — unpin + deregister a label
//    UNPIN     — remove pin from a node (new — REPL only had PIN)
//    STATUS    — health check / vault stats in JSON
//
//  Registry is persisted to registry.vtxr alongside vault.meta
//
//  Build (Windows, MSVC / vcpkg):
//    cl /std:c++17 /O2 voltex_api.cpp /link ws2_32.lib libssl.lib libcrypto.lib
//
//  Build (Linux/macOS):
//    g++ -std=c++17 -O2 voltex_api.cpp -lssl -lcrypto -lpthread -o voltex_api
//
//  Default port: 7474.  Set env VOLTEX_PORT to override.
// ═══════════════════════════════════════════════════════════════════

// ─── platform socket includes ───────────────────────────────────────
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
#define CLOSE_SOCKET(s) closesocket(s)
typedef SOCKET sock_t;
typedef int socklen_t;
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#define CLOSE_SOCKET(s) close(s)
typedef int sock_t;
static constexpr sock_t INVALID_SOCKET = -1;
static constexpr int SOCKET_ERROR = -1;
#endif

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <list>
#include <unordered_map>
#include <algorithm>
#include <cstring>
#include <cmath>
#include <ctime>
#include <cstdlib>
#include <functional>
#include <cassert>
#include <thread>
#include <mutex>
#include <atomic>
#include <openssl/sha.h>

// ─── Configuration ──────────────────────────────────────────────────
static constexpr int SHA256_BLOCK_SIZE = 32;
static constexpr float DECAY_MULTIPLIER = 0.95f;
static constexpr float DECAY_THRESHOLD = 0.2f;
static constexpr int MAX_BUF = 1024;
static constexpr int MAX_HOT_BLOBS = 4096;
static constexpr int DEFAULT_PORT = 7474;

static constexpr const char *META_FILE = "vault.meta";
static constexpr const char *BLOB_FILE = "vault.blob";
static constexpr const char *REGISTRY_FILE = "registry.vtxr";

// ─── Global mutex (all vault ops are single-threaded under this) ─────
static std::mutex g_vault_mutex;

// ════════════════════════════════════════════════════════════════════
//  CORE VAULT CODE  (identical to voltex_paged.cpp — reproduced here
//  so voltex_api.cpp is a self-contained translation unit)
// ════════════════════════════════════════════════════════════════════

struct HexID
{
    uint8_t hash[SHA256_BLOCK_SIZE] = {};
    bool operator==(const HexID &o) const { return std::memcmp(hash, o.hash, 32) == 0; }
    bool operator!=(const HexID &o) const { return !(*this == o); }
    bool isNull() const
    {
        for (int i = 0; i < 32; ++i)
            if (hash[i])
                return false;
        return true;
    }
    std::string toHexStr() const
    {
        char buf[65];
        for (int i = 0; i < 32; ++i)
            std::sprintf(buf + i * 2, "%02x", hash[i]);
        buf[64] = '\0';
        return {buf};
    }
};
struct HexIDHasher
{
    std::size_t operator()(const HexID &id) const
    {
        uint32_t v;
        std::memcpy(&v, id.hash, 4);
        return v;
    }
};
HexID computeSHA(const void *data, std::size_t len)
{
    HexID out;
    SHA256(reinterpret_cast<const unsigned char *>(data), len, out.hash);
    return out;
}
HexID computeSHADual(const HexID &a, const HexID &b)
{
    uint8_t raw[64];
    std::memcpy(raw, a.hash, 32);
    std::memcpy(raw + 32, b.hash, 32);
    return computeSHA(raw, 64);
}
HexID hexIDFromStr(const std::string &s) { return computeSHA(s.data(), s.size()); }

enum class NodeType : uint8_t
{
    ATOM = 0,
    CHUNK = 1
};

struct NodeMeta
{
    HexID id;
    HexID child_a_id, child_b_id;
    uint64_t blob_offset = 0;
    float vitality = 1.0f;
    NodeType type = NodeType::ATOM;
    bool is_pinned = false;
    bool blob_dirty = false;
    uint8_t _pad = 0;
    std::vector<HexID> dendrites;
    NodeMeta *next = nullptr;
};
struct NodeBlob
{
    HexID id;
    std::string lexeme;
};
struct History
{
    HexID old_id, new_id;
    char timestamp[20] = {};
    History *next = nullptr;
};

class VaultPager
{
public:
    NodeMeta *vault = nullptr;
    std::unordered_map<HexID, NodeMeta *, HexIDHasher> meta_map;
    std::unordered_map<HexID, NodeBlob, HexIDHasher> blob_cache;
    std::list<HexID> lru_order;
    std::fstream blob_fstream;
    uint64_t blob_eof = 0;
    uint64_t cache_hits = 0, cache_misses = 0;

    VaultPager()
    {
        blob_fstream.open(BLOB_FILE, std::ios::in | std::ios::out | std::ios::binary);
        if (!blob_fstream.is_open())
        {
            std::ofstream tmp(BLOB_FILE, std::ios::binary);
            tmp.close();
            blob_fstream.open(BLOB_FILE, std::ios::in | std::ios::out | std::ios::binary);
        }
        blob_fstream.seekg(0, std::ios::end);
        blob_eof = static_cast<uint64_t>(blob_fstream.tellg());
    }
    ~VaultPager()
    {
        if (blob_fstream.is_open())
            blob_fstream.close();
        NodeMeta *c = vault;
        while (c)
        {
            NodeMeta *nx = c->next;
            delete c;
            c = nx;
        }
    }
    void mapMeta(NodeMeta *m)
    {
        if (m)
            meta_map[m->id] = m;
    }
    void unmapMeta(const HexID &id)
    {
        meta_map.erase(id);
        blob_cache.erase(id);
        lru_order.remove(id);
    }
    NodeMeta *findMeta(const HexID &id)
    {
        auto it = meta_map.find(id);
        return it != meta_map.end() ? it->second : nullptr;
    }
    NodeMeta *findMetaFromHexStr(const std::string &hex)
    {
        if (hex.size() != 64)
            return nullptr;
        HexID tmp;
        for (int i = 0; i < 32; ++i)
        {
            unsigned int b;
            if (std::sscanf(hex.c_str() + i * 2, "%02x", &b) != 1)
                return nullptr;
            tmp.hash[i] = static_cast<uint8_t>(b);
        }
        return findMeta(tmp);
    }
    uint64_t writeBlob(const NodeBlob &blob)
    {
        uint64_t off = blob_eof;
        blob_fstream.seekp(static_cast<std::streamoff>(off));
        blob_fstream.write(reinterpret_cast<const char *>(blob.id.hash), 32);
        uint32_t ll = static_cast<uint32_t>(blob.lexeme.size());
        blob_fstream.write(reinterpret_cast<const char *>(&ll), sizeof(uint32_t));
        if (ll > 0)
            blob_fstream.write(blob.lexeme.data(), ll);
        blob_fstream.flush();
        blob_eof = off + 32 + sizeof(uint32_t) + ll;
        return off;
    }
    NodeBlob deserializeBlob(uint64_t off)
    {
        blob_fstream.seekg(static_cast<std::streamoff>(off));
        NodeBlob blob;
        blob_fstream.read(reinterpret_cast<char *>(blob.id.hash), 32);
        uint32_t ll = 0;
        blob_fstream.read(reinterpret_cast<char *>(&ll), sizeof(uint32_t));
        if (ll > 0)
        {
            blob.lexeme.resize(ll);
            blob_fstream.read(blob.lexeme.data(), ll);
        }
        return blob;
    }
    void touchLRU(const HexID &id)
    {
        lru_order.remove(id);
        lru_order.push_back(id);
    }
    void evictCold()
    {
        for (auto it = lru_order.begin(); it != lru_order.end(); ++it)
        {
            NodeMeta *m = findMeta(*it);
            if (!m || !m->is_pinned)
            {
                blob_cache.erase(*it);
                lru_order.erase(it);
                return;
            }
        }
        if (!lru_order.empty())
        {
            blob_cache.erase(lru_order.front());
            lru_order.pop_front();
        }
    }
    const NodeBlob *getBlob(const HexID &id)
    {
        auto it = blob_cache.find(id);
        if (it != blob_cache.end())
        {
            ++cache_hits;
            touchLRU(id);
            return &it->second;
        }
        ++cache_misses;
        NodeMeta *meta = findMeta(id);
        if (!meta)
            return nullptr;
        if (blob_cache.size() >= MAX_HOT_BLOBS)
            evictCold();
        NodeBlob blob = deserializeBlob(meta->blob_offset);
        blob_cache[id] = std::move(blob);
        lru_order.push_back(id);
        return &blob_cache[id];
    }
    void cacheBlob(NodeBlob blob)
    {
        if (blob_cache.size() >= MAX_HOT_BLOBS)
            evictCold();
        HexID id = blob.id;
        blob_cache[id] = std::move(blob);
        touchLRU(id);
    }
    int countAtoms() const
    {
        int c = 0;
        for (NodeMeta *n = vault; n; n = n->next)
            if (n->type == NodeType::ATOM)
                ++c;
        return c;
    }
    int countChunks() const
    {
        int c = 0;
        for (NodeMeta *n = vault; n; n = n->next)
            if (n->type == NodeType::CHUNK)
                ++c;
        return c;
    }
    int countTotal() const
    {
        int c = 0;
        for (NodeMeta *n = vault; n; n = n->next)
            ++c;
        return c;
    }

    void saveMeta()
    {
        std::ofstream f(META_FILE, std::ios::binary);
        if (!f)
            return;
        int count = 0;
        for (NodeMeta *c = vault; c; c = c->next)
            if (c->vitality > -1.0f)
                ++count;
        f.write(reinterpret_cast<const char *>(&count), sizeof(int));
        for (NodeMeta *c = vault; c; c = c->next)
        {
            if (c->vitality <= -1.0f)
                continue;
            f.write(reinterpret_cast<const char *>(&c->id), sizeof(HexID));
            f.write(reinterpret_cast<const char *>(&c->child_a_id), sizeof(HexID));
            f.write(reinterpret_cast<const char *>(&c->child_b_id), sizeof(HexID));
            f.write(reinterpret_cast<const char *>(&c->blob_offset), sizeof(uint64_t));
            f.write(reinterpret_cast<const char *>(&c->vitality), sizeof(float));
            f.write(reinterpret_cast<const char *>(&c->type), sizeof(NodeType));
            int pin = c->is_pinned ? 1 : 0;
            f.write(reinterpret_cast<const char *>(&pin), sizeof(int));
            int dc = static_cast<int>(c->dendrites.size());
            f.write(reinterpret_cast<const char *>(&dc), sizeof(int));
            if (dc > 0)
                f.write(reinterpret_cast<const char *>(c->dendrites.data()), dc * sizeof(HexID));
        }
        f.write(reinterpret_cast<const char *>(&blob_eof), sizeof(uint64_t));
    }
    void loadMeta()
    {
        std::ifstream f(META_FILE, std::ios::binary);
        if (!f)
            return;
        NodeMeta *c = vault;
        while (c)
        {
            NodeMeta *nx = c->next;
            delete c;
            c = nx;
        }
        vault = nullptr;
        meta_map.clear();
        blob_cache.clear();
        lru_order.clear();
        int count = 0;
        f.read(reinterpret_cast<char *>(&count), sizeof(int));
        for (int i = 0; i < count; ++i)
        {
            NodeMeta *m = new NodeMeta;
            f.read(reinterpret_cast<char *>(&m->id), sizeof(HexID));
            f.read(reinterpret_cast<char *>(&m->child_a_id), sizeof(HexID));
            f.read(reinterpret_cast<char *>(&m->child_b_id), sizeof(HexID));
            f.read(reinterpret_cast<char *>(&m->blob_offset), sizeof(uint64_t));
            f.read(reinterpret_cast<char *>(&m->vitality), sizeof(float));
            f.read(reinterpret_cast<char *>(&m->type), sizeof(NodeType));
            int pin;
            f.read(reinterpret_cast<char *>(&pin), sizeof(int));
            m->is_pinned = (pin != 0);
            int dc;
            f.read(reinterpret_cast<char *>(&dc), sizeof(int));
            m->dendrites.resize(dc);
            if (dc > 0)
                f.read(reinterpret_cast<char *>(m->dendrites.data()), dc * sizeof(HexID));
            m->next = vault;
            vault = m;
            mapMeta(m);
        }
        f.read(reinterpret_cast<char *>(&blob_eof), sizeof(uint64_t));
    }
};

static VaultPager pager;
static History *history_log = nullptr;

void recordHistory(const HexID &o, const HexID &n)
{
    History *e = new History;
    e->old_id = o;
    e->new_id = n;
    std::time_t now = std::time(nullptr);
    std::strftime(e->timestamp, 20, "%Y-%m-%d %H:%M:%S", std::localtime(&now));
    e->next = history_log;
    history_log = e;
}

int findNeuralSplit(const std::string &text)
{
    int len = static_cast<int>(text.size());
    if (len <= 4)
        return len / 2;
    int weakest = len / 2;
    float minStr = 2.0f;
    for (int i = 2; i < len - 2; ++i)
    {
        HexID ia = hexIDFromStr(text.substr(0, i)), ib = hexIDFromStr(text.substr(i));
        NodeMeta *a = pager.findMeta(ia), *b = pager.findMeta(ib);
        float s = (a && b) ? std::sqrt(a->vitality * b->vitality) : 0.5f;
        if (text[i] == ' ')
            s -= 0.2f;
        if (s < minStr)
        {
            minStr = s;
            weakest = i;
        }
    }
    return weakest;
}

NodeMeta *ingest(const std::string &text)
{
    int len = static_cast<int>(text.size());
    if (len <= 4)
    {
        HexID id = hexIDFromStr(text);
        NodeMeta *ex = pager.findMeta(id);
        if (ex)
        {
            ex->vitality = 1.0f;
            return ex;
        }
        NodeBlob blob;
        blob.id = id;
        blob.lexeme = text;
        uint64_t off = pager.writeBlob(blob);
        pager.cacheBlob(std::move(blob));
        NodeMeta *m = new NodeMeta;
        m->id = id;
        m->type = NodeType::ATOM;
        m->vitality = 1.0f;
        m->blob_offset = off;
        m->next = pager.vault;
        pager.vault = m;
        pager.mapMeta(m);
        return m;
    }
    int sp = findNeuralSplit(text);
    NodeMeta *a = ingest(text.substr(0, sp)), *b = ingest(text.substr(sp));
    HexID cid = computeSHADual(a->id, b->id);
    NodeMeta *ex = pager.findMeta(cid);
    if (ex)
    {
        ex->vitality = std::min(ex->vitality + 0.1f, 1.0f);
        return ex;
    }
    NodeBlob blob;
    blob.id = cid;
    uint64_t off = pager.writeBlob(blob);
    NodeMeta *m = new NodeMeta;
    m->id = cid;
    m->type = NodeType::CHUNK;
    m->child_a_id = a->id;
    m->child_b_id = b->id;
    m->vitality = std::sqrt(a->vitality * b->vitality);
    m->blob_offset = off;
    m->next = pager.vault;
    pager.vault = m;
    a->dendrites.push_back(cid);
    b->dendrites.push_back(cid);
    pager.mapMeta(m);
    return m;
}

void unrollToBuffer(NodeMeta *m, std::string &out)
{
    if (!m)
        return;
    if (m->type == NodeType::ATOM)
    {
        const NodeBlob *b = pager.getBlob(m->id);
        if (b && out.size() + b->lexeme.size() < MAX_BUF - 1)
            out += b->lexeme;
    }
    else
    {
        unrollToBuffer(pager.findMeta(m->child_a_id), out);
        unrollToBuffer(pager.findMeta(m->child_b_id), out);
    }
}

void rerouteGrandparents(NodeMeta *old_p, NodeMeta *new_t)
{
    if (!old_p || !new_t || old_p == new_t)
        return;
    for (NodeMeta *curr = pager.vault; curr; curr = curr->next)
    {
        if (curr == new_t)
            continue;
        if (curr->child_a_id == old_p->id)
            curr->child_a_id = new_t->id;
        if (curr->child_b_id == old_p->id)
            curr->child_b_id = new_t->id;
        for (auto &d : curr->dendrites)
            if (d == old_p->id)
                d = new_t->id;
    }
}

void performCoordinatedMorph()
{
    std::vector<NodeMeta *> nodes;
    for (NodeMeta *c = pager.vault; c; c = c->next)
        nodes.push_back(c);
    for (NodeMeta *m : nodes)
    {
        if (m->type == NodeType::CHUNK && !m->child_a_id.isNull() && !m->child_b_id.isNull())
        {
            HexID old_id = m->id, new_id = computeSHADual(m->child_a_id, m->child_b_id);
            if (new_id != old_id)
            {
                NodeMeta *col = pager.findMeta(new_id);
                if (col && col != m)
                {
                    recordHistory(m->id, col->id);
                    rerouteGrandparents(m, col);
                    m->vitality = -1.0f;
                }
                else
                {
                    pager.unmapMeta(old_id);
                    m->id = new_id;
                    pager.mapMeta(m);
                    recordHistory(old_id, new_id);
                }
            }
        }
    }
}

void executeDreamCycle()
{
    int stale = 0;
    for (NodeMeta *m = pager.vault; m; m = m->next)
    {
        if (!m->is_pinned && m->vitality > 0)
            m->vitality *= DECAY_MULTIPLIER;
        if (m->vitality < DECAY_THRESHOLD || m->vitality < 0)
        {
            if (!m->dendrites.empty() && m->vitality >= 0)
            {
                m->vitality = -2.0f;
                ++stale;
            }
            else
                m->vitality = -1.0f;
        }
    }
    if (stale > 0)
        performCoordinatedMorph();
    NodeMeta **curr = &pager.vault;
    while (*curr)
    {
        if ((*curr)->vitality <= -1.0f)
        {
            NodeMeta *old = *curr;
            *curr = (*curr)->next;
            pager.unmapMeta(old->id);
            delete old;
        }
        else
            curr = &((*curr)->next);
    }
}

int getNodeHeight(NodeMeta *m)
{
    if (!m)
        return 0;
    if (m->type == NodeType::ATOM)
        return 1;
    return 1 + std::max(getNodeHeight(pager.findMeta(m->child_a_id)),
                        getNodeHeight(pager.findMeta(m->child_b_id)));
}
int getVaultMaxDepth()
{
    int mx = 0;
    for (NodeMeta *c = pager.vault; c; c = c->next)
        mx = std::max(mx, getNodeHeight(c));
    return mx;
}

// ════════════════════════════════════════════════════════════════════
//  REGISTRY  —  label → hash mapping with namespace support
// ════════════════════════════════════════════════════════════════════

struct RegistryEntry
{
    std::string label; // e.g. "goals/learn-voltex"
    std::string hex;   // 64-char hex string
    std::string ns;    // e.g. "goals"
    bool pinned;       // mirrors vault pin state at registration time
};

static std::unordered_map<std::string, RegistryEntry> g_registry;

std::string extractNamespace(const std::string &label)
{
    auto pos = label.find('/');
    return pos == std::string::npos ? "" : label.substr(0, pos);
}

bool registrySave()
{
    std::ofstream f(REGISTRY_FILE);
    if (!f)
        return false;
    for (auto &[k, v] : g_registry)
        f << v.label << "\t" << v.hex << "\t" << v.ns << "\t" << (v.pinned ? 1 : 0) << "\n";
    return true;
}
void registryLoad()
{
    std::ifstream f(REGISTRY_FILE);
    if (!f)
        return;
    g_registry.clear();
    std::string line;
    while (std::getline(f, line))
    {
        if (line.empty())
            continue;
        std::istringstream ss(line);
        RegistryEntry e;
        std::string pinstr;
        if (!std::getline(ss, e.label, '\t'))
            continue;
        if (!std::getline(ss, e.hex, '\t'))
            continue;
        if (!std::getline(ss, e.ns, '\t'))
            continue;
        if (!std::getline(ss, pinstr, '\t'))
            e.pinned = false;
        else
            e.pinned = (pinstr == "1");
        g_registry[e.label] = e;
    }
}

// ════════════════════════════════════════════════════════════════════
//  MINIMAL JSON HELPERS  (no external deps)
// ════════════════════════════════════════════════════════════════════

// Escape a string for JSON output
std::string jsonEscape(const std::string &s)
{
    std::string out;
    out.reserve(s.size() + 4);
    for (char c : s)
    {
        if (c == '"')
            out += "\\\"";
        else if (c == '\\')
            out += "\\\\";
        else if (c == '\n')
            out += "\\n";
        else if (c == '\r')
            out += "\\r";
        else if (c == '\t')
            out += "\\t";
        else
            out += c;
    }
    return out;
}

std::string jsonOk(const std::string &fields)
{
    return "{\"ok\":true," + fields + "}";
}
std::string jsonErr(const std::string &msg)
{
    return "{\"ok\":false,\"error\":\"" + jsonEscape(msg) + "\"}";
}

// Extremely minimal key extraction — finds "key":"value" in JSON string
// Sufficient for the simple flat commands the API receives.
std::string jsonGet(const std::string &json, const std::string &key)
{
    std::string needle = "\"" + key + "\"";
    auto pos = json.find(needle);
    if (pos == std::string::npos)
        return "";
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos)
        return "";
    ++pos;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t'))
        ++pos;
    if (pos >= json.size())
        return "";
    if (json[pos] == '"')
    {
        // string value
        ++pos;
        std::string val;
        while (pos < json.size() && json[pos] != '"')
        {
            if (json[pos] == '\\')
            {
                ++pos;
                if (pos < json.size())
                    val += json[pos++];
            }
            else
                val += json[pos++];
        }
        return val;
    }
    // number or bool — read until delimiter
    std::string val;
    while (pos < json.size() && json[pos] != ',' && json[pos] != '}' && json[pos] != ']')
        val += json[pos++];
    // trim
    while (!val.empty() && (val.back() == ' ' || val.back() == '\t'))
        val.pop_back();
    return val;
}

// ════════════════════════════════════════════════════════════════════
//  COMMAND DISPATCH
// ════════════════════════════════════════════════════════════════════

// Each handler receives the raw JSON string and returns a JSON string.

std::string cmdIngest(const std::string &req)
{
    std::string text = jsonGet(req, "text");
    if (text.empty())
        return jsonErr("missing 'text' field");
    NodeMeta *res = ingest(text);
    if (!res)
        return jsonErr("ingest failed");
    std::string hex = res->id.toHexStr();
    return jsonOk("\"hash\":\"" + hex + "\",\"vitality\":" + std::to_string(res->vitality));
}

std::string cmdUnroll(const std::string &req)
{
    std::string hex = jsonGet(req, "hash");
    if (hex.empty())
        return jsonErr("missing 'hash' field");
    NodeMeta *m = pager.findMetaFromHexStr(hex);
    if (!m)
        return jsonErr("node not found: " + hex);
    std::string buf;
    unrollToBuffer(m, buf);
    return jsonOk("\"hash\":\"" + hex + "\",\"text\":\"" + jsonEscape(buf) + "\"");
}

std::string cmdPin(const std::string &req)
{
    std::string hex = jsonGet(req, "hash");
    if (hex.empty())
        return jsonErr("missing 'hash' field");
    NodeMeta *m = pager.findMetaFromHexStr(hex);
    if (!m)
        return jsonErr("node not found");
    m->is_pinned = true;
    m->vitality = 1.0f;
    return jsonOk("\"hash\":\"" + hex + "\",\"status\":\"immortal\"");
}

std::string cmdUnpin(const std::string &req)
{
    std::string hex = jsonGet(req, "hash");
    if (hex.empty())
        return jsonErr("missing 'hash' field");
    NodeMeta *m = pager.findMetaFromHexStr(hex);
    if (!m)
        return jsonErr("node not found");
    m->is_pinned = false;
    return jsonOk("\"hash\":\"" + hex + "\",\"status\":\"decaying\",\"vitality\":" + std::to_string(m->vitality));
}

std::string cmdDream(const std::string &)
{
    int before = pager.countTotal();
    executeDreamCycle();
    int after = pager.countTotal();
    return jsonOk("\"purged\":" + std::to_string(before - after) + ",\"remaining\":" + std::to_string(after));
}

std::string cmdSave(const std::string &)
{
    pager.saveMeta();
    registrySave();
    return jsonOk("\"nodes\":" + std::to_string(pager.countTotal()) +
                  ",\"registry_entries\":" + std::to_string(g_registry.size()));
}

std::string cmdLoad(const std::string &)
{
    pager.loadMeta();
    registryLoad();
    return jsonOk("\"nodes\":" + std::to_string(pager.countTotal()) +
                  ",\"registry_entries\":" + std::to_string(g_registry.size()));
}

std::string cmdStatus(const std::string &)
{
    int atoms = pager.countAtoms(), chunks = pager.countChunks();
    int depth = getVaultMaxDepth();
    float hitRate = 0.f;
    uint64_t total = pager.cache_hits + pager.cache_misses;
    if (total > 0)
        hitRate = 100.f * static_cast<float>(pager.cache_hits) / static_cast<float>(total);
    return jsonOk(
        "\"atoms\":" + std::to_string(atoms) +
        ",\"chunks\":" + std::to_string(chunks) +
        ",\"total\":" + std::to_string(atoms + chunks) +
        ",\"max_depth\":" + std::to_string(depth) +
        ",\"blob_bytes\":" + std::to_string(pager.blob_eof) +
        ",\"hot_blobs\":" + std::to_string(pager.blob_cache.size()) +
        ",\"cache_hit_rate\":" + std::to_string(hitRate) +
        ",\"registry_entries\":" + std::to_string(g_registry.size()));
}

// ── Registry commands ───────────────────────────────────────────────

std::string cmdRegister(const std::string &req)
{
    std::string label = jsonGet(req, "label");
    std::string hex = jsonGet(req, "hash");
    if (label.empty())
        return jsonErr("missing 'label' field");
    if (hex.empty())
        return jsonErr("missing 'hash' field");
    NodeMeta *m = pager.findMetaFromHexStr(hex);
    if (!m)
        return jsonErr("node not found — ingest first");
    RegistryEntry e;
    e.label = label;
    e.hex = hex;
    e.ns = extractNamespace(label);
    e.pinned = m->is_pinned;
    g_registry[label] = e;
    registrySave();
    return jsonOk("\"label\":\"" + jsonEscape(label) + "\",\"hash\":\"" + hex + "\"");
}

std::string cmdLookup(const std::string &req)
{
    std::string label = jsonGet(req, "label");
    if (label.empty())
        return jsonErr("missing 'label' field");
    auto it = g_registry.find(label);
    if (it == g_registry.end())
        return jsonErr("label not found: " + label);
    const RegistryEntry &e = it->second;
    NodeMeta *m = pager.findMetaFromHexStr(e.hex);
    if (!m)
        return jsonErr("node has decayed — hash no longer in vault");
    std::string buf;
    unrollToBuffer(m, buf);
    return jsonOk(
        "\"label\":\"" + jsonEscape(label) + "\","
                                             "\"hash\":\"" +
        e.hex + "\","
                "\"text\":\"" +
        jsonEscape(buf) + "\","
                          "\"vitality\":" +
        std::to_string(m->vitality) + ","
                                      "\"pinned\":" +
        (m->is_pinned ? "true" : "false"));
}

std::string cmdRlist(const std::string &req)
{
    std::string ns_filter = jsonGet(req, "namespace"); // "" = all
    std::string arr = "[";
    bool first = true;
    for (auto &[k, e] : g_registry)
    {
        if (!ns_filter.empty() && e.ns != ns_filter)
            continue;
        NodeMeta *m = pager.findMetaFromHexStr(e.hex);
        float vit = m ? m->vitality : 0.f;
        bool pin = m ? m->is_pinned : false;
        bool alive = m != nullptr;
        if (!first)
            arr += ",";
        arr += "{\"label\":\"" + jsonEscape(e.label) + "\","
                                                       "\"hash\":\"" +
               e.hex + "\","
                       "\"namespace\":\"" +
               jsonEscape(e.ns) + "\","
                                  "\"vitality\":" +
               std::to_string(vit) + ","
                                     "\"pinned\":" +
               (pin ? "true" : "false") + ","
                                          "\"alive\":" +
               (alive ? "true" : "false") + "}";
        first = false;
    }
    arr += "]";
    return jsonOk("\"entries\":" + arr + ",\"count\":" + std::to_string(g_registry.size()));
}

std::string cmdForget(const std::string &req)
{
    std::string label = jsonGet(req, "label");
    if (label.empty())
        return jsonErr("missing 'label' field");
    auto it = g_registry.find(label);
    if (it == g_registry.end())
        return jsonErr("label not found");
    std::string hex = it->second.hex;
    // unpin the node so it decays
    NodeMeta *m = pager.findMetaFromHexStr(hex);
    if (m)
    {
        m->is_pinned = false;
    }
    g_registry.erase(it);
    registrySave();
    return jsonOk("\"label\":\"" + jsonEscape(label) + "\",\"status\":\"forgotten\",\"hash\":\"" + hex + "\"");
}

// ─── dispatch table ──────────────────────────────────────────────────

struct CmdEntry
{
    std::string name;
    std::function<std::string(const std::string &)> fn;
};
static const std::vector<CmdEntry> COMMANDS = {
    {"INGEST", cmdIngest},
    {"UNROLL", cmdUnroll},
    {"PIN", cmdPin},
    {"UNPIN", cmdUnpin},
    {"DREAM", cmdDream},
    {"SAVE", cmdSave},
    {"LOAD", cmdLoad},
    {"STATUS", cmdStatus},
    {"REGISTER", cmdRegister},
    {"LOOKUP", cmdLookup},
    {"RLIST", cmdRlist},
    {"FORGET", cmdForget},
};

std::string dispatch(const std::string &raw)
{
    std::string cmd = jsonGet(raw, "cmd");
    if (cmd.empty())
        return jsonErr("missing 'cmd' field");
    for (auto &e : COMMANDS)
        if (e.name == cmd)
        {
            std::lock_guard<std::mutex> lock(g_vault_mutex);
            return e.fn(raw);
        }
    return jsonErr("unknown command: " + cmd);
}

// ════════════════════════════════════════════════════════════════════
//  TCP SERVER
//
//  Protocol: newline-delimited JSON over a persistent TCP connection.
//
//  Client sends:
//    {"cmd":"INGEST","text":"hello world"}\n
//
//  Server responds:
//    {"ok":true,"hash":"a3f8...","vitality":1.0}\n
//
//  One command per line. Connection may be held open for multiple
//  commands. Server reads until '\n', dispatches, writes response+'\n'.
// ════════════════════════════════════════════════════════════════════

void handleClient(sock_t client_fd)
{
    std::string buf;
    char tmp[4096];
    while (true)
    {
        // Read until newline
        while (buf.find('\n') == std::string::npos)
        {
            int n = recv(client_fd, tmp, sizeof(tmp) - 1, 0);
            if (n <= 0)
                goto done;
            tmp[n] = '\0';
            buf += tmp;
        }
        // Process all complete lines
        while (true)
        {
            auto nl = buf.find('\n');
            if (nl == std::string::npos)
                break;
            std::string line = buf.substr(0, nl);
            buf = buf.substr(nl + 1);
            if (!line.empty() && line.back() == '\r')
                line.pop_back();
            if (line.empty())
                continue;

            std::string resp = dispatch(line) + "\n";
            const char *p = resp.c_str();
            int rem = static_cast<int>(resp.size());
            while (rem > 0)
            {
                int sent = send(client_fd, p, rem, 0);
                if (sent <= 0)
                    goto done;
                p += sent;
                rem -= sent;
            }
        }
    }
done:
    CLOSE_SOCKET(client_fd);
}

int main(int argc, char **argv)
{
    // Load existing vault on startup
    pager.loadMeta();
    registryLoad();

    int port = DEFAULT_PORT;
    const char *env_port = std::getenv("VOLTEX_PORT");
    if (env_port)
        port = std::atoi(env_port);
    if (argc >= 2)
        port = std::atoi(argv[1]);

#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
#endif

    sock_t server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd == INVALID_SOCKET)
    {
        std::fprintf(stderr, "[ERROR] socket() failed\n");
        return 1;
    }
    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char *>(&opt), sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    addr.sin_addr.s_addr = INADDR_ANY;

    if (bind(server_fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) == SOCKET_ERROR)
    {
        std::fprintf(stderr, "[ERROR] bind() failed on port %d\n", port);
        return 1;
    }
    listen(server_fd, 32);

    std::printf("\n");
    std::printf("  ╔══════════════════════════════════════╗\n");
    std::printf("  ║   VOLTEX API SERVER  —  port %d    ║\n", port);
    std::printf("  ║   vault loaded: %6d nodes          ║\n", pager.countTotal());
    std::printf("  ║   registry:     %6zu labels         ║\n", g_registry.size());
    std::printf("  ╠══════════════════════════════════════╣\n");
    std::printf("  ║  Protocol: newline-delimited JSON    ║\n");
    std::printf("  ║  Commands: INGEST UNROLL PIN UNPIN   ║\n");
    std::printf("  ║            DREAM SAVE LOAD STATUS    ║\n");
    std::printf("  ║            REGISTER LOOKUP RLIST     ║\n");
    std::printf("  ║            FORGET                    ║\n");
    std::printf("  ╚══════════════════════════════════════╝\n\n");

    while (true)
    {
        sockaddr_in client_addr{};
        socklen_t client_len = sizeof(client_addr);
        sock_t client_fd = accept(server_fd, reinterpret_cast<sockaddr *>(&client_addr), &client_len);
        if (client_fd == INVALID_SOCKET)
            continue;
        std::thread(handleClient, client_fd).detach();
    }

    CLOSE_SOCKET(server_fd);
#ifdef _WIN32
    WSACleanup();
#endif
    return 0;
}

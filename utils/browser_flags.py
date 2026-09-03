"""Shared Chrome launch flags.

Kept in one place because the app builds three separate browsers (search, download
engine, profile browser) and they must not drift apart.

--- Why DNS needs forcing ---
Chrome delegates name lookups to the operating system, so it inherits whatever DNS
the machine is configured with. ISP resolvers routinely fail to resolve the anime
sites (users see net::ERR_NAME_NOT_RESOLVED even though their connection is fine).

Chrome's own DNS-over-HTTPS switches and profile preferences were both measured to
be ignored here -- a deliberately bogus DoH endpoint in "secure" mode still resolved
normally, i.e. Chrome silently fell back to system DNS. So instead we resolve the
hostnames ourselves through Google Public DNS (8.8.8.8, over its HTTPS API) and hand
Chrome the answers via --host-resolver-rules, which IS honoured. That keeps the
hostname intact for TLS/SNI and Host headers, so CDN-hosted sites still work.

This fails open: any hostname Google can't resolve simply gets no rule and falls
back to the system resolver, so the app is never worse off than before.
"""

import json
import threading
import urllib.parse
import urllib.request

GOOGLE_DNS_API = "https://dns.google/resolve"

# Hostnames worth pinning: the supported anime sites plus the file hosts episodes
# are actually fetched from.
DEFAULT_HOSTS = (
    "witanime.life", "www.witanime.life",
    # det.* is where eta.animerco.org now redirects. Pinning only the entry host
    # would leave the host every page actually loads from unresolved, which is the
    # failure this list exists to prevent.
    "eta.animerco.org", "det.animerco.org", "animerco.org", "www.animerco.org",
    "www.mediafire.com", "mediafire.com",
    "drive.google.com", "drive.usercontent.google.com",
    "workupload.com", "www.workupload.com",
    "mp4upload.com", "www.mp4upload.com",
    "yourupload.com", "www.yourupload.com",
    "mega.nz",
)

_cache = {}
_cache_lock = threading.Lock()


def google_resolve(host, timeout=6):
    """Resolve `host` through Google Public DNS. Returns a list of IPv4 addresses."""
    with _cache_lock:
        if host in _cache:
            return _cache[host]
    ips = []
    try:
        req = urllib.request.Request(
            f"{GOOGLE_DNS_API}?name={urllib.parse.quote(host)}&type=A",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/dns-json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        ips = [a["data"] for a in data.get("Answer", [])
               if a.get("type") == 1 and a.get("data")]
    except Exception:
        ips = []
    with _cache_lock:
        _cache[host] = ips
    return ips


def system_resolves(host, timeout=3):
    """True if the machine's own resolver can look `host` up."""
    import socket
    from concurrent.futures import ThreadPoolExecutor
    def _lookup():
        try:
            socket.getaddrinfo(host, 443, socket.AF_INET)
            return True
        except Exception:
            return False
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_lookup).result(timeout=timeout)
    except Exception:
        return False


def build_host_resolver_rules(hosts=None, timeout=6, only_broken=True):
    """Chrome MAP rules pinning hosts to Google-resolved addresses.

    With `only_broken` (the default) a host is pinned ONLY when the machine's own
    resolver cannot look it up. That matters: pinning a CDN-hosted site to a
    Google-chosen edge IP was measured to break it (connection reset) on networks
    where system DNS works fine, so forcing it unconditionally trades a working
    setup for a broken one. Pinning stays reserved for the case it actually fixes --
    a resolver that returns nothing at all.
    """
    hosts = tuple(hosts) if hosts else DEFAULT_HOSTS
    from concurrent.futures import ThreadPoolExecutor
    if only_broken:
        with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
            hosts = tuple(h for h, ok in zip(hosts, ex.map(system_resolves, hosts)) if not ok)
        if not hosts:
            return ""     # system DNS handles everything; change nothing
    results = {}
    try:
        with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
            for host, ips in zip(hosts, ex.map(lambda h: google_resolve(h, timeout), hosts)):
                if ips:
                    results[host] = ips[0]
    except Exception:
        return ""
    return ", ".join(f"MAP {h} {ip}" for h, ip in results.items())


def apply_dns_flags(options, hosts=None, only_broken=True):
    """Give Chrome Google-resolved addresses for any host the system can't resolve.

    Pass only_broken=False to pin every host regardless -- see the warning in
    build_host_resolver_rules before doing so.
    """
    try:
        rules = build_host_resolver_rules(hosts, only_broken=only_broken)
    except Exception:
        return ""     # never let DNS helping stop a browser from starting
    if rules:
        options.add_argument(f"--host-resolver-rules={rules}")
    return rules

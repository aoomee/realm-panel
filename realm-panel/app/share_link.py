"""
Parse and rewrite proxy share links so they point at a relay entry.

Given the exit machine's original link (e.g. vless://uuid@exit-ip:443?...),
`rewrite(link, relay_host, relay_port, suffix)` returns the same link with
the server address swapped for the relay's host:port. Everything else
(uuid, password, sni, reality keys, transport params) is preserved.

If the original server was a domain and the link carries no explicit SNI,
the domain is written into sni/host so TLS keeps validating after the
address changes to the relay's IP.

Supported: vless, vmess (base64 JSON), trojan, ss (SIP002 + legacy),
hysteria2/hy2, hysteria, tuic, anytls, socks/socks5, http/https.
"""
import base64
import ipaddress
import json
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

URI_SCHEMES = {"vless", "trojan", "hysteria2", "hy2", "hysteria", "tuic",
               "anytls", "socks", "socks5", "http", "https", "ss"}


def _b64decode(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _is_domain(host: str) -> bool:
    if not host:
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        return True


def _fmt_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def parse(link: str) -> dict | None:
    """Return {scheme, host, port, name} for a share link, or None."""
    link = (link or "").strip()
    if "://" not in link:
        return None
    scheme = link.split("://", 1)[0].lower()

    if scheme == "vmess":
        try:
            j = json.loads(_b64decode(link[8:].split("#", 1)[0]))
            return {"scheme": "vmess", "host": str(j.get("add", "")),
                    "port": str(j.get("port", "")), "name": j.get("ps", "")}
        except Exception:
            return None

    if scheme not in URI_SCHEMES:
        return None

    u = urlsplit(link)
    host, port = u.hostname or "", u.port
    if scheme == "ss" and not host:
        # legacy ss://base64(method:pass@host:port)#name
        try:
            decoded = _b64decode(u.netloc).decode()
            hp = decoded.rsplit("@", 1)[1]
            host, _, p = hp.rpartition(":")
            port = int(p)
        except Exception:
            return None
    return {"scheme": scheme, "host": host, "port": str(port or ""),
            "name": unquote(u.fragment or "")}


def rewrite(link: str, relay_host: str, relay_port, suffix: str = "") -> str | None:
    """Point `link` at relay_host:relay_port. Returns None if unsupported."""
    info = parse(link)
    if not info:
        return None
    relay_port = str(relay_port)
    orig_host = info["host"]
    link = link.strip()
    scheme = info["scheme"]

    if scheme == "vmess":
        j = json.loads(_b64decode(link[8:].split("#", 1)[0]))
        j["add"] = relay_host
        j["port"] = relay_port
        if _is_domain(orig_host) and j.get("tls") in ("tls", "reality"):
            j.setdefault("sni", "")
            if not j["sni"]:
                j["sni"] = orig_host
        if _is_domain(orig_host) and j.get("net") in ("ws", "h2", "http", "httpupgrade") and not j.get("host"):
            j["host"] = orig_host
        if suffix:
            j["ps"] = f'{j.get("ps", "")}{suffix}'
        raw = json.dumps(j, ensure_ascii=False, separators=(",", ":")).encode()
        return "vmess://" + base64.b64encode(raw).decode()

    u = urlsplit(link)

    if scheme == "ss" and not u.hostname:
        decoded = _b64decode(u.netloc).decode()
        cred = decoded.rsplit("@", 1)[0]
        netloc = base64.urlsafe_b64encode(
            f"{cred}@{_fmt_host(relay_host)}:{relay_port}".encode()).decode().rstrip("=")
        name = info["name"] + suffix
        return f"ss://{netloc}" + (f"#{quote(name)}" if name else "")

    userinfo = u.netloc.rsplit("@", 1)[0] + "@" if "@" in u.netloc else ""
    netloc = f"{userinfo}{_fmt_host(relay_host)}:{relay_port}"

    q = parse_qsl(u.query, keep_blank_values=True)
    keys = {k for k, _ in q}
    if _is_domain(orig_host) and scheme not in ("ss", "socks", "socks5", "http"):
        # keep TLS / ws Host pointing at the real domain
        if "sni" not in keys and "peer" not in keys:
            sec = dict(q).get("security", "")
            if scheme != "vless" or sec in ("tls", "reality", "xtls"):
                q.append(("sni", orig_host))
        if dict(q).get("type") in ("ws", "httpupgrade", "xhttp", "h2") and "host" not in keys:
            q.append(("host", orig_host))

    name = info["name"] + suffix
    return urlunsplit((u.scheme, netloc, u.path, urlencode(q, safe=",:/"),
                       quote(name) if name else ""))

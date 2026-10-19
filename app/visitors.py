"""Decide which visits are real people, without storing an IP address or a raw user agent.

A view is counted only when the browser calls us after the page has been open. This module
labels that call (browser, bot, headless, a hosting network, the team, and so on).
"""
import hashlib
import hmac
import ipaddress
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
# The box that was inflating the counter. Override or clear with TEAM_IPS.
_DEFAULT_TEAM = "104.28.208.14"
_CIDR_FILE = Path(__file__).with_name("datacenter_cidrs.txt")

# Order matters: headless and preview are checked before the generic "bot" word.
_HEADLESS_RE = re.compile(r"headless|playwright|puppeteer|phantomjs|selenium|webdriver", re.I)
_PREVIEW_RE = re.compile(
    r"twitterbot|applebot|slackbot|discordbot|telegrambot|whatsapp|linkedinbot|"
    r"facebookexternalhit|facebot|embedly|iframely|quora|pinterest|redditbot|"
    r"skypeuripreview|bitlybot|snapchat|vkshare|google-read-aloud|preview", re.I)
_SCRIPT_RE = re.compile(
    r"curl|wget|python|httpx|requests|go-http|java/|axios|node-fetch|scrapy|"
    r"libwww|okhttp|aiohttp|go-http-client|monitor|uptime|pingdom|lighthouse", re.I)
_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|gptbot|chatgpt-user|claudebot|claude-user|bytespider|"
    r"ccbot|amazonbot|petalbot|semrush|ahrefs|mj12bot|dotbot|perplexity|"
    r"meta-externalagent|imagesift|dataforseo|serpstat|yandex|baiduspider|duckduck|feed", re.I)


def eastern_day(when=None):
    """Calendar day in US Eastern time. `when` is a Unix timestamp or an aware datetime."""
    if when is None:
        dt = datetime.now(EASTERN)
    elif isinstance(when, datetime):
        dt = when.astimezone(EASTERN)
    else:
        dt = datetime.fromtimestamp(when, EASTERN)
    return dt.date().isoformat()


def ip_hash(ip, day):
    """Daily-salted hash. The same address differs by day, and the address is not recoverable."""
    salt = os.environ.get("VIEW_SALT") or "output-view-salt"
    digest = hmac.new(f"{salt}|{day}".encode(), (ip or "").encode(), hashlib.sha256).hexdigest()
    return digest[:16]


def ua_class(ua):
    """browser, headless, preview, script, or bot. Only browser can be counted."""
    if not ua or not ua.strip():
        return "script"
    if _HEADLESS_RE.search(ua):
        return "headless"
    if _PREVIEW_RE.search(ua):
        return "preview"
    if _SCRIPT_RE.search(ua):
        return "script"
    if _BOT_RE.search(ua):
        return "bot"
    return "browser"


def _parse_nets(raw):
    nets = []
    for part in re.split(r"[\s,]+", raw or ""):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets


@lru_cache(maxsize=8)
def _team_networks(raw):
    return tuple(_parse_nets(raw))


def team_networks():
    """TEAM_IPS replaces the default. An empty TEAM_IPS counts nobody as team."""
    raw = os.environ.get("TEAM_IPS")
    if raw is None:
        raw = _DEFAULT_TEAM
    return _team_networks(raw)


def _load_datacenter():
    wide4, by16, wide6, by32 = [], {}, [], {}
    try:
        text = _CIDR_FILE.read_text()
    except OSError:
        return wide4, by16, wide6, by32
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            net = ipaddress.ip_network(line, strict=False)
        except ValueError:
            continue
        if net.version == 4:
            if net.prefixlen < 16:
                wide4.append(net)
            else:
                start = int(net.network_address) >> 16
                end = int(net.broadcast_address) >> 16
                for bucket in range(start, end + 1):
                    by16.setdefault(bucket, []).append(net)
        else:
            if net.prefixlen < 32:
                wide6.append(net)
            else:
                start = int(net.network_address) >> 96
                end = int(net.broadcast_address) >> 96
                for bucket in range(start, min(end, start + 64) + 1):
                    by32.setdefault(bucket, []).append(net)
                if end > start + 64:
                    wide6.append(net)
    return wide4, by16, wide6, by32


_WIDE4, _BY16, _WIDE6, _BY32 = _load_datacenter()


def _ip(value):
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.split("%", 1)[0].strip())
    except ValueError:
        return None


def is_datacenter(ip):
    addr = _ip(ip)
    if addr is None or addr.is_private or addr.is_loopback or addr.is_reserved:
        return False
    if addr.version == 4:
        n = int(addr)
        for net in _WIDE4:
            if addr in net:
                return True
        for net in _BY16.get(n >> 16, ()):
            if addr in net:
                return True
        return False
    n = int(addr)
    for net in _WIDE6:
        if addr in net:
            return True
    for net in _BY32.get(n >> 96, ()):
        if addr in net:
            return True
    return False


def is_team_ip(ip):
    addr = _ip(ip)
    if addr is None:
        return False
    return any(addr in net for net in team_networks())


def classify_visit(ip, ua, country, opted_out):
    """(label, counted). counted is true only for a normal browser that is not us and not a hosting network."""
    if opted_out:
        return "optout", False
    if is_team_ip(ip):
        return "team", False
    kind = ua_class(ua)
    if kind != "browser":
        return kind, False
    if (country or "").upper() == "T1":
        return "tor", False
    if is_datacenter(ip):
        return "datacenter", False
    return "browser", True

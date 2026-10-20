"""Исходящие запросы по адресу, который ввели на странице настройки: только наружу и только https.

Зачем. Адрес API своей модели владелец может ввести на странице настройки. Тот, кто завладел
сессией страницы, тем же полем направил бы сервис на внутренний адрес: метаданные облака
(169.254.169.254), дашборд Hermes, базу, соседний контейнер — запрос ушёл бы изнутри сервера и
с ключом модели в заголовке. Поэтому адрес со страницы проходит два фильтра:

  * `check_url` — по записи адреса, до всякой сети: только `https`, без имени и пароля, без
    запроса и фрагмента; узел — не `localhost`, не имя без точки (так называются соседние
    контейнеры), не имя внутренней зоны и не IP-адрес закрытой сети;
  * `PinnedTransport` — в момент каждого запроса: имя разрешается заново, КАЖДЫЙ полученный
    адрес сверяется с перечнем закрытых сетей, и соединение идёт на проверенный адрес, а не на
    имя. Подмена записи DNS между проверкой и соединением (DNS rebinding) ничего не даёт:
    второго разрешения имени нет. Сертификат при этом проверяется по имени узла (SNI), как
    обычно. Перенаправления клиент модели не выполняет вовсе.

Закрытые сети: loopback, частные (10/8, 172.16/12, 192.168/16, fc00::/7), link-local
(169.254/16, fe80::/10 — там же метаданные облаков), CGNAT (100.64/10), multicast,
зарезервированные и неуказанные адреса, а также адреса IPv6, в которые вложен адрес IPv4:
IPv4-mapped (::ffff:a.b.c.d), IPv4-compatible (::a.b.c.d), 6to4, Teredo, NAT64 — они
проверяются по вложенному адресу.

Через прокси (`EGRESS_PROXY_URL`). Имя всё равно разрешает и проверяет сам сервис; не
разрешилось — запрос не уходит (отказ, а не доверие прокси). Прокси SOCKS получает уже
проверенный адрес. Прокси HTTP получает имя: клиент httpx в туннеле CONNECT проверяет сертификат
по тому, что стоит в адресе, и с адресом вместо имени соединение не установилось бы. Значит,
за прокси HTTP имя второй раз разрешает сам прокси, и от подмены DNS в этот промежуток защищает
уже он — это остаточный риск, он записан в docs.

Адрес из окружения сервиса (`SHTURMAN_LLM_BASE_URL`) этим фильтром не ограничен: его задаёт
оператор сервера, и там может стоять локальная модель.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable
from urllib.parse import SplitResult, urlsplit

import httpx

RESOLVE_SECONDS = 5.0
_INNER_ZONES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa", ".localdomain",
                ".corp", ".home", ".test", ".invalid", ".onion")
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_V4_COMPATIBLE = ipaddress.ip_network("::/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class Blocked(Exception):
    """Адрес не подходит. `code`: not_https — не https; bad_address — запись адреса неверна;
    blocked_address — узел во внутренней сети; dns_failed — имя не разрешилось."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _embedded(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Адрес IPv4, вложенный в адрес IPv6, если он там есть."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if any(ip in net for net in _NAT64) or (ip in _V4_COMPATIBLE and int(ip) > 1):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def blocked_ip(value: str | IPAddress) -> bool:
    """Закрыт ли адрес для запросов со страницы. Неразборчивый адрес закрыт."""
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0]) if isinstance(value, str) else value
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded(ip)
        if inner is not None:
            return blocked_ip(inner)
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or getattr(ip, "is_site_local", False) or not ip.is_global)


def literal_ip(host: str) -> str | None:
    """Узел записан IP-адресом — возвращает его обычную запись; иначе None.

    Понимает и записи, которые ОС принимает за адрес IPv4, а `ipaddress` — нет: одним числом
    (2130706433), восьмеричную и шестнадцатеричную (0177.0.0.1, 0x7f.1). Такие записи закрыты
    всегда: пользоваться ими незачем, а разрешаются они в адрес мимо DNS."""
    bare = host.strip("[]")
    try:
        return str(ipaddress.ip_address(bare.split("%", 1)[0]))
    except ValueError:
        pass
    try:
        socket.inet_aton(bare)
    except (OSError, UnicodeError, ValueError):
        return None
    return "0.0.0.0"          # нестандартная запись адреса IPv4: закрыта, как неуказанный адрес


def check_url(url: str) -> SplitResult:
    """Проверка по записи адреса, без сети. Возвращает разобранный адрес или бросает `Blocked`."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise Blocked("bad_address") from None
    if parts.scheme != "https":
        raise Blocked("not_https" if parts.scheme in ("http", "") else "bad_address")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment \
            or port == 0 or "\\" in url or "@" in parts.netloc:
        raise Blocked("bad_address")
    ip = literal_ip(host)
    if ip is not None:
        if blocked_ip(ip):
            raise Blocked("blocked_address")
        return parts
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise Blocked("bad_address") from None
    if "." not in host or host == "localhost" or host.endswith(_INNER_ZONES):
        raise Blocked("blocked_address")
    return parts


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    found = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [item[4][0] for item in found]


# Чем разрешается имя. Тесты подменяют: в сеть они не ходят.
resolver: Callable[[str, int], Awaitable[list[str]]] = system_resolver


async def pin(host: str, port: int) -> str:
    """Адрес, с которым можно соединяться вместо имени `host`. Один закрытый адрес среди
    полученных закрывает имя целиком: иначе запись с двумя адресами прошла бы проверку одним,
    а соединилась бы с другим."""
    ip = literal_ip(host)
    if ip is not None:
        if blocked_ip(ip):
            raise Blocked("blocked_address")
        return ip
    try:
        found = await asyncio.wait_for(resolver(host, port), RESOLVE_SECONDS)
    except (OSError, asyncio.TimeoutError, UnicodeError):
        raise Blocked("dns_failed") from None
    addresses = list(dict.fromkeys(found))
    if not addresses:
        raise Blocked("dns_failed")
    if any(blocked_ip(address) for address in addresses):
        raise Blocked("blocked_address")
    return addresses[0].split("%", 1)[0]


def proxy_resolves_names(proxy_url: str) -> bool:
    """Прокси HTTP получает имя узла и разрешает его сам (см. шапку модуля)."""
    return urlsplit(proxy_url).scheme.lower() in ("http", "https") if proxy_url else False


class PinnedTransport(httpx.AsyncBaseTransport):
    """Обёртка над транспортом httpx: каждый запрос уходит на адрес, проверенный в эту минуту.

    connect_by_name — за прокси HTTP: адрес проверяется так же, но в запросе остаётся имя."""

    def __init__(self, inner: httpx.AsyncBaseTransport, *, connect_by_name: bool = False) -> None:
        self.inner = inner
        self.connect_by_name = connect_by_name

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme != "https":
            raise Blocked("not_https")
        host = url.host
        try:
            check_url(f"https://{url.netloc.decode('ascii')}")
        except UnicodeError:
            raise Blocked("bad_address") from None
        address = await pin(host, url.port or 443)
        if literal_ip(host) is None and not self.connect_by_name:
            # Соединение — с проверенным адресом; имя остаётся в заголовке Host и в проверке
            # сертификата (SNI). Заголовок Host выставлен при сборке запроса и здесь не меняется.
            request.url = url.copy_with(host=address)
            request.extensions = {**request.extensions, "sni_hostname": host}
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()

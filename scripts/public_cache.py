"""Credential-free, read-only client for the fixed public pickup-stock cache.

This module never queries Apple, accepts server addresses from configuration, or
uses the caller's proxy environment. Unknown/stale data raises CacheError.
"""
import http.client
import json
import math
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime

URL = 'https://aivsbi.online/apple-stock/v1/stock.json'
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 64 * 1024
SUPPORTED_PARTS = {
    'MK2Q4CH/A': 'iPhone Duo 512GB 夜空色',
    'MK2W4CH/A': 'iPhone Duo 2TB 夜空色',
    'MK2P4CH/A': 'iPhone Duo 512GB 星光白色',
    'MK2M4CH/A': 'iPhone Duo 256GB 星光白色',
    'MK2U4CH/A': 'iPhone Duo 1TB 夜空色',
    'MK2N4CH/A': 'iPhone Duo 256GB 夜空色',
    'MK2V4CH/A': 'iPhone Duo 2TB 星光白色',
    'MK2T4CH/A': 'iPhone Duo 1TB 星光白色',
}


class CacheError(Exception):
    """Cache unavailable or unsafe to interpret; not an out-of-stock result."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def supports(cfg):
    """Only explicit R609 plus any nonempty subset of the eight cached SKUs."""
    if not isinstance(cfg, dict):
        return False
    stores, parts = cfg.get('stores'), cfg.get('parts')
    cities = cfg.get('cities', [])
    if cities is None:
        cities = []
    return (isinstance(stores, list) and len(stores) == 1
            and isinstance(stores[0], str) and stores[0].upper() == 'R609'
            and isinstance(cities, list) and all(c == '大连' for c in cities)
            and isinstance(parts, dict) and bool(parts)
            and all(p in SUPPORTED_PARTS for p in parts))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CacheError('服务器缓存包含重复字段；不会当成无货')
        result[key] = value
    return result


def read_public():
    """One bounded HTTPS GET; no credentials, redirects, proxies or retries."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(URL, headers={
        'Accept': 'application/json', 'User-Agent': 'apple-stock-watch-public-cache/1'})
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.geturl() != URL or response.status != 200:
                raise CacheError('公开库存入口返回异常或重定向；未回退直查官网')
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise CacheError('公开库存响应超过 64 KiB 上限；不会当成无货')
        return json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise CacheError('公开库存入口已限流（429），请等待下一轮，不要立即重试；未回退直查官网') from None
        if 300 <= exc.code < 400:
            raise CacheError('公开库存入口发生重定向，已拒绝；未回退直查官网') from None
        raise CacheError('公开库存入口暂不可用（HTTP {}）；未回退直查官网'.format(exc.code)) from None
    except (urllib.error.URLError, OSError, ValueError, UnicodeError,
            http.client.HTTPException, RecursionError):
        # Do not leak proxy settings, response bodies, certificates or local paths.
        raise CacheError('读取公开库存缓存失败；查不到不等于无货，未回退直查官网') from None


def _fresh(stamp, now, age_limit):
    return (not isinstance(stamp, bool) and isinstance(stamp, (int, float))
            and math.isfinite(stamp) and -15 <= now - stamp / 1000 <= age_limit)


def convert(data, cfg, now=None):
    """Validate the complete snapshot, then return only the configured subset.

    ``now`` is Unix seconds (like time.time); source timestamps are milliseconds.
    """
    if not supports(cfg):
        raise CacheError('公开库存目前仅覆盖大连恒隆 R609 的 iPhone Duo 八种配置；未回退直查官网')
    now = time.time() if now is None else now
    try:
        if (isinstance(now, bool) or not isinstance(now, (int, float))
                or not math.isfinite(now)):
            raise CacheError('库存校验时间异常')
        if (not isinstance(data, dict) or type(data.get('schemaVersion')) is not int
                or data['schemaVersion'] != 1
                or data.get('source') != 'apple-stock-public-cache'
                or data.get('healthy') is not True or data.get('workerAlive') is not True
                or data.get('workerError') is not None):
            raise CacheError('服务器库存未知或查询器异常；查不到不等于无货')
        if (type(data.get('cycles')) is not int or data['cycles'] < 0
                or type(data.get('freshnessLimitSeconds')) is not int
                or data['freshnessLimitSeconds'] != 120):
            raise CacheError('服务器缓存版本或周期格式异常；不会当成无货')
        if not _fresh(data.get('generatedAtMs'), now, 45):
            raise CacheError('公开库存缓存已过期或时间异常；不会当成无货')
        rows = data['snapshot']
        if not isinstance(rows, list) or len(rows) != len(SUPPORTED_PARTS):
            raise CacheError('服务器缓存型号覆盖不完整；不会当成无货')
        seen, selected = set(), {}
        for row in rows:
            target = row['target']
            part = target['partNumber']
            if (target['locale'] != 'zh_CN' or target['storeNumber'] != 'R609'
                    or part not in SUPPORTED_PARTS or part in seen
                    or not isinstance(target.get('storeTitle'), str)
                    or not 0 < len(target['storeTitle']) <= 120
                    or not isinstance(target.get('productName'), str)
                    or not 0 < len(target['productName']) <= 200):
                raise CacheError('服务器缓存门店、地区或型号覆盖异常；不会当成无货')
            seen.add(part)
            stamp = row['lastCheckedMs']
            if not _fresh(stamp, now, 120):
                raise CacheError('服务器库存数据已过期或时间异常；不会当成无货')
            kind = row['availability']['kind']
            if kind not in ('in_stock', 'out_of_stock'):
                raise CacheError('服务器当前查不到完整库存；不会当成无货')
            if part in cfg['parts']:
                checked = datetime.fromtimestamp(stamp / 1000).strftime('%Y-%m-%d %H:%M:%S')
                selected[part] = {
                    'state': 'available' if kind == 'in_stock' else 'unavailable',
                    'quote': '官网到店取货，库存查询时间 ' + checked,
                    'title': SUPPORTED_PARTS[part], 'last_checked_ms': stamp,
                    'source_cycle': data['cycles'], 'source': 'public_cache'}
        return [{'number': 'R609', 'name': '大连恒隆广场', 'city': '大连', 'parts': selected}]
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise CacheError('服务器缓存格式异常；未回退直查官网') from None


def query(cfg):
    if not supports(cfg):
        raise CacheError('公开库存尚不支持所选门店或型号；未回退直查官网')
    return convert(read_public(), cfg)

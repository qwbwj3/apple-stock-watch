#!/usr/bin/env python3
"""Apple Store 到店取货库存监控：只查询、只提醒，不加购、不下单。

仅用 Python 标准库。数据来自 Apple 中国官网选购页自己使用的公开查询地址。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://www.apple.com.cn"
PICKUP_URL = BASE + "/shop/retail/pickup-message"
STORELIST_URL = BASE + "/retail/storelist/"
BUY_INDEX_URL = BASE + "/shop/buy-iphone"
USER_AGENT = "apple-stock-watch/1.0 (personal pickup alert; stdlib urllib)"
DEFAULT_LOCATION = "北京 北京 东城区"
MIN_INTERVAL = 30
BLOCK_COOLDOWN = 900  # 被官网拦截后停 15 分钟再查；拦截期间继续发请求只会更久
PARTS_PER_REQUEST = 10

# 选购页在开售前不放出型号代码，这里内置一份，开售后以官网为准。
BUILTIN_PARTS = {
    "iphone-duo": [
        "MK2N4CH/A", "MK2M4CH/A", "MK2Q4CH/A", "MK2P4CH/A",
        "MK2U4CH/A", "MK2T4CH/A", "MK2W4CH/A", "MK2V4CH/A",
    ],
}

STATE_LABEL = {
    "available": "有货",
    "unavailable": "无货",
    "ineligible": "未开放",
    "unknown": "查不到",
}


class QueryError(Exception):
    """查询失败。失败不等于无货，调用方必须把它当成“查不到”。"""


def home() -> Path:
    p = Path(os.environ.get("APPLE_STOCK_WATCH_HOME", Path.home() / ".apple-stock-watch"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)


def http_get(url: str, params: list[tuple[str, str]] | None = None, timeout: int = 20) -> bytes:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            elif enc and enc != "identity":
                raise QueryError(f"官网用了读不了的压缩格式：{enc}")
            return data
    except urllib.error.HTTPError as e:
        if e.code == 541:
            raise QueryError("官网暂时拦截了查询（HTTP 541），多半是查得太频繁，过几分钟再试") from e
        raise QueryError(f"官网返回 HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise QueryError(f"网络不通：{getattr(e, 'reason', e)}") from e


def query_pickup(parts: list[str], location: str | None = None, store: str | None = None) -> list[dict]:
    """返回门店列表：[{number, name, city, parts: {part: {state, quote, title}}}]"""
    if not parts:
        raise QueryError("没有要查询的型号")
    if not location and not store:
        raise QueryError("没有指定地点")
    merged: dict[str, dict] = {}
    for i in range(0, len(parts), PARTS_PER_REQUEST):
        chunk = parts[i:i + PARTS_PER_REQUEST]
        params = [("pl", "true"), ("mts.0", "regular")]
        params += [(f"parts.{n}", p) for n, p in enumerate(chunk)]
        params.append(("store", store) if store else ("location", location))
        raw = http_get(PICKUP_URL, params)
        try:
            body = json.loads(raw).get("body", {})
        except (ValueError, AttributeError) as e:
            raise QueryError("官网返回的不是预期的数据格式") from e
        stores = body.get("stores") or []
        if not stores:
            reason = body.get("errorMessage") or "这个地点附近没有查到 Apple Store"
            raise QueryError(str(reason))
        for s in stores:
            num = s.get("storeNumber")
            if not num:
                continue
            entry = merged.setdefault(num, {
                "number": num,
                "name": (s.get("storeName") or "").strip(),
                "city": (s.get("city") or "").strip(),
                "parts": {},
            })
            avail = s.get("partsAvailability") or {}
            for p in chunk:
                info = avail.get(p)
                if not isinstance(info, dict) or "pickupDisplay" not in info:
                    entry["parts"][p] = {"state": "unknown", "quote": "官网没有返回这个型号", "title": ""}
                    continue
                state = info.get("pickupDisplay")
                if state not in ("available", "unavailable", "ineligible"):
                    state = "unknown"
                regular = (info.get("messageTypes") or {}).get("regular") or {}
                entry["parts"][p] = {
                    "state": state,
                    "quote": (info.get("pickupSearchQuote") or "").strip(),
                    "title": (regular.get("storePickupProductTitle") or "").strip(),
                }
    return list(merged.values())


def filter_stores(stores: list[dict], cities: list[str], numbers: list[str]) -> list[dict]:
    out = stores
    if numbers:
        wanted = {n.upper() for n in numbers}
        out = [s for s in out if s["number"].upper() in wanted]
    if cities:
        out = [s for s in out if any(c in s["city"] for c in cities)]
    return out


def fetch_store_directory() -> dict[str, dict]:
    """从官网门店列表页读出中国大陆门店：{城市: {province, stores: [{number, name, address}]}}"""
    html = http_get(STORELIST_URL).decode("utf-8", "ignore")
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise QueryError("官网门店列表页的格式变了，读不出门店")
    try:
        locales = json.loads(m.group(1))["props"]["pageProps"]["storeList"]
    except (ValueError, KeyError, TypeError) as e:
        raise QueryError("官网门店列表页的格式变了，读不出门店") from e
    cities: dict[str, dict] = {}
    for loc in locales:
        if not isinstance(loc, dict) or loc.get("locale") != "zh_CN":
            continue
        for state in loc.get("state") or loc.get("states") or []:
            for s in state.get("store") or state.get("stores") or []:
                addr = s.get("address") or {}
                city = (addr.get("city") or "").strip()
                num = (s.get("id") or "").strip()
                if not city or not num:
                    continue
                entry = cities.setdefault(city, {
                    "province": (addr.get("stateName") or state.get("name") or city).strip(),
                    "stores": [],
                })
                entry["stores"].append({
                    "number": num,
                    "name": (s.get("name") or "").strip(),
                    "address": (addr.get("address1") or "").strip(),
                })
    if not cities:
        raise QueryError("官网门店列表页里没有读到中国大陆门店")
    return cities


def match_city(directory: dict[str, dict], name: str) -> str | None:
    name = name.strip().rstrip("市")
    if name in directory:
        return name
    hits = [c for c in directory if name and (name in c or c in name)]
    return hits[0] if len(hits) == 1 else None


def district_candidates(city: str, info: dict) -> list[str]:
    """从门店地址里取出区县名，用来拼官网要求的“省 市 区”。"""
    out: list[str] = []
    for s in info["stores"]:
        addr = s["address"]
        for prefix in (info["province"] + "省", info["province"], city + "市", city, "市"):
            if addr.startswith(prefix):
                addr = addr[len(prefix):]
        m = re.match(r"(.{1,6}?(?:新区|区|县|市))", addr)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def location_cache() -> dict:
    p = home() / "locations.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def resolve_city(city_arg: str, probe_parts: list[str]) -> tuple[str, str, list[dict], list[dict]]:
    """返回 (城市名, “省 市 区”或空串, 该城市门店目录, 本次查到的门店结果)。

    只有一家店的城市直接按门店编号查，地点留空；多家店的城市用“省 市 区”一次查全。
    每一步都只发一次请求，查到的结果交给调用方复用，避免配置过程重复查询。
    """
    directory = fetch_store_directory()
    city = match_city(directory, city_arg)
    if not city:
        have = "、".join(sorted(directory))
        raise QueryError(f"“{city_arg}”没有 Apple Store。有门店的城市：{have}")
    info = directory[city]
    if len(info["stores"]) == 1:
        found = query_pickup(probe_parts, store=info["stores"][0]["number"])
        return city, "", info["stores"], found
    province = re.sub(r"(壮族|维吾尔|回族)?自治区$|省$|市$", "", info["province"]) or info["province"]
    cache = location_cache()
    candidates = [f"{province} {city} {d}" for d in district_candidates(city, info)[:3]]
    if cache.get(city) in candidates:
        candidates.remove(cache[city])
    if cache.get(city):
        candidates.insert(0, cache[city])
    last = "这个城市的地点格式官网不认"
    for n, location in enumerate(candidates):
        if n:
            time.sleep(3)  # 连续试几个区县时放慢，避免被官网当成刷接口
        try:
            found = query_pickup(probe_parts, location=location)
        except QueryError as e:
            last = str(e)
            if "541" in last:
                break  # 被拦截时再试只会更糟
            continue
        if any(city in s["city"] or s["city"] in city for s in found):
            cache[city] = location
            (home() / "locations.json").write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            return city, location, info["stores"], found
    raise QueryError(f"{city} 的门店查不到：{last}")


def fetch_families() -> list[str]:
    html = http_get(BUY_INDEX_URL).decode("utf-8", "ignore")
    out: list[str] = []
    for s in re.findall(r"/shop/buy-iphone/(iphone-[a-z0-9-]+)", html):
        if s not in out:
            out.append(s)
    if not out:
        raise QueryError("官网选购页的格式变了，读不出机型")
    return out


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-\u00a0]+", "", s).lower()


def fetch_catalog(slug: str) -> list[dict]:
    """只读选购页，返回 [{part, model, capacity, color}]，不发库存查询。读不出来时返回空列表。"""
    try:
        html = http_get(f"{BASE}/shop/buy-iphone/{slug}").decode("utf-8", "ignore")
    except QueryError:
        return []
    items = re.findall(
        r'\{"sku":"[^"]+","partNumber":"([A-Z0-9]+CH/A)","price":\{[^}]*\},"category":"iphone","name":"([^"]+)"\}',
        html)
    zh: list[str] = []
    for c in re.findall(r'<span class="dimensionColor">\s*([^<]+?)\s*</span>', html):
        if c not in zh:
            zh.append(c)
    en: list[str] = []
    for c in re.findall(r'"dimensionColor":"([^"]+)"', html):
        if c not in en:
            en.append(c)
    color_zh = {_norm(k): v for k, v in zip(en, zh)} if len(en) == len(zh) else {}
    out: list[dict] = []
    seen: set[str] = set()
    for part, name in items:
        if part in seen:
            continue
        seen.add(part)
        name = name.replace("\u00a0", " ").strip()
        m = re.match(r"(.+?)\s+(\d+\s?(?:GB|TB))\s+(.+)$", name)
        if not m:
            continue
        out.append({
            "part": part,
            "model": m.group(1).strip(),
            "capacity": m.group(2).replace(" ", "").upper(),
            "color": color_zh.get(_norm(m.group(3)), m.group(3).strip()),
        })
    return out


def uniq(seq):
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
    return out


def cap_sort(caps: list[str]) -> list[str]:
    def key(c):
        n = int(re.match(r"\d+", c).group(0))
        return n * (1024 if c.endswith("TB") else 1)
    return sorted(caps, key=key)


def select_parts(catalog: list[dict], model: str | None, capacities: list[str], colors: list[str]) -> list[dict]:
    """按用户说的机型、容量、颜色从目录里挑配置；说不清时报错并列出可选项。"""
    models = uniq(c["model"] for c in catalog)
    if model:
        hit = [m for m in models if _norm(m) == _norm(model)]
        if not hit:
            raise QueryError(f"没有“{model}”这个机型。可选：{' / '.join(models)}")
        rows = [c for c in catalog if c["model"] == hit[0]]
    elif len(models) > 1:
        raise QueryError(f"这个选购页里有 {len(models)} 个机型，要指定一个：{' / '.join(models)}")
    else:
        rows = list(catalog)
    all_caps = cap_sort(uniq(c["capacity"] for c in rows))
    all_colors = uniq(c["color"] for c in rows)
    if capacities:
        want = {_norm(x).upper().replace("G", "GB").replace("GBB", "GB").replace("T", "TB").replace("TBB", "TB") for x in capacities}
        bad = [x for x in want if x not in all_caps]
        if bad:
            raise QueryError(f"没有这个容量：{'、'.join(bad)}。可选：{' / '.join(all_caps)}")
        rows = [c for c in rows if c["capacity"] in want]
    if colors:
        norm_colors = {_norm(x): x for x in all_colors}
        want_c = []
        for x in colors:
            k = _norm(x)
            match = [v for kk, v in norm_colors.items() if kk == k or k in kk or kk.rstrip("色") == k]
            if len(match) != 1:
                raise QueryError(f"颜色“{x}”对不上。可选：{' / '.join(all_colors)}")
            want_c.append(match[0])
        rows = [c for c in rows if c["color"] in want_c]
    if not rows:
        raise QueryError("按这个机型、容量、颜色的组合，没有对应的配置")
    return rows


def split_title(title: str) -> tuple[str, str, str]:
    """“iPhone Duo 256GB 星光白色” → (机型, 容量, 颜色)"""
    m = re.match(r"(.+?)\s+(\d+\s?(?:GB|TB))\s+(.+)$", title)
    return (m.group(1), m.group(2).replace(" ", ""), m.group(3)) if m else (title, "", "")


def family_slug(arg: str) -> str:
    m = re.search(r"/shop/buy-[a-z]+/([a-z0-9-]+)", arg)
    return m.group(1) if m else arg.strip().lower().replace(" ", "-")


def fetch_family_parts(slug: str) -> tuple[list[str], str]:
    """返回 (型号代码列表, 来源)。"""
    url = f"{BASE}/shop/buy-iphone/{slug}"
    found: list[str] = []
    try:
        html = http_get(url).decode("utf-8", "ignore")
        found = sorted(set(re.findall(r'"partNumber":"([A-Z0-9]+CH/A)"', html)))
    except QueryError:
        found = []
    if found:
        return found, "官网选购页"
    if slug in BUILTIN_PARTS:
        return list(BUILTIN_PARTS[slug]), "内置列表（官网选购页暂未放出）"
    return [], "未找到"


def part_titles(parts: list[str], location: str) -> dict[str, str]:
    titles: dict[str, str] = {}
    stores = query_pickup(parts, location=location)
    for s in stores:
        for p, info in s["parts"].items():
            if info["title"] and p not in titles:
                titles[p] = info["title"]
    return titles


# ---------- 配置 ----------

def config_path() -> Path:
    return home() / "config.json"


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        sys.exit("还没有监控配置。先运行 init。")
    return json.loads(p.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    p = config_path()
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(p, 0o600)


def mask(secret: str) -> str:
    if not secret:
        return "未设置"
    tail = secret.rstrip("/").rsplit("/", 1)[-1]
    return f"已设置（…{tail[-4:]}）"


# ---------- 通知 ----------

def notify(cfg: dict, title: str, body: str, url: str = "", urgent: bool = True) -> list[str]:
    """发通知，返回成功的渠道名。任何渠道失败都不抛异常。"""
    sent: list[str] = []
    bark = (cfg.get("bark") or "").strip().rstrip("/")
    if bark:
        payload = {
            "title": title,
            "body": body,
            "group": "Apple库存",
            "level": "timeSensitive" if urgent else "passive",
        }
        if urgent:
            payload["sound"] = "alarm"
        if url:
            payload["url"] = url
        req = urllib.request.Request(
            bark, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if 200 <= resp.status < 300:
                    sent.append("Bark")
        except Exception as e:  # noqa: BLE001 通知失败不能中断监控
            log(f"Bark 推送失败：{e}")
    if sys.platform == "darwin" and cfg.get("local_notify", True) and shutil.which("osascript"):
        script = "display notification {} with title {}{}".format(
            json.dumps(body, ensure_ascii=False), json.dumps(title, ensure_ascii=False),
            ' sound name "Glass"' if urgent else "")
        try:
            subprocess.run(["osascript", "-e", script], timeout=10, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sent.append("本机通知")
        except Exception as e:  # noqa: BLE001
            log(f"本机通知失败：{e}")
    return sent


# ---------- 监控 ----------

def state_path() -> Path:
    return home() / "state.json"


def load_state() -> dict:
    p = state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def save_state(st: dict) -> None:
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path())


def check_once(cfg: dict) -> list[dict]:
    parts = list(cfg["parts"].keys())
    wanted = [n.upper() for n in (cfg.get("stores") or [])]
    stores: list[dict] = []
    if cfg.get("location"):
        stores = query_pickup(parts, location=cfg["location"])
    have = {s["number"].upper() for s in stores}
    errors = []
    for num in wanted:  # 单店城市直接按门店查；多店城市补查按地点没返回的门店
        if num not in have:
            try:
                stores += query_pickup(parts, store=num)
            except QueryError as e:
                errors.append(str(e))
    stores = filter_stores(stores, cfg.get("cities") or [], wanted)
    if not stores and errors:
        raise QueryError(errors[0])
    if not stores:
        raise QueryError("查到了门店，但没有一家在你选的城市或门店范围内")
    return stores


def summarize(stores: list[dict], names: dict[str, str]) -> str:
    lines = []
    for s in stores:
        for p, info in s["parts"].items():
            name = names.get(p) or info["title"] or p
            label = STATE_LABEL[info["state"]]
            extra = f"（{info['quote']}）" if info["quote"] else ""
            lines.append(f"{label}\t{s['city']} {s['name']}\t{name}{extra}")
    return "\n".join(lines)


def watch(cfg: dict) -> None:
    interval = max(MIN_INTERVAL, int(cfg.get("interval", 60)))
    names: dict[str, str] = cfg["parts"]
    buy_url = cfg.get("buy_url") or BASE + "/shop/buy-iphone"
    st = load_state()
    pairs: dict[str, dict] = st.get("pairs", {})
    fail_since = None
    fail_count = 0
    last_health_alert = 0.0
    health_alerted = False
    first_ok = True
    where = cfg.get("location") or "、".join(cfg.get("cities") or []) or "指定门店"
    log(f"开始监控：{where}，{len(names)} 个型号，每 {interval} 秒查一次")

    while True:
        tick = time.time()
        blocked = False
        try:
            stores = check_once(cfg)
        except QueryError as e:
            blocked = "541" in str(e)
            fail_count += 1
            fail_since = fail_since or tick
            log(f"查不到（第 {fail_count} 次）：{e}")
            st.update({"last_error": str(e), "last_error_at": now_str(), "fail_count": fail_count})
            save_state(st)
            if (fail_count >= 3 and tick - fail_since >= 600
                    and tick - last_health_alert >= 3600):
                mins = int((tick - fail_since) / 60)
                notify(cfg, "库存监控查不到数据", f"已经 {mins} 分钟查不到，原因：{e}。这段时间有没有货我不知道。",
                       urgent=False)
                last_health_alert = tick
                health_alerted = True
        else:
            if health_alerted:
                notify(cfg, "库存监控恢复正常", "又能查到数据了。", urgent=False)
            fail_since, fail_count, health_alerted = None, 0, False
            new_hits: list[tuple[dict, str, dict]] = []
            counts = {"available": 0, "unavailable": 0, "ineligible": 0, "unknown": 0}
            for s in stores:
                for p, info in s["parts"].items():
                    counts[info["state"]] += 1
                    key = f"{s['number']}|{p}"
                    prev = pairs.get(key, {})
                    cur = dict(prev)
                    if info["state"] == "available":
                        if not prev.get("on"):
                            cur = {"on": True, "since": tick, "miss": 0}
                            new_hits.append((s, p, info))
                        else:
                            cur["miss"] = 0
                    elif info["state"] in ("unavailable", "ineligible"):
                        if prev.get("on"):
                            cur["miss"] = prev.get("miss", 0) + 1
                            if cur["miss"] >= 2:
                                mins = max(1, int((tick - prev.get("since", tick)) / 60))
                                name = names.get(p) or info["title"] or p
                                notify(cfg, "没货了", f"{s['city']} {s['name']} 的 {name} 已经没货，这次放货持续约 {mins} 分钟。",
                                       urgent=False)
                                log(f"没货了：{s['name']} {name}（持续约 {mins} 分钟）")
                                cur = {"on": False}
                    # unknown：保持原状态，不当成无货
                    pairs[key] = cur
            if len(new_hits) > 5:
                by_model: dict[str, list[str]] = {}
                for s, p, info in new_hits:
                    by_model.setdefault(names.get(p) or info["title"] or p, []).append(s["name"])
                body = "\n".join(f"{m}：{'、'.join(v)}" for m, v in by_model.items())
                head = "现在就有货" if first_ok else "大量放货"
                notify(cfg, f"{head}：{len(new_hits)} 处", body, url=buy_url)
                log(f"{head}：{len(new_hits)} 处")
            else:
                for s, p, info in new_hits:
                    name = names.get(p) or info["title"] or p
                    quote = info["quote"] or "可取货"
                    notify(cfg, f"有货：{name}", f"{s['city']} {s['name']}，{quote}。点开直接去买。", url=buy_url)
                    log(f"有货：{s['city']} {s['name']} {name}（{quote}）")
            first_ok = False
            log("本轮：有货 {available}，无货 {unavailable}，未开放 {ineligible}，查不到 {unknown}".format(**counts))
            st.update({
                "pairs": pairs, "last_ok_at": now_str(), "last_counts": counts,
                "last_summary": summarize(stores, names), "fail_count": 0,
            })
            st.pop("last_error", None)
            save_state(st)
        if blocked:
            log(f"被官网拦截，暂停 {BLOCK_COOLDOWN // 60} 分钟后再查")
            time.sleep(BLOCK_COOLDOWN)
        else:
            time.sleep(max(5, interval * random.uniform(0.9, 1.1) - (time.time() - tick)))


# ---------- 后台运行 ----------

def pid_path() -> Path:
    return home() / "watch.pid"


def running_pid() -> int | None:
    p = pid_path()
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def cmd_start(_: argparse.Namespace) -> None:
    load_config()
    if running_pid():
        sys.exit(f"监控已经在运行（进程 {running_pid()}）。要换配置先 stop。")
    cmd = [sys.executable, str(Path(__file__).resolve()), "watch"]
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + cmd  # 防止电脑睡眠时停掉
    logf = open(home() / "watch.log", "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    pid_path().write_text(str(proc.pid))
    time.sleep(3)
    if proc.poll() is not None:
        sys.exit(f"启动后立刻退出了，看日志：{home() / 'watch.log'}")
    print(f"监控已在后台运行（进程 {proc.pid}）。日志：{home() / 'watch.log'}")
    print("注意：电脑关机或合盖后监控会停。")


def cmd_stop(_: argparse.Namespace) -> None:
    pid = running_pid()
    if not pid:
        print("监控没有在运行。")
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    pid_path().unlink(missing_ok=True)
    print("监控已停止。")


def cmd_status(_: argparse.Namespace) -> None:
    cfg = load_config()
    st = load_state()
    pid = running_pid()
    print("运行状态：" + (f"运行中（进程 {pid}）" if pid else "没有在运行"))
    print(f"城市：{'、'.join(cfg.get('cities') or []) or cfg.get('location') or '未指定'}  门店数：{len(cfg.get('stores') or []) or '按地点'}")
    print(f"型号：{'、'.join(cfg['parts'].values())}")
    print(f"间隔：{cfg.get('interval', 60)} 秒  手机推送：{mask(cfg.get('bark', ''))}")
    print(f"上次查到数据：{st.get('last_ok_at', '还没有')}")
    if st.get("last_error"):
        print(f"最近一次查不到：{st.get('last_error_at')}，{st['last_error']}（连续 {st.get('fail_count', 0)} 次）")
    if st.get("last_summary"):
        print("上次结果：")
        print(st["last_summary"])


# ---------- 命令 ----------

def cmd_models(a: argparse.Namespace) -> None:
    slug = family_slug(a.family)
    catalog = fetch_catalog(slug)
    if catalog:
        print(f"# {slug}，共 {len(catalog)} 个配置，来源：官网选购页")
        for model in uniq(c["model"] for c in catalog):
            rows = [c for c in catalog if c["model"] == model]
            caps = cap_sort(uniq(c["capacity"] for c in rows))
            colors = uniq(c["color"] for c in rows)
            print(f"# {model}｜容量：{' / '.join(caps)}｜颜色：{' / '.join(colors)}")
        for c in catalog:
            print(f"{c['part']}\t{c['model']} {c['capacity']} {c['color']}")
        return
    # 页面格式变了时的后备：从页面或内置列表拿代码，再向库存接口要名称（会占用查询额度）
    parts, source = fetch_family_parts(slug)
    if not parts:
        sys.exit(f"没找到 {slug} 的型号代码。检查机型名称，或直接给选购页链接。")
    titles = part_titles(parts, a.location)
    phones = [p for p in parts if titles.get(p)]
    if not phones:
        sys.exit(f"{slug} 的型号代码官网暂时都查不到名称，稍后再试。")
    print(f"# {slug}，共 {len(phones)} 个配置，来源：{source}")
    for p in phones:
        print(f"{p}\t{titles[p]}")


def cmd_cities(_: argparse.Namespace) -> None:
    directory = fetch_store_directory()
    by_prov: dict[str, list[str]] = {}
    for city, info in directory.items():
        by_prov.setdefault(info["province"], []).append(f"{city}（{len(info['stores'])} 家）")
    print(f"# 中国大陆有 Apple Store 的城市，共 {len(directory)} 个")
    for prov in sorted(by_prov):
        print(f"{prov}\t{'、'.join(by_prov[prov])}")


def cmd_families(_: argparse.Namespace) -> None:
    print("# 官网在售的 iPhone 机型（用作 models 命令的参数）")
    for s in fetch_families():
        print(s)


def cmd_stores(a: argparse.Namespace) -> None:
    """只读官网的门店列表页，不发库存查询。"""
    directory = fetch_store_directory()
    city = match_city(directory, a.city)
    if not city:
        have = "、".join(sorted(directory))
        raise QueryError(f"“{a.city}”没有 Apple Store。有门店的城市：{have}")
    stores = directory[city]["stores"]
    print(f"# {city}，共 {len(stores)} 家")
    for s in stores:
        print(f"{s['number']}\t{city}\t{s['name']}\t{s['address']}")


def cmd_init(a: argparse.Namespace) -> None:
    names: dict[str, str] = {}
    if a.family:
        slug = family_slug(a.family)
        catalog = fetch_catalog(slug)
        if not catalog:
            sys.exit(f"读不出 {slug} 的配置列表。用 models 命令看一下，或改用 --part 直接给型号代码。")
        chosen = select_parts(catalog, a.model, a.capacity or [], a.color or [])
        a.part = [c["part"] for c in chosen]
        names = {c["part"]: f"{c['model']} {c['capacity']} {c['color']}" for c in chosen}
        if not a.buy_url:
            a.buy_url = f"{BASE}/shop/buy-iphone/{slug}"
    if not a.part:
        sys.exit("要给 --family（机型）或 --part（型号代码）其中一个")
    parts = [p.strip().upper() for p in a.part]
    if a.location:
        stores = query_pickup(parts, location=a.location)
    else:
        if not a.city or len(a.city) != 1:
            sys.exit("没给 --location 时，要用 --city 给且只给一个城市")
        city, a.location, city_stores, stores = resolve_city(a.city[0], parts)
        a.city = [city]
        if not a.store:
            a.store = [s["number"] for s in city_stores]
    have = {s["number"].upper() for s in stores}
    for num in (a.store or []):
        if num.upper() not in have:
            try:
                stores += query_pickup(parts, store=num.upper())
            except QueryError:
                pass
    titles: dict[str, str] = {}
    for s in stores:
        for p, info in s["parts"].items():
            if info["title"]:
                titles.setdefault(p, info["title"])
    for p, n in names.items():
        titles.setdefault(p, n)
    missing = [p for p in parts if p not in titles]
    if missing:
        sys.exit(f"这些型号官网不认识：{'、'.join(missing)}。用 models 命令重新查。")
    kept = filter_stores(stores, a.city or [], a.store or [])
    if not kept:
        all_cities = "、".join(sorted({s["city"] for s in stores}))
        sys.exit(f"按你给的城市或门店过滤后一家都不剩。这个地点能查到的城市有：{all_cities}")
    old = json.loads(config_path().read_text(encoding="utf-8")) if config_path().exists() else {}
    cfg = {
        "location": a.location,
        "cities": a.city or [],
        "stores": [s.upper() for s in (a.store or [])],
        "parts": {p: titles[p] for p in parts},
        "interval": max(MIN_INTERVAL, a.interval),
        "bark": a.bark if a.bark is not None else old.get("bark", ""),
        "buy_url": a.buy_url or "",
        "local_notify": not a.no_local_notify,
    }
    save_config(cfg)
    state_path().unlink(missing_ok=True)
    print("配置已保存。")
    print(f"监控门店（{len(kept)} 家）：" + "、".join(f"{s['city']} {s['name']}" for s in kept))
    print("监控型号：" + "、".join(cfg["parts"].values()))
    print(f"手机推送：{mask(cfg['bark'])}")


def cmd_check(_: argparse.Namespace) -> None:
    cfg = load_config()
    try:
        stores = check_once(cfg)
    except QueryError as e:
        sys.exit(f"查不到：{e}（查不到不代表无货）")
    print(summarize(stores, cfg["parts"]))


def cmd_watch(_: argparse.Namespace) -> None:
    try:
        watch(load_config())
    except KeyboardInterrupt:
        log("已停止")


def cmd_notify_test(_: argparse.Namespace) -> None:
    cfg = load_config()
    sent = notify(cfg, "库存监控测试", "收到这条说明通知是通的。", url=cfg.get("buy_url") or BASE)
    print("已送达：" + ("、".join(sent) if sent else "没有任何渠道成功"))
    if not sent:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Apple Store 到店取货库存监控（只提醒，不下单）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("models", help="列出一个机型所有配置的型号代码")
    p.add_argument("family", help="机型，如 iphone-duo、iphone-18-pro，或选购页链接")
    p.add_argument("--location", default=DEFAULT_LOCATION)
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("cities", help="列出中国大陆有 Apple Store 的城市")
    p.set_defaults(fn=cmd_cities)

    p = sub.add_parser("families", help="列出官网在售的 iPhone 机型")
    p.set_defaults(fn=cmd_families)

    p = sub.add_parser("stores", help="列出一个城市的 Apple Store")
    p.add_argument("--city", required=True, help="城市名，如：深圳")
    p.set_defaults(fn=cmd_stores)

    p = sub.add_parser("init", help="写入监控配置")
    p.add_argument("--location", help="省 市 区（一般不用，给 --city 即可自动算出）")
    p.add_argument("--city", action="append", help="城市名；不给 --location 时只给一个")
    p.add_argument("--store", action="append", help="只看这些门店编号，可重复")
    p.add_argument("--family", help="机型，如 iphone-duo（families 命令里的名字）")
    p.add_argument("--model", help="一个选购页里有多个机型时指定，如 “iPhone 18 Pro Max”")
    p.add_argument("--capacity", action="append", help="容量，如 256GB，可重复；不给就是全部")
    p.add_argument("--color", action="append", help="颜色中文名，如 星光白色，可重复；不给就是全部")
    p.add_argument("--part", action="append", help="直接给型号代码，可重复（一般不用）")
    p.add_argument("--interval", type=int, default=60, help=f"查询间隔秒数，最小 {MIN_INTERVAL}")
    p.add_argument("--bark", help="Bark 推送地址，如 https://api.day.app/你的key")
    p.add_argument("--buy-url", help="通知里附带的购买链接")
    p.add_argument("--no-local-notify", action="store_true")
    p.set_defaults(fn=cmd_init)

    for name, fn, hlp in [
        ("check", cmd_check, "立刻查一次"),
        ("watch", cmd_watch, "前台持续监控"),
        ("start", cmd_start, "后台持续监控"),
        ("stop", cmd_stop, "停止后台监控"),
        ("status", cmd_status, "查看监控状态"),
        ("notify-test", cmd_notify_test, "发一条测试通知"),
    ]:
        p = sub.add_parser(name, help=hlp)
        p.set_defaults(fn=fn)

    a = ap.parse_args()
    try:
        a.fn(a)
    except QueryError as e:
        sys.exit(f"查不到：{e}")


if __name__ == "__main__":
    main()

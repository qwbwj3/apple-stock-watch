"""用模拟数据验证状态变化时的通知行为。运行：python3 tests/test_transitions.py"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["APPLE_STOCK_WATCH_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import apple_stock as m  # noqa: E402

P = "MK2M4CH/A"
CFG = {"location": "北京 北京 东城区", "parts": {P: "iPhone Duo 256GB 星光白色"}, "interval": 60}


def store(state):
    return [{"number": "R448", "name": "王府井", "city": "北京",
             "parts": {P: {"state": state, "quote": "", "title": ""}}}]


class Done(Exception):
    pass


SLEEPS = []


def run(script, tick_seconds=60):
    """script: 每一轮返回的状态，'ERR' 表示查询失败。返回 (通知标题列表)。"""
    sent, clock, it = [], [1_000_000.0], iter(script)

    def fake_check(_cfg):
        try:
            step = next(it)
        except StopIteration:
            raise Done
        if step == "ERR":
            raise m.QueryError("模拟失败")
        if step == "BLOCK":
            raise m.QueryError("官网暂时拦截了查询（HTTP 541）")
        return store(step)

    class FakeTime:
        @staticmethod
        def time():
            return clock[0]

        @staticmethod
        def sleep(s):
            SLEEPS.append(s)
            clock[0] += max(tick_seconds, s)

    m.check_once = fake_check
    m.notify = lambda cfg, title, body, url="", urgent=True: sent.append(title) or ["mock"]
    m.time = FakeTime
    m.state_path().unlink(missing_ok=True)
    try:
        m.watch(dict(CFG))
    except Done:
        pass
    return sent


def check(name, got, want):
    ok = got == want
    print(("通过" if ok else "失败"), name, "" if ok else f"\n   期望 {want}\n   实际 {got}")
    return ok


results = [
    check("一直无货不通知", run(["unavailable"] * 4), []),
    check("未开放不通知", run(["ineligible"] * 4), []),
    check("无货变有货通知一次", run(["unavailable", "available", "available", "available"]),
          ["有货：iPhone Duo 256GB 星光白色"]),
    check("未开放变有货也通知", run(["ineligible", "available"]), ["有货：iPhone Duo 256GB 星光白色"]),
    check("启动时已有货通知一次", run(["available", "available"]), ["有货：iPhone Duo 256GB 星光白色"]),
    check("有货后连续两轮无货才报没货",
          run(["available", "unavailable", "unavailable", "unavailable"]),
          ["有货：iPhone Duo 256GB 星光白色", "没货了"]),
    check("只缺一轮不报没货，回来也不重复报有货",
          run(["available", "unavailable", "available"]), ["有货：iPhone Duo 256GB 星光白色"]),
    check("查不到不当成没货", run(["available", "ERR", "ERR", "unknown", "available"]),
          ["有货：iPhone Duo 256GB 星光白色"]),
    check("没货后再次放货会再通知",
          run(["available", "unavailable", "unavailable", "available"]),
          ["有货：iPhone Duo 256GB 星光白色", "没货了", "有货：iPhone Duo 256GB 星光白色"]),
    check("连续查不到满 10 分钟报一次，恢复后报一次",
          run(["unavailable"] + ["ERR"] * 14 + ["unavailable"]),
          ["库存监控查不到数据", "库存监控恢复正常"]),
    check("短暂查不到不打扰", run(["unavailable", "ERR", "ERR", "unavailable"]), []),
]
SLEEPS.clear()
run(["unavailable", "BLOCK", "unavailable"])
results.append(check("被拦截后停 15 分钟再查", [s >= 900 for s in SLEEPS[:2]], [False, True]))
print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)

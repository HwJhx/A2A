"""对 herdr/a2a_codex 的复核探针(Claude 编写)。

只在临时目录里操作,不修改 a2a_codex 的任何文件。运行方式(仓库根目录):

    PYTHONPATH=herdr PYTHONDONTWRITEBYTECODE=1 python3 herdr/claude/review-probes/probe_a2a_codex.py

每一段输出对应 07-review-of-a2a_codex.md 里的一个编号(Q1~Q10)。
"""
import os, sys, json, time, tempfile, argparse
from pathlib import Path
from unittest import mock
import a2a_codex
from a2a_codex import AgentIdentity, Registry, Topology, TopologyStore, Spool, AuditLog, Router, SendRejected
from a2a_codex.messages import Message, new_msg_id, now_iso, QUEUED, DELIVERED
from a2a_codex import errors as E

def msg(dst="sw_uart", state=QUEUED):
    return Message(new_msg_id(), now_iso(), "dv_done", "dv_uart", dst, "soc_a", "uart", "walk1", "x", state, "r", updated_at=now_iso())

print("Q1 状态目录:A2A_STATE_DIR 给相对路径")
a = tempfile.mkdtemp(); b = tempfile.mkdtemp(); out = []
for d in (a, b):
    os.chdir(d); out.append(str(Registry.default_state_dir({"A2A_STATE_DIR": "relative/dir"})))
print("  ", out[0]); print("  ", out[1], "  <- 相对路径被接受,且随工作目录变化(悄悄产生多份状态)" if out[0] != out[1] else "")

print("Q2 Spool 构造耗时(每次 a2a send 都会构造一次;构造时扫描全部 done 文件)")
for n in (500, 3000, 10000):
    root = Path(tempfile.mkdtemp()); (root/"done").mkdir(parents=True)
    for i in range(n):
        m = msg(state=DELIVERED); (root/"done"/f"{m.msg_id}.json").write_text(json.dumps(m.to_dict()))
    t = time.time(); Spool(root); print(f"   done 里有 {n:>5} 条 -> 构造 {time.time()-t:.2f} 秒")

print("Q3 审计日志:中间有一行损坏")
td = Path(tempfile.mkdtemp()); al = AuditLog(td/"a.jsonl"); al.record({"n":1})
with open(al.path,"ab") as f: f.write(b"{broken\n")
al.record({"n":3})
try: print("   读出:", [e.get("n") for e in al.read()])
except Exception as e: print("   整个日志无法读取:", type(e).__name__, "-", str(e)[:60])

print("Q4 TopologyStore:文件被改坏后,current 每次都重新解析")
CFG = {"version":1,"project_id":"soc_a","roles":{"dv":{},"sw":{}},"ips":["uart"],
       "edges":[{"id":"dv_done","from":"dv","to":"sw","template":"{ip} done"}]}
import yaml
tp = Path(tempfile.mkdtemp())/"t.yaml"; tp.write_text(yaml.safe_dump(CFG)); st = TopologyStore(tp)
tp.write_text("version: 1\nproject_id: soc_a\nroles: {}\n")
calls = []; real = Topology.load
with mock.patch.object(Topology, "load", side_effect=lambda p: (calls.append(1), real(p))[1]):
    for _ in range(5): st.current
print("   连续读 5 次,解析了", len(calls), "次  (我的实现记住坏文件的签名,只解析 1 次)")

print("Q9 revision 的含义")
tp.write_text(yaml.safe_dump(CFG)); st.current; r1 = st.revision
os.utime(tp, None); st.current; r2 = st.revision
print("   内容未变,仅 touch 之后修订号:", "变了" if r1 != r2 else "没变", f"({r1}  ->  {r2})")

print("Q5 登记时 session=None 的记录能否被 Router 使用")
root = Path(tempfile.mkdtemp()); store = TopologyStore(tp); reg = Registry(root/"r.json"); topo = store.current
reg.register(AgentIdentity.create(topo,"dv","uart"), session=None, workspace_id="w1", tab_id="w1:t1", pane_id="w1:p2")
reg.register(AgentIdentity.create(topo,"sw","uart"), session=None, workspace_id="w1", tab_id="w1:t1", pane_id="w1:p3")
r = Router(store, reg, Spool(root/"s"), AuditLog(root/"a.jsonl"))
env = {"A2A_PROJECT_ID":"soc_a","A2A_ROLE":"dv","A2A_IP":"uart","HERDR_PANE_ID":"w1:p2"}
try: r.send("dv_done", env); print("   可用")
except SendRejected as e: print("   登记被接受,但 Router 永远拒绝:", e.code, "-", e.reason[:50])

print("Q6 命令行 --until 的 argparse 默认值")
captured = {}
class Stub:
    def __init__(self, *a, **k): pass
    def wait_agent(self, target, until, timeout_ms=None):
        captured["until"] = list(until); return type("R", (), {"raw": {}})()
import a2a_codex.cli as cli
with mock.patch.object(cli, "HerdrClient", Stub): cli.main(["wait-agent", "t", "--until", "idle"])
print("   传入 --until idle 实际等待:", captured["until"])

print("Q7 错误类型映射")
for code in ("agent_name_taken", "invalid_agent_name", "agent_prompt_stalled"):
    print(f"   {code:<22} -> {type(E.from_code(code, 'x')).__name__}")

print("Q8 队列:终态之后再改回非终态;msg_id 路径穿越")
sp = Spool(Path(tempfile.mkdtemp())/"s"); m = sp.enqueue(msg()); sp.update(m.msg_id, state="DELIVERED")
try: sp.update(m.msg_id, state="QUEUED"); print("   DELIVERED -> QUEUED 被接受,当前", sp.get(m.msg_id).state)
except Exception as e: print("   被拒绝:", type(e).__name__)
try: sp.get("../../x"); print("   路径穿越未被拦截")
except Exception as e: print("   路径穿越被拦截:", type(e).__name__)

print("Q10 源码检查")
import subprocess, re
src = open(Path(a2a_codex.__file__).parent/"cli.py", encoding="utf-8").read()
print("   cli.py 有 send-prompt <target> <text> (任意目标、任意文本):", "send-prompt" in src)
print("   pyproject 把它注册为命令 a2a-herdr:", "a2a-herdr" in open(Path(a2a_codex.__file__).parent/"pyproject.toml").read())

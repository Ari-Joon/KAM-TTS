"""The live feed lists chunks in reading order, and a clear keeps what can still
be marked solid.

The feed used to sort by when each chunk finished synthesising and label it
with its rank among every row in the table, so "chunk 3/9" meant "the third
thing made", not the third chunk of the page, and any chunk made out of turn
appeared out of place. Each chunk now records its read, its position and the
read's length, and the feed orders and labels by those.

Works on an empty database in a temp directory; never touches the live one."""
import ast
import pathlib
import shutil
import sqlite3
import sys
import tempfile

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
HERE = pathlib.Path(tempfile.mkdtemp(prefix="kam_read_order_test_"))
for name in ("learner.py", "alignment.py"):
    shutil.copy(SERVER_DIR / name, HERE / name)
sys.path.insert(0, str(HERE))
import learner as L                      # builds an empty database here

PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")

def read(rid, index, total, ts):
    return {"id": rid, "index": index, "total": total, "ts": ts}

print("\n=== chunks made out of turn are listed in reading order ===")
# An old row from before reads were recorded, then a read of five chunks whose
# synthesis finished in the order 1, 3, 2, 5, 4.
L.log_chunk("An old chunk from before.", "statement", 200)
for i in (1, 3, 2, 5, 4):
    L.log_chunk(f"Chunk number {i} of the page.", "statement", 200,
                read=read("readA", i, 5, 2_000_000_000.0))
feed = L.get_chunk_feed(limit=20)
labels = [(r["seq"], r["total"]) for r in feed]
check("newest read first, last chunk at the top, in position order",
      [r["chunk_text"] for r in feed[:5]],
      [f"Chunk number {i} of the page." for i in (5, 4, 3, 2, 1)])
check("labelled by place in the read, not rank in the table",
      labels[:5], [(5, 5), (4, 5), (3, 5), (2, 5), (1, 5)])
check("a row with no read keeps the old label and sits below", feed[5]["chunk_text"],
      "An old chunk from before.")

print("\n=== a later read sits above an earlier one ===")
for i in (2, 1):
    L.log_chunk(f"Second read chunk {i}.", "statement", 200,
                read=read("readB", i, 2, 2_000_000_100.0))
feed = L.get_chunk_feed(limit=20)
check("the later read is on top, in order",
      [r["chunk_text"] for r in feed[:3]],
      ["Second read chunk 2.", "Second read chunk 1.", "Chunk number 5 of the page."])

print("\n=== a re-synthesis with no read keeps its place ===")
L.log_chunk("Chunk number 3 of the page.", "statement", 200)   # e.g. the benchmark
row = [r for r in L.get_chunk_feed(limit=20) if r["chunk_text"] == "Chunk number 3 of the page."][0]
check("still chunk 3 of 5 of its read", (row["seq"], row["total"], row["read_id"]), (3, 5, "readA"))

print("\n=== a clear keeps the read in progress ===")
ids = {r["chunk_text"]: r["chunk_id"] for r in L.get_chunk_feed(limit=20)}
keep = [ids["Second read chunk 2."]]
L.reset_stats(keep_ids=keep)
left = [r["chunk_text"] for r in L.get_chunk_feed(limit=20)]
check("only the kept chunk survives", left, ["Second read chunk 2."])
L.reset_stats()
check("a clear with nothing to keep empties it", L.get_chunk_feed(limit=20), [])

print("\n=== heard chunks the history no longer has are counted, not lost silently ===")
cid = L.log_chunk("A chunk that will be heard.", "statement", 200, read=read("readC", 1, 1, 2_000_000_200.0))
before = L.get_counter("solid_missed_total") or 0
res = L.mark_session_solid(played=[cid, "0123456789abcdef"])
check("the one still there is marked solid", res["solid"], 1)
check("the one cleared away is reported missing", res["missing"], 1)
check("and counted for good", (L.get_counter("solid_missed_total") or 0) - before, 1)
c = sqlite3.connect(str(L.DB_PATH))
check("the solid mark is on the row", c.execute("SELECT user_feedback FROM chunks WHERE id=?", (cid,)).fetchone()[0], "solid")
c.close()

print("\n=== the server reads a request's place safely ===")
src = (SERVER_DIR / "server.py").read_text(encoding="utf-8")
fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_read_from")
ns = {}
exec(compile(ast.Module([fn], []), "server._read_from", "exec"), ns)
rf = ns["_read_from"]
check("a full request", rf({"read_id": "r1", "index": 3, "read_total": 9, "read_ts": 12.5, "epoch": 77}),
      {"id": "r1", "index": 3, "total": 9, "ts": 12.5, "epoch": 77.0})
check("no read id means no read", rf({"index": 3}), None)
check("junk values become None, never an error",
      rf({"read_id": "r1", "index": "x", "read_total": None, "read_ts": "nope", "epoch": "?"}),
      {"id": "r1", "index": None, "total": None, "ts": None, "epoch": None})

shutil.rmtree(HERE, ignore_errors=True)
print(f"\n{'='*64}\n  {PASS} passed, {FAIL} failed\n{'='*64}")
sys.exit(1 if FAIL else 0)

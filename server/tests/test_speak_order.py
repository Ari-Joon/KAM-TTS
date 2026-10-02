"""The order chunks are synthesised in (speak_order.py).

Real threads, a fake 'synthesis' that records when it ran. The failure this
guards: prefetched chunks arriving together and being synthesised in whatever
order their threads woke, so the feed listed them out of order and playback
waited on the chunk it actually needed."""
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from speak_order import SpeakOrder

PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")


def run(order, jobs, synth_s=0.03):
    """jobs: (delay before arriving, epoch, index). Returns the (epoch, index)
    pairs in the order they were synthesised, and the ones superseded."""
    ran, dropped, lock = [], [], threading.Lock()
    def worker(delay, epoch, index):
        time.sleep(delay)
        if not order.acquire(epoch, index):
            with lock: dropped.append((epoch, index))
            return
        try:
            with lock: ran.append((epoch, index))
            time.sleep(synth_s)
        finally:
            order.release(epoch)
    ts = [threading.Thread(target=worker, args=j) for j in jobs]
    for t in ts: t.start()
    for t in ts: t.join(10)
    return ran, sorted(dropped)


print("\n=== a prefetch burst that lands out of order is synthesised in order ===")
ran, dropped = run(SpeakOrder(), [(0.00, 1, 3), (0.005, 1, 1), (0.01, 1, 4), (0.015, 1, 2)])
check("positions run 1, 2, 3, 4 whatever order they arrived in",
      [i for _, i in ran], [1, 2, 3, 4])
check("nothing dropped", dropped, [])

print("\n=== a later arrival with a lower position still goes before higher ones ===")
# Chunk 5 is mid-synthesis when 7 and then 6 arrive; 6 must go before 7.
ran, _ = run(SpeakOrder(), [(0.0, 1, 5), (0.20, 1, 7), (0.24, 1, 6)], synth_s=0.4)
check("5, then 6, then 7", [i for _, i in ran], [5, 6, 7])

print("\n=== a gap in positions is not a deadlock ===")
ran, _ = run(SpeakOrder(), [(0.0, 1, 1), (0.0, 1, 3), (0.0, 1, 4)])
check("1, 3, 4 with 2 never asked for", [i for _, i in ran], [1, 3, 4])

print("\n=== a jump supersedes what the old read still had waiting ===")
o = SpeakOrder()
# The jump lands while chunk 1 is synthesising (after the 0.15 s gather).
ran, dropped = run(o, [(0.0, 1, 1), (0.0, 1, 2), (0.0, 1, 3), (0.25, 2, 10), (0.25, 2, 11)], synth_s=0.3)
check("the chunk already synthesising finishes; the new read follows in order",
      ran, [(1, 1), (2, 10), (2, 11)])
check("the old read's waiting chunks never touch the GPU", dropped, [(1, 2), (1, 3)])
check("and a late request from the old read is refused at once", o.acquire(1, 4), False)

o = SpeakOrder()
ran, dropped = run(o, [(0.0, 1, 1), (0.0, 1, 2), (0.01, 2, 10)], synth_s=0.05)
check("a jump inside the old read's gather window drops all of the old read",
      (ran, dropped), ([(2, 10)], [(1, 1), (1, 2)]))

print("\n=== a stop empties the queue without waiting for a new read ===")
o = SpeakOrder()
results = {}
def slow():
    o.acquire(5, 1); time.sleep(0.2); o.release(5)
def waiter(i):
    results[i] = o.acquire(5, i)
    if results[i]: o.release(5)
threads = [threading.Thread(target=slow)] + [threading.Thread(target=waiter, args=(i,)) for i in (2, 3)]
threads[0].start(); time.sleep(0.05)
for t in threads[1:]: t.start()
time.sleep(0.05)
o.cancel(5)
for t in threads: t.join(5)
check("waiting chunks of a stopped read give up", results, {2: False, 3: False})
check("nothing is left waiting", o.waiting(), [])
check("a new read after the stop runs", o.acquire(6, 1), True); o.release(6)

print("\n=== requests with no epoch are never superseded ===")
o = SpeakOrder()
ran, dropped = run(o, [(0.0, None, None), (0.0, 3, 1), (0.01, 4, 1), (0.02, None, None)])
check("both untagged requests ran", sum(1 for e, _ in ran if e is None), 2)
check("only the superseded epoch was dropped", dropped, [] if (3, 1) in ran else [(3, 1)])

print(f"\n{'='*64}\n  {PASS} passed, {FAIL} failed\n{'='*64}")
sys.exit(1 if FAIL else 0)

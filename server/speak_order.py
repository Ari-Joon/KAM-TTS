"""The order chunks are synthesised in.

The reader asks for two or three chunks ahead of the one playing, and they
arrive within milliseconds of each other. Synthesis is serial, one chunk at a
time on one GPU, and it used to be serialised by a plain lock. A lock is not a
queue: when one chunk finished, whichever waiting request happened to wake first
went next, so chunk 4 could be synthesised before chunk 3. Playback then waited
on chunk 3 for no reason, and the dashboard's live feed, which lists chunks in
the order they were made, showed them out of order.

Two other things made it worse. Stopping or jumping aborted the requests in the
extension, but a server thread cannot see that, so the old read's chunks were
still synthesised, ahead of the new read's, for audio nobody would hear. And a
request that was already waiting had no idea a newer read had begun.

So this is a small turnstile in front of synthesis:

  - Requests carry an epoch (one per playback run; a new read, a jump and a
    stop all start a new one) and their position in the read.
  - Within the newest epoch, the lowest waiting position goes next.
  - A request from an older epoch never goes: it is told it was superseded and
    returns at once without touching the GPU. The extension also announces a
    stop, so a stopped read's queue empties without waiting for a new one.
  - The first request of an epoch waits a moment before starting, since its
    siblings are usually a few milliseconds behind it, and starting on
    whichever landed first is how the order went wrong in the first place.
  - A request with no epoch (the benchmark, a direct API call) is never
    superseded and takes its turn in arrival order behind the current read.

It never waits for a position that was never asked for, since the reader skips
chunks that clean to nothing, so a gap is not a deadlock."""
import threading
import time


class SpeakOrder:
    # How long the first request of a new epoch waits for its siblings. The
    # prefetch burst lands within a few milliseconds; synthesis takes seconds.
    GATHER_S = 0.15

    def __init__(self, clock=time.monotonic):
        self._cv = threading.Condition()
        self._clock = clock
        self._busy = False
        self._latest = None          # newest epoch seen, or cancelled up to
        self._waiting = {}           # ticket -> (epoch, index, arrived)
        self._gathering_until = {}   # epoch -> time its first turn may start
        self._seq = 0

    # --- bookkeeping ---
    def _note_epoch(self, epoch):
        if epoch is None:
            return
        if self._latest is None or epoch > self._latest:
            self._latest = epoch
            self._gathering_until[epoch] = self._clock() + self.GATHER_S
            # Older epochs can never run now, so their gather marks go too.
            for e in [e for e in self._gathering_until if e < epoch]:
                del self._gathering_until[e]
            self._cv.notify_all()

    def _superseded(self, epoch):
        return epoch is not None and self._latest is not None and epoch < self._latest

    def _best(self):
        """The ticket that should go next, or None. The current read first, in
        position order; untagged requests after it, in arrival order."""
        best = best_key = None
        for t, (epoch, index, arrived) in self._waiting.items():
            if self._superseded(epoch):
                continue
            if epoch is None:
                key = (1, arrived, 0)
            else:
                key = (0, index if index is not None else 0, arrived)
            if best_key is None or key < best_key:
                best, best_key = t, key
        return best

    # --- the turnstile ---
    def acquire(self, epoch=None, index=None):
        """Block until it is this request's turn. Returns True to go ahead, or
        False when a newer read has superseded it and it should not run."""
        with self._cv:
            self._note_epoch(epoch)
            if self._superseded(epoch):
                return False
            self._seq += 1
            ticket = self._seq
            self._waiting[ticket] = (epoch, index, self._clock())
            try:
                while True:
                    if self._superseded(epoch):
                        return False
                    if not self._busy and self._best() == ticket:
                        hold = self._gathering_until.get(epoch, 0) - self._clock()
                        if hold <= 0:
                            self._busy = True
                            return True
                        self._cv.wait(hold)
                        continue
                    self._cv.wait(1.0)
            finally:
                self._waiting.pop(ticket, None)
                # Whoever is next needs to look again, whether this one went
                # ahead or gave up.
                self._cv.notify_all()

    def release(self, epoch=None):
        with self._cv:
            self._busy = False
            # Once an epoch has started its first turn, nothing later in it
            # needs to wait to be gathered.
            if epoch is not None:
                self._gathering_until.pop(epoch, None)
            self._cv.notify_all()

    def cancel(self, epoch):
        """The extension stopped this epoch. Everything from it that is still
        waiting gives up, without needing a new read to arrive first."""
        if epoch is None:
            return
        with self._cv:
            # One past it, so that epoch itself counts as superseded.
            self._note_epoch(epoch + 1)
            self._gathering_until.pop(epoch + 1, None)

    def waiting(self):
        with self._cv:
            return sorted((e, i) for e, i, _ in self._waiting.values())

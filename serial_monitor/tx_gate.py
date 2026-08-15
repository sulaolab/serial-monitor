"""Transmit gate: exclusivity for the duration of one multi-write transfer.

Why this exists
---------------
Every writer -- each TCP terminal, ``POST /command``, and the XMODEM sender --
writes to the same UART.  ``SerialManager._ser_lock`` makes a *single* write
atomic, which is all a keystroke or a command line needs.  It is not enough for
XMODEM: that is hundreds of consecutive ``write_binary`` calls, and one stray
byte injected between two frames corrupts a firmware update.

The obvious fix -- "the first client that types owns the port until it
disconnects" -- is deliberately NOT what this module does.  That model makes a
second Tera Term silently dead for the rest of the session, which is exactly the
behaviour this project rejects: every terminal must be able to type, and
avoiding a collision is the operator's business.

So exclusivity here is scoped to a *transfer*, not to a *client*:

    open              -> every writer may transmit (the normal state)
    held by transfer  -> other writers are rejected, and told why
    transfer ends     -> open again, for everyone, immediately

Nobody accumulates a privilege, and the closed window lasts seconds.

Safety valve
------------
A transfer that dies without releasing (killed thread, unhandled exception in a
caller that bypassed the context manager) would otherwise wedge the UART for
good.  A hold therefore expires after ``max_hold_s``: the gate reopens, the
expiry is counted, and the stale holder's later release becomes a no-op -- it
cannot reopen a window a *newer* holder has since taken.  An UART that accepts
input again is strictly better than one that is quietly mute forever.

Two phases, because arming is not transferring
----------------------------------------------
A firmware transfer starts by *arming* the board (write its arm verb, then wait
for the receiver's ``C``) and only then streams frames.  Closing the gate for the arm
phase too is what an operator experiences as a freeze: their terminal goes deaf
for up to the whole handshake timeout while nothing visible happens, and if the
handshake never arrives they never learn why (reported 2026-08-13).  Nothing is
protected by it either -- there are no frames in flight yet, and a keystroke
during the arm phase is exactly the keystroke the operator may need (a reset, or
arming by hand).

:meth:`transfer` therefore splits the window: *reserved* keeps a second transfer
out while every ordinary writer still transmits, and the caller closes the gate
itself the moment the handshake lands.  The reservation blocks no writer, so it
is not covered by the expiry valve below; the caller's handshake timeout bounds
it.  :meth:`hold` remains the one-phase form for a transfer with no arm phase.

"The moment the handshake lands" is not the moment it is *confirmed*
---------------------------------------------------------------------
Recognising the receiver's ``C`` takes time: a lone ``C`` is also ordinary text,
so the sender only accepts one that is followed by a short silence.  Closing the
gate after that silence leaves the receiver already listening while ordinary
writers are still permitted -- a keystroke in that window reaches a receiver that
is waiting for block 1 (reported by review, 2026-08-15).  The window was as long
as the isolation interval, which is 100% of the time between the two events.

So a close may be *provisional*: taken on the candidate byte, before the evidence
is in.  It blocks writers immediately (and is covered by the expiry valve, since
it does), and it can be reverted by :meth:`TransferWindow.reopen` when the
candidate turns out to have been text -- which must not cost the operator their
terminal for the rest of the handshake timeout.  Only a confirmed close counts as
a hold in :meth:`status`; ``provisional`` there says which kind is in force.

Ask for a permit; do not ask a question
---------------------------------------
The gate cannot be enforced inside ``send_raw`` -- the holder itself has to be
able to write -- so it is the *callers* who consult it.  Consulting it with
:meth:`blocked_by` and then writing is a check-then-act, and the gap between the
two is exactly wide enough for the failure this module exists to prevent:

    ordinary writer            transfer
    blocked_by() -> None
                               handshake 'C' -> close()
                               frames going out
    write("x")                 <- lands between two frames

so an ordinary writer takes a :meth:`write_permit` instead, which checks and
registers in one critical section.  While any permit is out, closing the gate
waits for it (bounded -- see ``_drain_writers_locked``); once the gate is closed,
a new permit is refused.  The two states cannot overlap, which is the whole
point.  :meth:`blocked_by` remains, for *reporting* why input was dropped
(``/status``), not for deciding whether to write.

Draining is not the same as being clean
---------------------------------------
Waiting out an in-flight write keeps its bytes from landing *between* two frames,
which is what a hold needs.  A handshake needs more: the receiver that emitted
``C`` is already waiting for block 1, so the tail of a write that started
*earlier* can reach it in front of block 1 -- and one guarded TCP write is a
whole ``recv`` buffer, not necessarily one keystroke (reported by review,
2026-08-15).  Nothing here can tell whether that happened, so the gate does not
guess: :attr:`TransferWindow.contended` records that a write was in flight when
the window closed, and the sender abandons that transfer instead of streaming an
image at a receiver that may already have swallowed a stray byte.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager


class TxGateBusy(RuntimeError):
    """Raised when a transfer asks for the gate while another one holds it."""


class TxBlocked(RuntimeError):
    """Raised when an ordinary writer asks to transmit while a transfer holds it.

    Carries ``.blocker`` so the caller can name the transfer in its 409 / notice
    instead of asking the gate a second question whose answer may already differ.
    """

    def __init__(self, blocker: str):
        super().__init__(f"{blocker} holds the transmit gate")
        self.blocker = blocker


# How long closing the gate waits for writes that were already in flight. An
# ordinary write is one keystroke or one command line; if it has not finished in
# this long it is stuck in the OS, and postponing a firmware transfer forever is
# the worse of the two outcomes. Counted in status() so it is never silent.
_DRAIN_TIMEOUT_S = 2.0


class TransferWindow:
    """The gate-closing half of :meth:`TxGate.transfer`.

    Callable, so ``close_gate()`` reads as it did when this was a bare function:
    that form is the confirmed close.  Two extra shapes exist for a sender that
    has to act on a *candidate* handshake before it can prove it (see the module
    docstring):

    * ``close(provisional=True)`` -- block writers now, revertible;
    * :meth:`reopen` -- undo a provisional close, nothing having been sent.

    Confirming is just ``close()`` again: it upgrades a provisional close in
    place, without reopening the gate in between.
    """

    __slots__ = ("_gate", "_owner", "_my_gen", "_confirmed", "_contended")

    def __init__(self, gate: "TxGate", owner: str):
        self._gate = gate
        self._owner = owner
        self._my_gen: int | None = None
        self._confirmed = False
        self._contended = False

    def __call__(self) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        return self._my_gen is not None

    @property
    def contended(self) -> bool:
        """True if an ordinary write was still in flight when this window closed.

        Closing waits such a write out, so nothing of it can land *between* our
        frames -- but waiting is not the same as being clean.  On a handshake the
        receiver is already listening for block 1, so bytes from a write that
        started *before* the candidate ``C`` can still reach it in front of block
        1.  The gate cannot know whether they did; it only reports the doubt, and
        the caller decides (the sender refuses to start such a transfer).

        Cleared by :meth:`reopen`: a candidate that turned out to be text means
        the writer was legitimately permitted and nothing was armed.
        """
        return self._contended

    def close(self, *, provisional: bool = False) -> None:
        """Close the gate.  Idempotent; a second call confirms a provisional one."""
        gate = self._gate
        with gate._lock:
            if self._my_gen is not None:
                # Already closed.  A confirming call only has to drop the
                # provisional flag -- reopening and re-closing would open the very
                # window this exists to remove.
                if not provisional and not self._confirmed:
                    self._confirmed = True
                    gate._provisional = False
                    gate._holds += 1
                return
            gate._owner = self._owner
            gate._since = gate._clock()
            gate._gen += 1
            self._my_gen = gate._gen
            self._confirmed = not provisional
            gate._provisional = provisional
            if self._confirmed:
                gate._holds += 1
            # From here no new ordinary write is permitted; wait out the ones
            # already inside so no byte can land between our first frames.  Note
            # first whether there were any: see .contended.
            if gate._writers > 0:
                self._contended = True
                gate._contended_closes += 1
            gate._drain_writers_locked()

    def reopen(self) -> None:
        """Revert a provisional close: the candidate was not the handshake.

        A no-op once confirmed, and a no-op if the window expired or was replaced
        -- reopening then would reopen somebody else's hold.
        """
        gate = self._gate
        with gate._lock:
            if self._my_gen is None or self._confirmed:
                return
            if gate._gen != self._my_gen or gate._owner is None:
                return
            gate._owner = None
            gate._gen += 1
            gate._provisional = False
            gate._provisional_reverts += 1
            self._my_gen = None
            self._contended = False


class TxGate:
    def __init__(self, *, max_hold_s: float = 180.0, clock=time.monotonic):
        # A Condition, not a plain Lock: closing the gate has to wait for the
        # ordinary writes already inside it (see _drain_writers_locked).
        self._lock = threading.Condition(threading.Lock())
        self._owner: str | None = None
        # A transfer that has started but has not closed the gate yet (arm phase).
        # Keeps a second transfer out; blocks no ordinary writer.
        self._reserved_by: str | None = None
        self._since = 0.0
        # Bumped on every acquire and every release/expiry, so a release can tell
        # "my window" from "a window that replaced mine".
        self._gen = 0
        self._holds = 0
        self._expiries = 0
        # Ordinary writes currently in flight, and how often draining them timed
        # out (a stuck writer, not a normal one).
        self._writers = 0
        self._drain_timeouts = 0
        # A close taken on a candidate handshake, not yet confirmed, and how many
        # of those turned out to be text after all.
        self._provisional = False
        self._provisional_reverts = 0
        # Closes taken while an ordinary write was still in flight (see
        # TransferWindow.contended).  The transfer that hits one is abandoned, so
        # this is the count of "retry it" outcomes.
        self._contended_closes = 0
        self._max_hold_s = max_hold_s
        self._clock = clock

    # -- internals ------------------------------------------------------------
    def _expire_locked(self) -> None:
        if self._owner is None:
            return
        if (self._clock() - self._since) < self._max_hold_s:
            return
        self._owner = None
        self._gen += 1
        self._expiries += 1
        self._provisional = False

    def _drain_writers_locked(self) -> None:
        """Wait out the ordinary writes already in flight.  Caller holds the lock.

        New writers are refused the moment ``_owner`` is set, so this waits for a
        finite, already-started set -- normally none at all.  The deadline is real
        time rather than ``self._clock``: an injected test clock decides when a
        *hold* expires, but how long a UART write takes is not up to it.
        """
        deadline = time.monotonic() + _DRAIN_TIMEOUT_S
        while self._writers > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._drain_timeouts += 1
                return
            self._lock.wait(remaining)

    # -- transfers ------------------------------------------------------------
    @contextmanager
    def transfer(self, owner: str):
        """Two-phase window for a transfer that arms the board first.

        Yields a :class:`TransferWindow`.  It is callable, so ``close_gate()``
        still closes the gate; until something closes it the gate stays open --
        other writers transmit normally and only a second transfer is refused.
        Call it the moment frames are about to go out; it is idempotent, and the
        window is released when the context exits either way.  A sender that must
        act on an unproven handshake uses ``close(provisional=True)`` and
        ``reopen()`` instead -- see the module docstring.
        """
        with self._lock:
            self._expire_locked()
            busy = self._owner or self._reserved_by
            if busy is not None:
                raise TxGateBusy(f"{busy} is holding the transmit gate")
            self._reserved_by = owner

        window = TransferWindow(self, owner)
        try:
            yield window
        finally:
            with self._lock:
                if self._reserved_by == owner:
                    self._reserved_by = None
                # Only release the window we actually took (see hold()).
                my_gen = window._my_gen
                if (my_gen is not None) and (self._gen == my_gen) and \
                        (self._owner is not None):
                    self._owner = None
                    self._gen += 1
                    self._provisional = False

    @contextmanager
    def hold(self, owner: str):
        """Hold the gate for one transfer.  Raises TxGateBusy if already held."""
        with self._lock:
            self._expire_locked()
            busy = self._owner or self._reserved_by
            if busy is not None:
                raise TxGateBusy(f"{busy} is holding the transmit gate")
            self._owner = owner
            self._since = self._clock()
            self._gen += 1
            self._holds += 1
            my_gen = self._gen
            self._drain_writers_locked()
        try:
            yield
        finally:
            with self._lock:
                # Only release the window we actually took; if it already expired
                # and someone else took one, leave theirs alone.
                if self._gen == my_gen and self._owner is not None:
                    self._owner = None
                    self._gen += 1

    # -- other writers --------------------------------------------------------
    @contextmanager
    def write_permit(self, who: str = "writer"):
        """Permit for ONE ordinary write.  The gate cannot close while it is held.

        Raises :class:`TxBlocked` if a transfer holds the gate -- the caller has
        written nothing and should say so (409, or a notice to the terminal that
        typed).  Hold it for the write itself only: a transfer waiting to start is
        waiting on exactly this.

        Not for the transfer holder.  It writes with ``send_raw``, which asks the
        gate nothing, because the closed gate is its own.
        """
        with self._lock:
            self._expire_locked()
            if self._owner is not None:
                raise TxBlocked(self._owner)
            self._writers += 1
        try:
            yield
        finally:
            with self._lock:
                self._writers -= 1
                # Wake a close() that is draining us.
                self._lock.notify_all()

    def blocked_by(self) -> str | None:
        """Name of the transfer currently blocking writes, or None if open.

        For *reporting* -- "why did my keystroke vanish" -- not for deciding
        whether to write.  Deciding that way is the race described in the module
        docstring; use :meth:`write_permit`.
        """
        with self._lock:
            self._expire_locked()
            return self._owner

    def status(self) -> dict:
        with self._lock:
            self._expire_locked()
            held_ms = (
                int((self._clock() - self._since) * 1000)
                if self._owner is not None
                else None
            )
            return {
                "held_by": self._owner,
                # A transfer in its arm phase: no writer is blocked yet.
                "reserved_by": self._reserved_by,
                # True while the close is only provisional -- writers ARE blocked,
                # but on a handshake candidate that may yet prove to be text.
                "provisional": self._provisional,
                "provisional_reverts": self._provisional_reverts,
                "held_ms": held_ms,
                "max_hold_s": self._max_hold_s,
                "holds": self._holds,
                "expiries": self._expiries,
                # Ordinary writes in flight right now, and how often a transfer
                # gave up waiting for one (should stay 0; non-zero means a writer
                # was stuck in the OS when a transfer started).
                "writers_in_flight": self._writers,
                "drain_timeouts": self._drain_timeouts,
                # Closes that found a write already in flight -- each one aborts
                # its transfer rather than guess whether the receiver was dirtied.
                "contended_closes": self._contended_closes,
            }

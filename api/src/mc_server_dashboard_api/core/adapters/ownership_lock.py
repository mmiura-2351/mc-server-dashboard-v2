"""The community-ownership lock: account deletion against community writes.

An account deletion refuses an account that owns a community, and its delete
cascades the account's memberships, role assignments and grants. A conferral of
ownership -- a community provisioned with the account as its Owner, the Owner
role assigned to it -- writes exactly the rows that cascade removes (#3217).
Adding the account as a member, or granting it a resource permission, confers
nothing but inserts a row referencing the account. Unheld, a deletion committed
after the write checked the account makes that insert fail its foreign key, and
a deletion whose cascade waits for the membership a grant creation holds
deadlocks with it (#3856). Both sides take this lock, keyed on the account, in
the transaction that commits their write and before they decide, and hold it
until that transaction ends:

- an account deletion takes it **exclusively**, then reads whether the account
  owns a community (identity's ``CommunityOwnership`` adapter);
- a community write on the account -- a conferral, a member addition, a grant
  creation -- takes it **shared** as the first statement of its transaction,
  then reads that the account exists (community's ``UserExistenceChecker``
  adapter). These writes do not wait for each other.

Whichever side comes second waits for the first to commit, and every statement
it runs after the wait takes a new READ COMMITTED snapshot, so it decides from
what the first committed: a deletion refuses an account that now owns a
community, and otherwise cascades what the write committed, and a write finds
no account once it was deleted. The lock lives
here, in the adapter layer's shared kernel, because both contexts' adapters take
it and neither context may import the other's domain.

**Lock order.** A deletion takes this lock right after its ``user`` row locks
(step 1 of the identity order, ``RefreshTokenRepository``) and before every
other lock; a community write takes it before any other lock (step 0 of the
community order, ``permission_ceiling``). No wait cycle runs through it. A write
waiting for it holds nothing. A deletion waiting for it holds only ``user`` rows
locked ``FOR NO KEY UPDATE``, which a holder of the lock never waits for: a
deletion holding it locked its own ``user`` rows before it, and a write locks no
``user`` row except, when it inserts a membership or a grant, the account's row
``FOR KEY SHARE`` for the foreign-key check, which conflicts only with
``FOR UPDATE``. The last-Owner guard's lock on the Owner assignments (#1959) is
taken by no transaction that takes this lock or any ``user`` row lock, and a
deletion that is not refused cascades no Owner assignment: its account owns no
community.

**Key namespace.** The two-key form ``pg_advisory_xact_lock(int4, int4)``: the
first key is :data:`OWNERSHIP_LOCK_CLASS`, the second a 32-bit fold of the user
id. PostgreSQL keeps the two-key space apart from the single ``bigint`` key
space, which every other advisory lock of this codebase uses (the first-user
bootstrap lock and the refresh-token session locks in ``identity``, the server
lifecycle lock in ``servers``), so this lock cannot collide with any of them. A
new two-key lock must take a different class. Two accounts whose folds collide
share one lock, which only makes them wait for each other: no transaction takes
this lock for more than one account, so a collision cannot close a wait cycle.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The first key of every community-ownership lock: ``mcsd`` in ASCII, which fits
# a signed int4.
OWNERSHIP_LOCK_CLASS = 0x6D63_7364


def _ownership_key(user_id: uuid.UUID) -> int:
    """Fold a user id into the signed 32-bit second key of its lock.

    Hashed rather than truncated: a time-ordered id would share its leading bits
    with every account created around the same time.
    """

    digest = hashlib.blake2b(user_id.bytes, digest_size=4).digest()
    return int.from_bytes(digest, "big", signed=True)


async def lock_community_ownership(
    session: AsyncSession, user_id: uuid.UUID, *, shared: bool
) -> None:
    """Take ``user_id``'s community-ownership lock until the transaction ends.

    ``shared`` for a community write on the user, exclusive for the user's
    deletion. Blocks while a conflicting holder's transaction is open.
    """

    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await session.execute(
        text(
            f"SELECT {function}"
            "(CAST(:lock_class AS integer), CAST(:lock_key AS integer))"
        ).bindparams(lock_class=OWNERSHIP_LOCK_CLASS, lock_key=_ownership_key(user_id))
    )

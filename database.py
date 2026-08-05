import sqlite3
import os
from datetime import datetime, timezone, timedelta
from config import DATABASE_NAME

IST = timezone(timedelta(hours=5, minutes=30))

# Every new member/expense starts on this currency until changed in Settings.
DEFAULT_CURRENCY = "INR"


def now_ist() -> str:
    """Returns the current time in IST as 'YYYY-MM-DD HH:MM:SS'.

    SQLite's DEFAULT CURRENT_TIMESTAMP always stores UTC. Use this helper
    to explicitly write local (IST) times wherever a created_at/settled_at
    value is inserted, so history and monthly-summary date filters line up
    with the user's actual local day/time.
    """
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def get_connection():
    os.makedirs("data", exist_ok=True)

    conn = sqlite3.connect(DATABASE_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables():
    conn = get_connection()
    cursor = conn.cursor()

    # -----------------------------
    # GROUPS
    # -----------------------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        group_id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_chat_id INTEGER NOT NULL UNIQUE,
        group_name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # -----------------------------
    # MEMBERS
    # -----------------------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS members (
        member_id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        telegram_user_id INTEGER NOT NULL,
        display_name TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'confirmed',
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY(group_id)
            REFERENCES groups(group_id)
            ON DELETE CASCADE,

        UNIQUE(group_id, telegram_user_id)
    )
    """)

    # -----------------------------
    # EXPENSES
    # -----------------------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        expense_id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        payer_member_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        category TEXT NOT NULL,
        description TEXT,
        split_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY(group_id)
            REFERENCES groups(group_id)
            ON DELETE CASCADE,

        FOREIGN KEY(payer_member_id)
            REFERENCES members(member_id)
            ON DELETE RESTRICT
    )
    """)

    # Migration: expenses created before the decline-window feature don't
    # have a status column yet. Backfill them as 'active' so pre-existing
    # history/balances are unaffected.
    cursor.execute("PRAGMA table_info(expenses)")
    existing_expense_cols = {row[1] for row in cursor.fetchall()}
    if "status" not in existing_expense_cols:
        cursor.execute("ALTER TABLE expenses ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")

    # -----------------------------
    # EXPENSE SHARES
    # -----------------------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expense_shares (
        share_id INTEGER PRIMARY KEY AUTOINCREMENT,
        expense_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        share_amount REAL NOT NULL,

        FOREIGN KEY(expense_id)
            REFERENCES expenses(expense_id)
            ON DELETE CASCADE,

        FOREIGN KEY(member_id)
            REFERENCES members(member_id)
            ON DELETE RESTRICT
    )
    """)

    # -----------------------------
    # MEMBER DEBTS
    # -----------------------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS member_debts (
        debt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        expense_id INTEGER NOT NULL,

        debtor_member_id INTEGER NOT NULL,
        creditor_member_id INTEGER NOT NULL,

        amount REAL NOT NULL,

        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY(group_id)
            REFERENCES groups(group_id)
            ON DELETE CASCADE,

        FOREIGN KEY(expense_id)
            REFERENCES expenses(expense_id)
            ON DELETE CASCADE,

        FOREIGN KEY(debtor_member_id)
            REFERENCES members(member_id)
            ON DELETE RESTRICT,

        FOREIGN KEY(creditor_member_id)
            REFERENCES members(member_id)
            ON DELETE RESTRICT
    )
    """)

    # -----------------------------
    # SETTLEMENTS
    # -----------------------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settlements (
        settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
        debt_id INTEGER NOT NULL,
        group_id INTEGER,
        debtor_member_id INTEGER,
        creditor_member_id INTEGER,
        amount REAL NOT NULL,
        settled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY(debt_id)
            REFERENCES member_debts(debt_id)
            ON DELETE CASCADE
    )
    """)

    # Migration: add columns for existing databases created before this change.
    # SQLite has no "ADD COLUMN IF NOT EXISTS", so check pragma first.
    cursor.execute("PRAGMA table_info(settlements)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    for col in ("group_id", "debtor_member_id", "creditor_member_id"):
        if col not in existing_cols:
            cursor.execute(f"ALTER TABLE settlements ADD COLUMN {col} INTEGER")

    # Migration: members created before the invite/confirm feature don't have
    # a status column yet. Backfill them as 'confirmed' -- they already went
    # through the old /start-based registration, so they shouldn't suddenly
    # be treated as pending and re-prompted to join.
    cursor.execute("PRAGMA table_info(members)")
    existing_member_cols = {row[1] for row in cursor.fetchall()}
    if "status" not in existing_member_cols:
        cursor.execute("ALTER TABLE members ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'")

    # Migration: per-member display currency. Everyone starts on INR until
    # they change it in Settings -> Currency (see handlers/settings.py).
    if "currency" not in existing_member_cols:
        cursor.execute(f"ALTER TABLE members ADD COLUMN currency TEXT NOT NULL DEFAULT '{DEFAULT_CURRENCY}'")

    # Migration: the currency an expense was actually entered in (the
    # payer's currency at the moment they added it). Snapshotted once at
    # creation -- see stamp_expense_currency() -- so it never drifts even
    # if the payer changes their personal currency later.
    cursor.execute("PRAGMA table_info(expenses)")
    existing_expense_cols = {row[1] for row in cursor.fetchall()}
    if "currency" not in existing_expense_cols:
        cursor.execute(f"ALTER TABLE expenses ADD COLUMN currency TEXT NOT NULL DEFAULT '{DEFAULT_CURRENCY}'")

    # Migration: a per-share converted-amount snapshot, so a participant
    # whose display currency differs from the expense's currency sees a
    # fixed conversion frozen at expense time -- see save_share_conversion().
    cursor.execute("PRAGMA table_info(expense_shares)")
    existing_share_cols = {row[1] for row in cursor.fetchall()}
    if "converted_amount" not in existing_share_cols:
        cursor.execute("ALTER TABLE expense_shares ADD COLUMN converted_amount REAL")
    if "converted_currency" not in existing_share_cols:
        cursor.execute("ALTER TABLE expense_shares ADD COLUMN converted_currency TEXT")
    if "fx_rate" not in existing_share_cols:
        cursor.execute("ALTER TABLE expense_shares ADD COLUMN fx_rate REAL")

    # -----------------------------
    # PENDING EXPENSES
    # -----------------------------
    # Holds an expense that can't be finalized yet because one or more
    # selected members haven't confirmed they're in the group. Once every
    # row in pending_expense_members is confirmed, this gets turned into a
    # real row in `expenses` (see handlers/join.py) and deleted.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pending_expenses (
        pending_id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        payer_member_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        category TEXT NOT NULL,
        description TEXT,
        status_chat_id INTEGER NOT NULL,
        status_message_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

        FOREIGN KEY(group_id)
            REFERENCES groups(group_id)
            ON DELETE CASCADE,

        FOREIGN KEY(payer_member_id)
            REFERENCES members(member_id)
            ON DELETE RESTRICT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pending_expense_members (
        pending_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 0,

        PRIMARY KEY (pending_id, member_id),

        FOREIGN KEY(pending_id)
            REFERENCES pending_expenses(pending_id)
            ON DELETE CASCADE,

        FOREIGN KEY(member_id)
            REFERENCES members(member_id)
            ON DELETE CASCADE
    )
    """)

    # -----------------------------
    # USER SUBGROUPS (NEW)
    # -----------------------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_subgroups (
        user_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL,
        selected_member_ids TEXT NOT NULL,
        expires_at TIMESTAMP NOT NULL,
        PRIMARY KEY (user_id, group_id),
        FOREIGN KEY(group_id)
            REFERENCES groups(group_id)
            ON DELETE CASCADE
    )
    """)

    # -----------------------------
    # EXPENSE SHARE NOTIFICATIONS (NEW)
    # -----------------------------
    # Tracks the private "your share is ₹X — Decline?" DM sent to each
    # participant of an expense, so that when one person declines we can go
    # back and edit every other participant's copy of that DM (and the
    # original card) to show it's no longer actionable.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expense_notifications (
        expense_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        telegram_user_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,

        PRIMARY KEY (expense_id, member_id),

        FOREIGN KEY(expense_id)
            REFERENCES expenses(expense_id)
            ON DELETE CASCADE,

        FOREIGN KEY(member_id)
            REFERENCES members(member_id)
            ON DELETE CASCADE
    )
    """)

    # -----------------------------
    # SUBSCRIPTIONS (NEW)
    # -----------------------------
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        telegram_user_id INTEGER PRIMARY KEY,
        plan TEXT NOT NULL DEFAULT 'pro',
        expires_at TIMESTAMP NOT NULL,
        last_payment_charge_id TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()


# -----------------------------
# SUBSCRIPTION HELPERS
# -----------------------------
def is_subscribed(telegram_user_id: int) -> bool:
    """True if this user currently has an active (non-expired) subscription."""
    row = execute(
        "SELECT expires_at FROM subscriptions WHERE telegram_user_id=?",
        (telegram_user_id,),
        fetch=True,
    )
    if not row:
        return False
    expires_at = datetime.strptime(row[0][0], "%Y-%m-%d %H:%M:%S")
    return expires_at > datetime.now(IST).replace(tzinfo=None)


def get_subscription_expiry(telegram_user_id: int):
    """Returns the expiry datetime string for this user, or None if never subscribed."""
    row = execute(
        "SELECT expires_at FROM subscriptions WHERE telegram_user_id=?",
        (telegram_user_id,),
        fetch=True,
    )
    return row[0][0] if row else None


def grant_subscription(telegram_user_id: int, days: int, charge_id: str = None, plan: str = "pro"):
    """
    Activates or extends a user's subscription. If they still have time left
    on an existing plan, the new period is added on top of it (rather than
    overwriting), so renewing early never costs the user days they already paid for.
    """
    existing_expiry = get_subscription_expiry(telegram_user_id)
    now = datetime.now(IST).replace(tzinfo=None)

    base = now
    if existing_expiry:
        existing_dt = datetime.strptime(existing_expiry, "%Y-%m-%d %H:%M:%S")
        if existing_dt > now:
            base = existing_dt

    new_expiry = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    execute(
        """
        INSERT INTO subscriptions (telegram_user_id, plan, expires_at, last_payment_charge_id, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            plan=excluded.plan,
            expires_at=excluded.expires_at,
            last_payment_charge_id=excluded.last_payment_charge_id,
            updated_at=excluded.updated_at
        """,
        (telegram_user_id, plan, new_expiry, charge_id, now_ist()),
    )
    return new_expiry


def execute(
    query,
    params=(),
    fetch=False,
    many=False,
    return_lastrowid=False
):
    conn = get_connection()
    cursor = conn.cursor()

    if many:
        cursor.executemany(query, params)
    else:
        cursor.execute(query, params)

    result = None

    if fetch:
        result = cursor.fetchall()

    elif return_lastrowid:
        result = cursor.lastrowid

    conn.commit()
    conn.close()

    return result


def get_or_create_group_id(chat_id: int, chat_title: str = None) -> int:
    """
    Returns the group_id for a Telegram chat, creating the `groups` row if
    this is the first time we've seen this chat_id. Shared by /start and the
    "bot added to a group" / "new members added" handlers so a group row can
    exist WITHOUT anyone having sent /start yet.
    """
    group = execute(
        "SELECT group_id FROM groups WHERE telegram_chat_id = ?",
        (chat_id,),
        fetch=True,
    )
    if group:
        return group[0][0]

    # Two Telegram updates for the same brand-new chat (e.g. the bot's own
    # my_chat_member update and the first new_chat_members update) can land
    # on the event loop close enough together that both see "no row yet"
    # from the SELECT above and both try to INSERT. telegram_chat_id is
    # UNIQUE, so the loser used to blow up with an unhandled
    # IntegrityError -- which, for new_chat_members_handler, meant the
    # welcome message never got sent at all. Treat that race as a cache
    # miss and just re-select instead of crashing.
    try:
        execute(
            "INSERT INTO groups (telegram_chat_id, group_name) VALUES (?, ?)",
            (chat_id, chat_title or "Group"),
        )
    except sqlite3.IntegrityError:
        pass

    return execute(
        "SELECT group_id FROM groups WHERE telegram_chat_id = ?",
        (chat_id,),
        fetch=True,
    )[0][0]


# -----------------------------
# PER-USER SUBGROUP HELPERS
# -----------------------------
# NOTE: `expires_at` is NOT NULL in the schema, but this feature has no real
# expiry -- a saved subgroup should stay active until the owning user resets
# it. We store a far-future sentinel instead of altering the schema. Nothing
# ever reads expires_at to enforce expiry; it's a leftover artifact of the
# original table design, kept only so we don't need a migration.
_NO_EXPIRY_SENTINEL = "2099-12-31 23:59:59"


def get_user_subgroup_member_ids(group_id: int, user_id: int):
    """
    Returns this user's saved subgroup as a list of member_ids, or None if
    they've never saved one (or reset it) -- meaning "use every active group
    member" as the default, and that default automatically includes anyone
    who joins the group later.
    """
    row = execute(
        "SELECT selected_member_ids FROM user_subgroups WHERE group_id=? AND user_id=?",
        (group_id, user_id),
        fetch=True,
    )
    if not row or not row[0][0]:
        return None
    return [int(x) for x in row[0][0].split(",") if x.strip()]


def save_user_subgroup(group_id: int, user_id: int, member_ids: list):
    """
    Persists this user's subgroup selection so it's reused for every future
    expense they add, until they reset it. Stays active across bot restarts.
    """
    ids_str = ",".join(str(m) for m in member_ids)

    existing = execute(
        "SELECT 1 FROM user_subgroups WHERE group_id=? AND user_id=?",
        (group_id, user_id),
        fetch=True,
    )
    if existing:
        execute(
            "UPDATE user_subgroups SET selected_member_ids=?, expires_at=? WHERE group_id=? AND user_id=?",
            (ids_str, _NO_EXPIRY_SENTINEL, group_id, user_id),
        )
    else:
        execute(
            "INSERT INTO user_subgroups (user_id, group_id, selected_member_ids, expires_at) VALUES (?, ?, ?, ?)",
            (user_id, group_id, ids_str, _NO_EXPIRY_SENTINEL),
        )


def reset_user_subgroup(group_id: int, user_id: int):
    """Deletes this user's saved subgroup, reverting them to 'all active group members'."""
    execute(
        "DELETE FROM user_subgroups WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    )

# Shared exclusion clause: keeps "real" group spend distinct from settlement
# log entries and personal (non-split) expenses. Reused across every
# monthly-scoped query below so the numbers stay consistent with each other.
# Two variants are kept explicit (rather than derived via string substitution)
# so each query's SQL is directly readable/greppable.
_EXPENSE_FILTER_SQL = """
    AND (category IS NULL OR LOWER(category) NOT IN ('settlement', 'settle'))
    AND (split_type IS NULL OR LOWER(split_type) != 'personal')
    AND (status IS NULL OR status != 'declined')
"""

_EXPENSE_FILTER_SQL_ALIASED = """
    AND (e.category IS NULL OR LOWER(e.category) NOT IN ('settlement', 'settle'))
    AND (e.split_type IS NULL OR LOWER(e.split_type) != 'personal')
    AND (e.status IS NULL OR e.status != 'declined')
"""


# -----------------------------
# EXPENSE DECLINE WINDOW
# -----------------------------
# How long a linked participant has to decline their share of an expense
# after it's added, before it's locked in for good. Kept here (rather than
# only in handlers/expense.py) so any future job/cron that wants to expire
# stale decline buttons can import the same constant.
EXPENSE_DECLINE_WINDOW_HOURS = 12


def expense_decline_deadline_passed(created_at: str) -> bool:
    """True if more than EXPENSE_DECLINE_WINDOW_HOURS have elapsed since
    `created_at` (an IST 'YYYY-MM-DD HH:MM:SS' string, as written by
    now_ist()). No scheduler needed -- this is checked on-demand whenever
    someone taps Decline."""
    created_dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    now_naive = datetime.now(IST).replace(tzinfo=None)
    return now_naive > created_dt + timedelta(hours=EXPENSE_DECLINE_WINDOW_HOURS)


def record_expense_notification(expense_id: int, member_id: int, telegram_user_id: int, message_id: int):
    """Remembers where a participant's 'your share' DM was sent, so it can
    be edited later (e.g. once someone else declines)."""
    execute(
        """
        INSERT INTO expense_notifications (expense_id, member_id, telegram_user_id, message_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(expense_id, member_id) DO UPDATE SET
            telegram_user_id=excluded.telegram_user_id,
            message_id=excluded.message_id
        """,
        (expense_id, member_id, telegram_user_id, message_id),
    )


def get_expense_notifications(expense_id: int):
    """Returns [(member_id, telegram_user_id, message_id), ...] for every
    participant DM sent for this expense."""
    return execute(
        "SELECT member_id, telegram_user_id, message_id FROM expense_notifications WHERE expense_id=?",
        (expense_id,),
        fetch=True,
    ) or []


# -----------------------------
# MEMBER REMOVAL (member left/was removed from the Telegram group)
# -----------------------------
def deactivate_member(group_id: int, telegram_user_id: int):
    """
    Marks a member inactive when Telegram tells us they've left or been
    removed from the group chat. Without this, a member who was removed
    (especially one who never confirmed via the join flow, i.e. still
    'pending') stays is_active=1 forever, so every "active members" query
    -- including the equal-split participant list -- keeps including them
    and equal splits get stuck waiting for someone who isn't in the group
    anymore to confirm/join.

    We deliberately keep their row (history/expense_shares reference it)
    and just flip is_active=0, mirroring how the rest of the schema treats
    membership.
    """
    execute(
        "UPDATE members SET is_active=0 WHERE group_id=? AND telegram_user_id=?",
        (group_id, telegram_user_id),
    )


# -----------------------------
# CURRENCY
# -----------------------------
def get_member_currency(member_id: int) -> str:
    """Returns a member's personal display/entry currency (defaults to
    DEFAULT_CURRENCY for anyone who hasn't changed it)."""
    row = execute(
        "SELECT currency FROM members WHERE member_id=?",
        (member_id,),
        fetch=True,
    )
    return (row[0][0] if row and row[0][0] else DEFAULT_CURRENCY)


def set_member_currency(group_id: int, telegram_user_id: int, currency: str):
    """Updates the acting user's personal currency for this group. New
    expenses they pay for will be recorded in this currency going forward;
    past expenses are untouched."""
    execute(
        "UPDATE members SET currency=? WHERE group_id=? AND telegram_user_id=?",
        (currency, group_id, telegram_user_id),
    )


def stamp_expense_currency(expense_id: int, payer_member_id: int) -> str:
    """
    Freezes the payer's *current* currency onto the expense row at the
    moment it's created, and returns it. Expenses always display/report in
    whatever currency they were entered in, even if the payer later changes
    their personal currency setting -- history must never retroactively
    change.
    """
    currency = get_member_currency(payer_member_id)
    execute("UPDATE expenses SET currency=? WHERE expense_id=?", (currency, expense_id))
    return currency


def save_share_conversion(expense_id: int, member_id: int, converted_amount, converted_currency, fx_rate):
    """
    Freezes a single participant's share, converted into their own display
    currency, at expense-creation time. Storing the snapshot (rather than
    recomputing on every read) is what guarantees the shown amount never
    moves later just because exchange rates did.
    """
    execute(
        "UPDATE expense_shares SET converted_amount=?, converted_currency=?, fx_rate=? "
        "WHERE expense_id=? AND member_id=?",
        (converted_amount, converted_currency, fx_rate, expense_id, member_id),
    )


def _month_total_expenditure(group_id: int, year: int, month: int) -> float:
    """Small helper used for month-over-month comparison (previous month's total)."""
    month_filter = f"{year}-{month:02d}%"
    result = execute(
        f"""
        SELECT SUM(amount) FROM expenses
        WHERE group_id = ?
          AND (created_at LIKE ? OR strftime('%Y-%m', created_at) LIKE ?)
          {_EXPENSE_FILTER_SQL}
        """,
        (group_id, month_filter, month_filter),
        fetch=True
    )
    return result[0][0] if result and result[0][0] is not None else 0.0


def _previous_month(year: int, month: int):
    if month == 1:
        return year - 1, 12
    return year, month - 1


def get_monthly_summary_data(group_id: int, year: int, month: int):
    """Fetches all raw metrics needed for the Monthly Summary report with robust date matching and filtering.

    Note on scope: everything derived from `expenses`/`settlements` below is
    strictly filtered to (year, month) and is a historical snapshot — it will
    never change once the month has passed. `debts` (current outstanding
    balances) is deliberately NOT month-filtered: debts carry forward until
    settled, so it always reflects TODAY's live balances regardless of which
    month is being viewed. The caller/renderer must label it as "current",
    not as belonging to the selected month.
    """

    month_filter = f"{year}-{month:02d}%"

    # 1. Total Group Expenditure (Excluding settlements and personal expenses)
    total_group_exp = _month_total_expenditure(group_id, year, month)

    # 1b. Month-over-month comparison
    prev_year, prev_month = _previous_month(year, month)
    prev_month_exp = _month_total_expenditure(group_id, prev_year, prev_month)
    if prev_month_exp > 0:
        mom_change_pct = ((total_group_exp - prev_month_exp) / prev_month_exp) * 100
    else:
        mom_change_pct = None  # No baseline to compare against

    # 2. Total Settlements Executed this Month (+ how many settlement events)
    settlements_query = execute(
        """
        SELECT SUM(s.amount), COUNT(*) FROM settlements s
        WHERE s.group_id = ?
          AND (s.settled_at LIKE ? OR strftime('%Y-%m', s.settled_at) LIKE ?)
        """,
        (group_id, month_filter, month_filter),
        fetch=True
    )
    total_settlements = settlements_query[0][0] if settlements_query and settlements_query[0][0] is not None else 0.0
    settlement_count = settlements_query[0][1] if settlements_query else 0

    # 3. Top Spenders (Strictly excluding settlements/personal) — full ranking
    paid_ranking = execute(
        f"""
        SELECT m.display_name, SUM(e.amount) as total_paid
        FROM expenses e
        JOIN members m ON e.payer_member_id = m.member_id
        WHERE e.group_id = ?
          AND (e.created_at LIKE ? OR strftime('%Y-%m', e.created_at) LIKE ?)
          {_EXPENSE_FILTER_SQL_ALIASED}
        GROUP BY m.member_id
        ORDER BY total_paid DESC
        """,
        (group_id, month_filter, month_filter),
        fetch=True
    )

    top_3 = paid_ranking[:3] if paid_ranking else []

    # Lowest spender: Only pick from ranking if there are multiple spenders, otherwise None
    least_paid = paid_ranking[-1] if paid_ranking and len(paid_ranking) > 1 else None

    # 4. Category Breakdown (same exclusions as total spend, so it sums to total_group_exp)
    category_breakdown = execute(
        f"""
        SELECT category, SUM(amount) as cat_total
        FROM expenses
        WHERE group_id = ?
          AND (created_at LIKE ? OR strftime('%Y-%m', created_at) LIKE ?)
          {_EXPENSE_FILTER_SQL}
        GROUP BY category
        ORDER BY cat_total DESC
        """,
        (group_id, month_filter, month_filter),
        fetch=True
    )

    # 5. Transaction count & average expense size (same filter, this month only)
    txn_stats = execute(
        f"""
        SELECT COUNT(*), AVG(amount) FROM expenses
        WHERE group_id = ?
          AND (created_at LIKE ? OR strftime('%Y-%m', created_at) LIKE ?)
          {_EXPENSE_FILTER_SQL}
        """,
        (group_id, month_filter, month_filter),
        fetch=True
    )
    transaction_count = txn_stats[0][0] if txn_stats else 0
    avg_expense = txn_stats[0][1] if txn_stats and txn_stats[0][1] is not None else 0.0

    # 6. Biggest single expense this month
    biggest_expense_row = execute(
        f"""
        SELECT e.amount, e.category, e.description, m.display_name
        FROM expenses e
        JOIN members m ON e.payer_member_id = m.member_id
        WHERE e.group_id = ?
          AND (e.created_at LIKE ? OR strftime('%Y-%m', e.created_at) LIKE ?)
          {_EXPENSE_FILTER_SQL_ALIASED}
        ORDER BY e.amount DESC
        LIMIT 1
        """,
        (group_id, month_filter, month_filter),
        fetch=True
    )
    biggest_expense = biggest_expense_row[0] if biggest_expense_row else None

    # 7. Active Debts — CURRENT balances, NOT scoped to the selected month.
    # These carry forward until settled, so this is always "as of today".
    debts = execute(
        """
        SELECT m_debtor.display_name, m_creditor.display_name, md.amount
        FROM member_debts md
        JOIN members m_debtor ON md.debtor_member_id = m_debtor.member_id
        JOIN members m_creditor ON md.creditor_member_id = m_creditor.member_id
        WHERE md.group_id = ? AND md.amount > 0.01
        """,
        (group_id,),
        fetch=True
    )

    return {
        "total_group_exp": total_group_exp,
        "prev_month_exp": prev_month_exp,
        "mom_change_pct": mom_change_pct,
        "total_settlements": total_settlements,
        "settlement_count": settlement_count,
        "paid_ranking": paid_ranking,
        "top_paid": top_3,
        "least_paid": least_paid,
        "category_breakdown": category_breakdown,
        "transaction_count": transaction_count,
        "avg_expense": avg_expense,
        "biggest_expense": biggest_expense,
        "debts": debts,  # NOTE: current/live, not month-scoped — see docstring above
    }
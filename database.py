import sqlite3
import os

os.makedirs("data", exist_ok=True)

conn = sqlite3.connect(
    "data/splitmate.db",
    check_same_thread=False
)

cursor = conn.cursor()


def create_tables():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expenses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        payer_id INTEGER,
        payer_name TEXT,
        amount REAL,
        category TEXT,
        split_type TEXT,
        share1 REAL,
        share2 REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()


def save_expense(
    chat_id,
    payer_id,
    payer_name,
    amount,
    category,
    split_type,
    share1=0,
    share2=0
):
    cursor.execute(
        """
        INSERT INTO expenses(
            chat_id,
            payer_id,
            payer_name,
            amount,
            category,
            split_type,
            share1,
            share2
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            payer_id,
            payer_name,
            amount,
            category,
            split_type,
            share1,
            share2
        )
    )
    conn.commit()


def get_history(chat_id):
    cursor.execute(
        """
        SELECT payer_name, amount, category, split_type, created_at
        FROM expenses
        WHERE chat_id = ?
        ORDER BY id DESC
        """,
        (chat_id,)
    )

    return cursor.fetchall()

def get_balances(chat_id):
    cursor.execute(
        """
        SELECT payer_name, SUM(amount)
        FROM expenses
        WHERE chat_id = ?
        GROUP BY payer_name
        """,
        (chat_id,)
    )

    return cursor.fetchall()
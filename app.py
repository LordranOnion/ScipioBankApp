from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "family_budget.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["DATABASE"] = str(DATABASE)


# -----------------------------
# Database helpers
# -----------------------------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def table_exists(db: sqlite3.Connection, table_name: str) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def column_exists(db: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def init_db() -> None:
    db = get_db()
    schema = (BASE_DIR / "schema.sql").read_text(encoding="utf-8")
    db.executescript(schema)
    db.commit()


def migrate_legacy_transactions_if_needed() -> None:
    db = get_db()

    if not table_exists(db, "transactions"):
        return

    if column_exists(db, "transactions", "bank_account_id"):
        return

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bank_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            account_name TEXT NOT NULL,
            bank_name TEXT,
            account_identifier TEXT,
            initial_balance REAL NOT NULL DEFAULT 0 CHECK (initial_balance >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES family_members(id) ON DELETE CASCADE
        )
        """
    )

    members_with_transactions = db.execute(
        "SELECT DISTINCT member_id FROM transactions"
    ).fetchall()

    account_map: dict[int, int] = {}

    for row in members_with_transactions:
        member_id = int(row["member_id"])

        existing = db.execute(
            """
            SELECT id
            FROM bank_accounts
            WHERE member_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (member_id,),
        ).fetchone()

        if existing:
            account_map[member_id] = int(existing["id"])
        else:
            cursor = db.execute(
                """
                INSERT INTO bank_accounts (
                    member_id, account_name, bank_name, account_identifier, initial_balance
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (member_id, "Main account", "Migrated account", None, 0),
            )
            account_map[member_id] = int(cursor.lastrowid)

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            bank_account_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
            category TEXT,
            amount REAL NOT NULL CHECK (amount > 0),
            note TEXT,
            transaction_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES family_members(id) ON DELETE CASCADE,
            FOREIGN KEY (bank_account_id) REFERENCES bank_accounts(id) ON DELETE CASCADE
        )
        """
    )

    old_transactions = db.execute(
        """
        SELECT id, member_id, kind, category, amount, note, transaction_date, created_at
        FROM transactions
        ORDER BY id ASC
        """
    ).fetchall()

    for tx in old_transactions:
        member_id = int(tx["member_id"])
        bank_account_id = account_map[member_id]

        db.execute(
            """
            INSERT INTO transactions_new (
                id, member_id, bank_account_id, kind, category, amount, note, transaction_date, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tx["id"],
                member_id,
                bank_account_id,
                tx["kind"],
                tx["category"],
                tx["amount"],
                tx["note"],
                tx["transaction_date"],
                tx["created_at"],
            ),
        )

    db.execute("DROP TABLE transactions")
    db.execute("ALTER TABLE transactions_new RENAME TO transactions")
    db.commit()


def migrate_fixed_expenses_to_household_if_needed() -> None:
    db = get_db()

    if not table_exists(db, "fixed_expenses"):
        return

    has_member_id = column_exists(db, "fixed_expenses", "member_id")
    has_bank_account_id = column_exists(db, "fixed_expenses", "bank_account_id")

    if not has_member_id and not has_bank_account_id:
        return

    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS fixed_expenses_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            amount REAL NOT NULL CHECK (amount > 0),
            note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    old_rows = db.execute(
        """
        SELECT id, title, amount, note, created_at
        FROM fixed_expenses
        ORDER BY id ASC
        """
    ).fetchall()

    for row in old_rows:
        db.execute(
            """
            INSERT INTO fixed_expenses_new (id, title, amount, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["title"],
                row["amount"],
                row["note"],
                row["created_at"],
            ),
        )

    db.execute("DROP TABLE fixed_expenses")
    db.execute("ALTER TABLE fixed_expenses_new RENAME TO fixed_expenses")
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")


@app.before_request
def ensure_db_ready() -> None:
    db = get_db()

    if not table_exists(db, "family_members"):
        init_db()
    else:
        schema = (BASE_DIR / "schema.sql").read_text(encoding="utf-8")
        db.executescript(schema)
        db.commit()

    migrate_legacy_transactions_if_needed()
    migrate_fixed_expenses_to_household_if_needed()


@app.cli.command("init-db")
def init_db_command() -> None:
    init_db()
    print("Database initialized.")


# -----------------------------
# Utility helpers
# -----------------------------
def query_one(query: str, params: tuple = ()) -> sqlite3.Row | None:
    return get_db().execute(query, params).fetchone()


def query_all(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    return get_db().execute(query, params).fetchall()


def current_month_bounds() -> tuple[str, str]:
    today = date.today()
    start = today.replace(day=1)

    if today.month == 12:
        end = today.replace(year=today.year + 1, month=1, day=1)
    else:
        end = today.replace(month=today.month + 1, day=1)

    return start.isoformat(), end.isoformat()


def current_month_key() -> str:
    return date.today().strftime("%Y-%m")


def current_month_label() -> str:
    return date.today().strftime("%B %Y")


def calculate_account_summary(account_id: int) -> dict[str, float]:
    db = get_db()

    account = db.execute(
        """
        SELECT initial_balance
        FROM bank_accounts
        WHERE id = ?
        """,
        (account_id,),
    ).fetchone()

    if account is None:
        return {
            "initial_balance": 0.0,
            "total_income": 0.0,
            "total_expenses": 0.0,
            "balance": 0.0,
            "month_income": 0.0,
            "month_expenses": 0.0,
        }

    totals = db.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN kind = 'income' THEN amount END), 0) AS total_income,
            COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount END), 0) AS total_expenses
        FROM transactions
        WHERE bank_account_id = ?
        """,
        (account_id,),
    ).fetchone()

    month_start, month_end = current_month_bounds()
    monthly = db.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN kind = 'income' THEN amount END), 0) AS month_income,
            COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount END), 0) AS month_expenses
        FROM transactions
        WHERE bank_account_id = ?
          AND transaction_date >= ?
          AND transaction_date < ?
        """,
        (account_id, month_start, month_end),
    ).fetchone()

    initial_balance = float(account["initial_balance"])
    total_income = float(totals["total_income"])
    total_expenses = float(totals["total_expenses"])

    return {
        "initial_balance": initial_balance,
        "total_income": total_income,
        "total_expenses": total_expenses,
        "balance": initial_balance + total_income - total_expenses,
        "month_income": float(monthly["month_income"]),
        "month_expenses": float(monthly["month_expenses"]),
    }


def calculate_member_summary(member_id: int) -> dict[str, float | int]:
    db = get_db()

    accounts_row = db.execute(
        """
        SELECT
            COUNT(*) AS accounts_count,
            COALESCE(SUM(initial_balance), 0) AS initial_balance_total
        FROM bank_accounts
        WHERE member_id = ?
        """,
        (member_id,),
    ).fetchone()

    totals = db.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN t.kind = 'income' THEN t.amount END), 0) AS total_income,
            COALESCE(SUM(CASE WHEN t.kind = 'expense' THEN t.amount END), 0) AS total_expenses
        FROM transactions t
        JOIN bank_accounts b ON b.id = t.bank_account_id
        WHERE b.member_id = ?
        """,
        (member_id,),
    ).fetchone()

    month_start, month_end = current_month_bounds()
    monthly = db.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN t.kind = 'income' THEN t.amount END), 0) AS month_income,
            COALESCE(SUM(CASE WHEN t.kind = 'expense' THEN t.amount END), 0) AS month_expenses
        FROM transactions t
        JOIN bank_accounts b ON b.id = t.bank_account_id
        WHERE b.member_id = ?
          AND t.transaction_date >= ?
          AND t.transaction_date < ?
        """,
        (member_id, month_start, month_end),
    ).fetchone()

    initial_balance_total = float(accounts_row["initial_balance_total"])
    total_income = float(totals["total_income"])
    total_expenses = float(totals["total_expenses"])

    return {
        "accounts_count": int(accounts_row["accounts_count"]),
        "initial_balance_total": initial_balance_total,
        "total_income": total_income,
        "total_expenses": total_expenses,
        "balance": initial_balance_total + total_income - total_expenses,
        "month_income": float(monthly["month_income"]),
        "month_expenses": float(monthly["month_expenses"]),
    }


def calculate_household_fixed_summary() -> dict[str, float]:
    db = get_db()
    month_key = current_month_key()

    row = db.execute(
        """
        SELECT
            COALESCE(SUM(f.amount), 0) AS planned_total,
            COALESCE(SUM(CASE WHEN s.is_paid = 1 THEN f.amount ELSE 0 END), 0) AS paid_total
        FROM fixed_expenses f
        LEFT JOIN fixed_expense_status s
            ON s.fixed_expense_id = f.id
           AND s.month_key = ?
        """,
        (month_key,),
    ).fetchone()

    planned_total = float(row["planned_total"])
    paid_total = float(row["paid_total"])

    return {
        "planned_total": planned_total,
        "paid_total": paid_total,
        "remaining_total": planned_total - paid_total,
    }


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def index():
    members = query_all(
        "SELECT id, full_name, relation, created_at FROM family_members ORDER BY full_name COLLATE NOCASE"
    )

    member_cards: list[dict[str, Any]] = []
    family_total = 0.0

    for member in members:
        summary = calculate_member_summary(int(member["id"]))
        family_total += float(summary["balance"])
        member_cards.append({"member": member, "summary": summary})

    fixed_panel_items = query_all(
        """
        SELECT
            f.id,
            f.title,
            f.amount,
            COALESCE(s.is_paid, 0) AS is_paid
        FROM fixed_expenses f
        LEFT JOIN fixed_expense_status s
            ON s.fixed_expense_id = f.id
           AND s.month_key = ?
        ORDER BY f.title COLLATE NOCASE, f.id ASC
        """,
        (current_month_key(),),
    )

    household_fixed_summary = calculate_household_fixed_summary()

    return render_template(
        "index.html",
        member_cards=member_cards,
        family_total=family_total,
        fixed_panel_items=fixed_panel_items,
        household_fixed_summary=household_fixed_summary,
        month_label=current_month_label(),
    )


@app.post("/members")
def add_member():
    full_name = request.form.get("full_name", "").strip()
    relation = request.form.get("relation", "").strip()

    if not full_name:
        flash("Please provide a family member name.", "error")
        return redirect(url_for("index"))

    db = get_db()
    db.execute(
        "INSERT INTO family_members (full_name, relation) VALUES (?, ?)",
        (full_name, relation or None),
    )
    db.commit()

    flash(f"Added {full_name}.", "success")
    return redirect(url_for("index"))


@app.post("/member/<int:member_id>/accounts")
def add_bank_account(member_id: int):
    member = query_one("SELECT id, full_name FROM family_members WHERE id = ?", (member_id,))
    if member is None:
        abort(404)

    account_name = request.form.get("account_name", "").strip()
    bank_name = request.form.get("bank_name", "").strip()
    account_identifier = request.form.get("account_identifier", "").strip()
    initial_balance_raw = request.form.get("initial_balance", "").strip() or "0"

    if not account_name:
        flash("Please provide an account name.", "error")
        return redirect(url_for("member_detail", member_id=member_id))

    try:
        initial_balance = round(float(initial_balance_raw), 2)
        if initial_balance < 0:
            raise ValueError
    except ValueError:
        flash("Initial balance must be zero or a positive number.", "error")
        return redirect(url_for("member_detail", member_id=member_id))

    db = get_db()
    db.execute(
        """
        INSERT INTO bank_accounts (member_id, account_name, bank_name, account_identifier, initial_balance)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            member_id,
            account_name,
            bank_name or None,
            account_identifier or None,
            initial_balance,
        ),
    )
    db.commit()

    flash(f"New bank account added for {member['full_name']}.", "success")
    return redirect(url_for("member_detail", member_id=member_id))


@app.post("/accounts/<int:account_id>/delete")
def delete_bank_account(account_id: int):
    row = query_one(
        """
        SELECT b.id, b.member_id, b.account_name, m.full_name
        FROM bank_accounts b
        JOIN family_members m ON m.id = b.member_id
        WHERE b.id = ?
        """,
        (account_id,),
    )
    if row is None:
        abort(404)

    db = get_db()
    db.execute("DELETE FROM bank_accounts WHERE id = ?", (account_id,))
    db.commit()

    flash(
        f"Removed account '{row['account_name']}' from {row['full_name']}. Related transactions were also removed.",
        "success",
    )
    return redirect(url_for("member_detail", member_id=row["member_id"]))


@app.route("/member/<int:member_id>")
def member_detail(member_id: int):
    member = query_one(
        "SELECT id, full_name, relation, created_at FROM family_members WHERE id = ?",
        (member_id,),
    )
    if member is None:
        abort(404)

    summary = calculate_member_summary(member_id)

    accounts = query_all(
        """
        SELECT id, account_name, bank_name, account_identifier, initial_balance, created_at
        FROM bank_accounts
        WHERE member_id = ?
        ORDER BY id DESC
        """,
        (member_id,),
    )

    account_cards: list[dict[str, Any]] = []
    for account in accounts:
        account_cards.append(
            {
                "account": account,
                "summary": calculate_account_summary(int(account["id"])),
            }
        )

    transactions = query_all(
        """
        SELECT
            t.id,
            t.kind,
            t.category,
            t.amount,
            t.note,
            t.transaction_date,
            t.created_at,
            b.id AS bank_account_id,
            b.account_name,
            b.bank_name,
            b.account_identifier
        FROM transactions t
        JOIN bank_accounts b ON b.id = t.bank_account_id
        WHERE t.member_id = ?
        ORDER BY t.transaction_date DESC, t.id DESC
        """,
        (member_id,),
    )

    pending_fixed_expenses = query_all(
        """
        SELECT
            f.id,
            f.title,
            f.amount
        FROM fixed_expenses f
        LEFT JOIN fixed_expense_status s
            ON s.fixed_expense_id = f.id
           AND s.month_key = ?
        WHERE COALESCE(s.is_paid, 0) = 0
        ORDER BY f.title COLLATE NOCASE
        """,
        (current_month_key(),),
    )

    return render_template(
        "member_detail.html",
        member=member,
        summary=summary,
        accounts=accounts,
        account_cards=account_cards,
        transactions=transactions,
        today=date.today().isoformat(),
        pending_fixed_expenses=pending_fixed_expenses,
    )


@app.route("/fixed-expenses")
def manage_fixed_expenses():
    fixed_expenses = query_all(
        """
        SELECT
            f.id,
            f.title,
            f.amount,
            f.note,
            COALESCE(s.is_paid, 0) AS is_paid
        FROM fixed_expenses f
        LEFT JOIN fixed_expense_status s
            ON s.fixed_expense_id = f.id
           AND s.month_key = ?
        ORDER BY f.title COLLATE NOCASE, f.id DESC
        """,
        (current_month_key(),),
    )

    fixed_summary = calculate_household_fixed_summary()

    return render_template(
        "fixed_expenses_manage.html",
        fixed_expenses=fixed_expenses,
        fixed_summary=fixed_summary,
        month_label=current_month_label(),
    )


@app.post("/fixed-expenses")
def add_fixed_expense():
    title = request.form.get("title", "").strip()
    note = request.form.get("note", "").strip()
    amount_raw = request.form.get("amount", "").strip()

    if not title:
        flash("Please provide a fixed expense title.", "error")
        return redirect(url_for("manage_fixed_expenses"))

    try:
        amount = round(float(amount_raw), 2)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash("Amount must be a positive number.", "error")
        return redirect(url_for("manage_fixed_expenses"))

    db = get_db()
    db.execute(
        """
        INSERT INTO fixed_expenses (title, amount, note)
        VALUES (?, ?, ?)
        """,
        (title, amount, note or None),
    )
    db.commit()

    flash(f"Fixed expense '{title}' added.", "success")
    return redirect(url_for("manage_fixed_expenses"))


@app.route("/fixed-expenses/<int:fixed_expense_id>/edit", methods=["GET", "POST"])
def edit_fixed_expense(fixed_expense_id: int):
    fixed_expense = query_one(
        """
        SELECT id, title, amount, note
        FROM fixed_expenses
        WHERE id = ?
        """,
        (fixed_expense_id,),
    )
    if fixed_expense is None:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        note = request.form.get("note", "").strip()
        amount_raw = request.form.get("amount", "").strip()

        if not title:
            flash("Please provide a fixed expense title.", "error")
            return redirect(url_for("edit_fixed_expense", fixed_expense_id=fixed_expense_id))

        try:
            amount = round(float(amount_raw), 2)
            if amount <= 0:
                raise ValueError
        except ValueError:
            flash("Amount must be a positive number.", "error")
            return redirect(url_for("edit_fixed_expense", fixed_expense_id=fixed_expense_id))

        db = get_db()
        db.execute(
            """
            UPDATE fixed_expenses
            SET title = ?, amount = ?, note = ?
            WHERE id = ?
            """,
            (title, amount, note or None, fixed_expense_id),
        )
        db.commit()

        flash("Fixed expense updated.", "success")
        return redirect(url_for("manage_fixed_expenses"))

    return render_template(
        "fixed_expense_edit.html",
        fixed_expense=fixed_expense,
    )


@app.post("/fixed-expenses/<int:fixed_expense_id>/delete")
def delete_fixed_expense(fixed_expense_id: int):
    row = query_one(
        """
        SELECT id, title
        FROM fixed_expenses
        WHERE id = ?
        """,
        (fixed_expense_id,),
    )
    if row is None:
        abort(404)

    db = get_db()
    db.execute("DELETE FROM fixed_expenses WHERE id = ?", (fixed_expense_id,))
    db.commit()

    flash(f"Removed fixed expense '{row['title']}'.", "success")
    return redirect(url_for("manage_fixed_expenses"))


@app.post("/member/<int:member_id>/transactions")
def add_transaction(member_id: int):
    member = query_one("SELECT id, full_name FROM family_members WHERE id = ?", (member_id,))
    if member is None:
        abort(404)

    kind = request.form.get("kind", "expense").strip().lower()
    category = request.form.get("category", "").strip()
    note = request.form.get("note", "").strip()
    transaction_date = request.form.get("transaction_date", "").strip() or date.today().isoformat()
    amount_raw = request.form.get("amount", "").strip()
    bank_account_id_raw = request.form.get("bank_account_id", "").strip()
    fixed_expense_id_raw = request.form.get("fixed_expense_id", "").strip()

    if kind not in {"income", "expense"}:
        flash("Invalid transaction type.", "error")
        return redirect(url_for("member_detail", member_id=member_id))

    try:
        amount = round(float(amount_raw), 2)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash("Amount must be a positive number.", "error")
        return redirect(url_for("member_detail", member_id=member_id))

    try:
        datetime.strptime(transaction_date, "%Y-%m-%d")
    except ValueError:
        flash("Invalid date format.", "error")
        return redirect(url_for("member_detail", member_id=member_id))

    try:
        bank_account_id = int(bank_account_id_raw)
    except ValueError:
        flash("Please select a bank account.", "error")
        return redirect(url_for("member_detail", member_id=member_id))

    account = query_one(
        """
        SELECT id, account_name
        FROM bank_accounts
        WHERE id = ? AND member_id = ?
        """,
        (bank_account_id, member_id),
    )
    if account is None:
        flash("Selected bank account is not valid for this member.", "error")
        return redirect(url_for("member_detail", member_id=member_id))

    linked_fixed_expense = None

    if fixed_expense_id_raw:
        try:
            fixed_expense_id = int(fixed_expense_id_raw)
        except ValueError:
            flash("Invalid fixed expense selection.", "error")
            return redirect(url_for("member_detail", member_id=member_id))

        linked_fixed_expense = query_one(
            """
            SELECT id, title, amount, note
            FROM fixed_expenses
            WHERE id = ?
            """,
            (fixed_expense_id,),
        )
        if linked_fixed_expense is None:
            flash("Selected fixed expense does not exist.", "error")
            return redirect(url_for("member_detail", member_id=member_id))

        if kind != "expense":
            flash("A fixed expense can only be linked to an expense transaction.", "error")
            return redirect(url_for("member_detail", member_id=member_id))

        if abs(float(linked_fixed_expense["amount"]) - amount) > 0.009:
            flash("When paying a fixed expense, the amount must match the checklist amount.", "error")
            return redirect(url_for("member_detail", member_id=member_id))

        status = query_one(
            """
            SELECT is_paid, transaction_id
            FROM fixed_expense_status
            WHERE fixed_expense_id = ? AND month_key = ?
            """,
            (fixed_expense_id, current_month_key()),
        )
        if status is not None and int(status["is_paid"]) == 1 and status["transaction_id"] is not None:
            existing_tx = query_one(
                "SELECT id FROM transactions WHERE id = ?",
                (status["transaction_id"],),
            )
            if existing_tx is not None:
                flash("This fixed expense is already paid for the current month.", "error")
                return redirect(url_for("member_detail", member_id=member_id))

        if not category:
            category = str(linked_fixed_expense["title"])
        if not note:
            note = str(linked_fixed_expense["note"] or "Household fixed expense")

    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO transactions (member_id, bank_account_id, kind, category, amount, note, transaction_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            member_id,
            bank_account_id,
            kind,
            category or None,
            amount,
            note or None,
            transaction_date,
        ),
    )
    transaction_id = int(cursor.lastrowid)

    if linked_fixed_expense is not None:
        db.execute(
            """
            INSERT INTO fixed_expense_status (
                fixed_expense_id, month_key, is_paid, paid_date, transaction_id
            )
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(fixed_expense_id, month_key)
            DO UPDATE SET
                is_paid = 1,
                paid_date = excluded.paid_date,
                transaction_id = excluded.transaction_id
            """,
            (
                int(linked_fixed_expense["id"]),
                current_month_key(),
                transaction_date,
                transaction_id,
            ),
        )

    db.commit()

    if linked_fixed_expense is not None:
        flash(
            f"Expense recorded and checklist item '{linked_fixed_expense['title']}' was marked as paid.",
            "success",
        )
    else:
        action = "Income added" if kind == "income" else "Expense added"
        flash(f"{action} in account '{account['account_name']}' for {member['full_name']}.", "success")

    return redirect(url_for("member_detail", member_id=member_id))


@app.post("/transactions/<int:transaction_id>/delete")
def delete_transaction(transaction_id: int):
    row = query_one(
        "SELECT id, member_id FROM transactions WHERE id = ?",
        (transaction_id,),
    )
    if row is None:
        abort(404)

    db = get_db()
    db.execute(
        """
        UPDATE fixed_expense_status
        SET is_paid = 0,
            paid_date = NULL,
            transaction_id = NULL
        WHERE transaction_id = ?
        """,
        (transaction_id,),
    )
    db.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
    db.commit()

    flash("Transaction removed.", "success")
    return redirect(url_for("member_detail", member_id=row["member_id"]))


@app.post("/member/<int:member_id>/delete")
def delete_member(member_id: int):
    row = query_one("SELECT full_name FROM family_members WHERE id = ?", (member_id,))
    if row is None:
        abort(404)

    db = get_db()
    db.execute("DELETE FROM family_members WHERE id = ?", (member_id,))
    db.commit()

    flash(f"Removed {row['full_name']} and all related accounts and transactions.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=True)
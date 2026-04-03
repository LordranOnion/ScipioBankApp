from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, date
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


def init_db() -> None:
    db = get_db()
    with closing(open(BASE_DIR / "schema.sql", "r", encoding="utf-8")) as f:
        db.executescript(f.read())
    db.commit()


@app.cli.command("init-db")
def init_db_command() -> None:
    init_db()
    print("Database initialized.")


@app.before_request
def ensure_db_exists() -> None:
    if not DATABASE.exists():
        init_db()


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


def calculate_member_summary(member_id: int) -> dict[str, float]:
    db = get_db()
    row = db.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN kind = 'income' THEN amount END), 0) AS total_income,
            COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount END), 0) AS total_expenses
        FROM transactions
        WHERE member_id = ?
        """,
        (member_id,),
    ).fetchone()

    month_start, month_end = current_month_bounds()
    monthly = db.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN kind = 'income' THEN amount END), 0) AS month_income,
            COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount END), 0) AS month_expenses
        FROM transactions
        WHERE member_id = ?
          AND transaction_date >= ?
          AND transaction_date < ?
        """,
        (member_id, month_start, month_end),
    ).fetchone()

    total_income = float(row["total_income"])
    total_expenses = float(row["total_expenses"])
    return {
        "total_income": total_income,
        "total_expenses": total_expenses,
        "balance": total_income - total_expenses,
        "month_income": float(monthly["month_income"]),
        "month_expenses": float(monthly["month_expenses"]),
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
        family_total += summary["balance"]
        member_cards.append({"member": member, "summary": summary})

    return render_template(
        "index.html",
        member_cards=member_cards,
        family_total=family_total,
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


@app.route("/member/<int:member_id>")
def member_detail(member_id: int):
    member = query_one(
        "SELECT id, full_name, relation, created_at FROM family_members WHERE id = ?",
        (member_id,),
    )
    if member is None:
        abort(404)

    summary = calculate_member_summary(member_id)
    transactions = query_all(
        """
        SELECT id, kind, category, amount, note, transaction_date, created_at
        FROM transactions
        WHERE member_id = ?
        ORDER BY transaction_date DESC, id DESC
        """,
        (member_id,),
    )

    return render_template(
        "member_detail.html",
        member=member,
        summary=summary,
        transactions=transactions,
        today=date.today().isoformat(),
    )


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

    db = get_db()
    db.execute(
        """
        INSERT INTO transactions (member_id, kind, category, amount, note, transaction_date)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (member_id, kind, category or None, amount, note or None, transaction_date),
    )
    db.commit()

    action = "Income added" if kind == "income" else "Expense added"
    flash(f"{action} for {member['full_name']}.", "success")
    return redirect(url_for("member_detail", member_id=member_id))


@app.post("/transactions/<int:transaction_id>/delete")
def delete_transaction(transaction_id: int):
    row = query_one("SELECT id, member_id FROM transactions WHERE id = ?", (transaction_id,))
    if row is None:
        abort(404)

    db = get_db()
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
    flash(f"Removed {row['full_name']} and related transactions.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
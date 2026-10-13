"""Mart queries (the SQL behind the BI measures), evaluated as-of a date against SCD2 history."""

def delinquency_by_product(conn, as_of: str) -> list[dict]:
    """Balance-weighted 30+/60+/90+ delinquency ratios per product."""
    rows = conn.execute("""
        SELECT product, SUM(balance_cents) AS total,
          SUM(CASE WHEN delinquency_bucket IN ('30','60','90+') THEN balance_cents END) AS d30,
          SUM(CASE WHEN delinquency_bucket IN ('60','90+') THEN balance_cents END) AS d60,
          SUM(CASE WHEN delinquency_bucket = '90+' THEN balance_cents END) AS d90
        FROM dim_loan_account WHERE valid_from <= ? AND ? < valid_to GROUP BY product ORDER BY product
    """, (as_of, as_of)).fetchall()
    return [{"product": r["product"], "total_cents": r["total"],
             "d30_plus": round((r["d30"] or 0) / r["total"], 4),
             "d60_plus": round((r["d60"] or 0) / r["total"], 4),
             "d90_plus": round((r["d90"] or 0) / r["total"], 4)} for r in rows]


def product_penetration(conn, as_of: str) -> dict:
    m = conn.execute("SELECT COUNT(*) n, SUM(share_accounts) s FROM dim_member WHERE valid_from<=? AND ?<valid_to",
                     (as_of, as_of)).fetchone()
    l = conn.execute("""SELECT COUNT(*) n, COUNT(DISTINCT member_id) borrowers FROM dim_loan_account
                        WHERE valid_from<=? AND ?<valid_to""", (as_of, as_of)).fetchone()
    return {"members": m["n"], "share_accounts": m["s"], "loan_accounts": l["n"],
            "share_to_loan_ratio": round(m["s"] / l["n"], 2), "pct_members_with_loan": round(l["borrowers"] / m["n"], 4)}


def deposit_velocity(conn) -> list[dict]:
    """Net deposits (credits to SH- accounts) per month and month-over-month change."""
    rows = conn.execute("""SELECT substr(txn_date,1,7) AS month, SUM(amount_cents) AS dep
        FROM fact_member_transactions WHERE entry_type='C' AND account_id LIKE 'SH-%' GROUP BY 1 ORDER BY 1""").fetchall()
    out, prev = [], None
    for r in rows:
        out.append({"month": r["month"], "deposits_cents": r["dep"],
                    "mom_change": None if not prev else round((r["dep"] - prev) / prev, 4)})
        prev = r["dep"]
    return out


def branch_region_portfolio(conn, region: str | None) -> list[dict]:
    """Row-level-security analogue: a branch manager passes their region; executives pass None."""
    sql = """SELECT m.region, COUNT(DISTINCT l.loan_id) loans, SUM(l.balance_cents) balance_cents
             FROM dim_loan_account l JOIN dim_member m ON m.member_id=l.member_id AND m.is_current=1
             WHERE l.is_current=1 {} GROUP BY m.region ORDER BY m.region"""
    if region:
        return [dict(r) for r in conn.execute(sql.format("AND m.region=?"), (region,))]
    return [dict(r) for r in conn.execute(sql.format(""))]

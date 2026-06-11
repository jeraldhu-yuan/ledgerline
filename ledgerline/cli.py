"""ledgerline CLI."""

import csv as csv_mod
import sys
from datetime import date
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ledgerline import LedgerlineError, db
from ledgerline.money import format_cents, parse_amount_to_cents

console = Console()


class _Group(click.Group):
    """Turn LedgerlineError into a clean message + nonzero exit."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except LedgerlineError as e:
            console.print(f"[red]error:[/red] {e}")
            sys.exit(1)


@click.group(cls=_Group)
@click.option("--db", "db_file", type=click.Path(path_type=Path), default=None,
              help="Database path (default: data/ledgerline.db or $LEDGERLINE_DB).")
@click.pass_context
def cli(ctx, db_file):
    """Local-first personal finance tracker."""
    ctx.obj = db_file


@cli.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--account", required=True, help='Local account label, e.g. "US Checking".')
@click.option("--profile", default=None, help="CSV profile name (auto-detected if omitted).")
@click.pass_obj
def ingest(db_file, path, account, profile):
    """Import a bank export (CSV/OFX/QFX), idempotently."""
    from ledgerline.categorize import categorize_rules_only
    from ledgerline.ingest import ingest_file

    conn = db.connect(db_file)
    result = ingest_file(conn, path, account, profile)
    console.print(
        f"[green]{result.new} new[/green] / {result.duplicates} duplicate"
        f" / [yellow]{result.failed} failed[/yellow] rows"
    )
    if result.failed:
        console.print("[yellow]failed rows quarantined — inspect the quarantine table[/yellow]")
    _, unknown = categorize_rules_only(conn)
    if unknown:
        console.print(
            f"{len(unknown)} merchants uncategorized — run [bold]ledgerline categorize[/bold] "
            "to resolve them with the LLM"
        )


@cli.command()
@click.option("--month", default=None, help="YYYY-MM (default: current month).")
@click.pass_obj
def summary(db_file, month):
    """Monthly income/outflow by category, top merchants, deltas vs prior month."""
    from ledgerline.query import month_summary

    month = month or date.today().strftime("%Y-%m")
    conn = db.connect(db_file)
    s = month_summary(conn, month)
    if not s["txn_count"]:
        console.print(f"no transactions in {month}")
        return
    console.print(
        f"[bold]{month}[/bold]  income {format_cents(s['income_cents'])}"
        f"  outflow {format_cents(s['outflow_cents'])}"
        f"  net {format_cents(s['income_cents'] + s['outflow_cents'])}"
    )
    t = Table(title="By category")
    t.add_column("category")
    t.add_column("total", justify="right")
    t.add_column("vs prior", justify="right")
    t.add_column("txns", justify="right")
    for r in s["by_category"]:
        t.add_row(r["category"], format_cents(r["total_cents"]),
                  format_cents(r["delta_cents"]), str(r["n"]))
    console.print(t)
    t = Table(title="Top merchants (outflow)")
    t.add_column("merchant")
    t.add_column("total", justify="right")
    t.add_column("txns", justify="right")
    for r in s["top_merchants"]:
        t.add_row(r["merchant_clean"] or "(unknown)",
                  format_cents(r["total_cents"]), str(r["n"]))
    console.print(t)


@cli.command()
@click.pass_obj
def categorize(db_file):
    """Resolve uncached merchants with one batched LLM call (needs ANTHROPIC_API_KEY)."""
    from ledgerline.categorize import categorize_llm, categorize_rules_only

    conn = db.connect(db_file)
    _, unknown = categorize_rules_only(conn)
    if not unknown:
        console.print("nothing to categorize — cache and rules cover everything")
        return
    n = categorize_llm(conn, unknown)
    console.print(f"categorized {n} transactions across {len(unknown)} merchants")


@cli.command()
@click.pass_obj
def review(db_file):
    """Confirm or correct cached categories (LLM-assigned first)."""
    from ledgerline.categorize import TAXONOMY, confirm, set_manual, unconfirmed

    conn = db.connect(db_file)
    rows = unconfirmed(conn)
    if not rows:
        console.print("nothing to review")
        return
    console.print(f"{len(rows)} merchants to review. "
                  "[Enter]=confirm, type a category to correct, s=skip, q=quit")
    for r in rows:
        answer = click.prompt(
            f"{r['merchant_clean']} -> {r['category']} ({r['source']}, {r['txn_count']} txns)",
            default="", show_default=False,
        ).strip().lower()
        if answer == "q":
            break
        if answer == "s":
            continue
        if answer == "":
            confirm(conn, r["merchant_clean"])
        elif answer in TAXONOMY:
            n = set_manual(conn, r["merchant_clean"], answer)
            console.print(f"  recategorized {n} transactions -> {answer}")
        else:
            console.print(f"  [red]not a category[/red] (taxonomy: {', '.join(TAXONOMY)})")


@cli.group()
def recurring():
    """Recurring payment groups."""


@recurring.command("detect")
@click.pass_obj
def recurring_detect(db_file):
    """Detect recurring groups (>=3 occurrences, stable amount + interval)."""
    from ledgerline.recurring import detect

    conn = db.connect(db_file)
    found = detect(conn)
    if not found:
        console.print("no recurring groups detected")
        return
    for g in found:
        day = f" (day {g['expected_day']})" if g["expected_day"] else ""
        console.print(
            f"[green]{g['label']}[/green]: {g['cadence']}{day},"
            f" ~{format_cents(g['expected_amount_cents'])}"
        )


@recurring.command("add")
@click.option("--label", required=True)
@click.option("--amount", required=True, help="Expected charge, e.g. 850.00 (outflow).")
@click.option("--cadence", type=click.Choice(["monthly", "weekly", "annual", "irregular"]),
              default="monthly")
@click.option("--day", type=int, default=None, help="Day of month (monthly cadence).")
@click.option("--merchant", default=None, help="merchant_clean to link existing transactions.")
@click.pass_obj
def recurring_add(db_file, label, amount, cadence, day, merchant):
    """Manually add a known installment so `upcoming` warns before 3 charges exist."""
    from ledgerline.recurring import add_manual_group

    cents = parse_amount_to_cents(amount)
    if cents > 0:
        cents = -cents  # expected charges are outflows
    conn = db.connect(db_file)
    add_manual_group(conn, label, cents, cadence, day, merchant)
    console.print(f"added recurring group [green]{label}[/green]")


@recurring.command("list")
@click.pass_obj
def recurring_list(db_file):
    conn = db.connect(db_file)
    t = Table(title="Recurring groups")
    for col in ("id", "label", "cadence", "expected", "day", "active"):
        t.add_column(col)
    for g in conn.execute("SELECT * FROM recurring_groups"):
        t.add_row(str(g["id"]), g["label"], g["cadence"] or "",
                  format_cents(g["expected_amount_cents"] or 0),
                  str(g["expected_day"] or ""), str(g["active"]))
    console.print(t)


@cli.command()
@click.option("--days", default=30, show_default=True)
@click.pass_obj
def upcoming(db_file, days):
    """Expected charges in the window, from active recurring groups."""
    from ledgerline.recurring import upcoming as upcoming_fn

    conn = db.connect(db_file)
    expected = upcoming_fn(conn, days=days)
    if not expected:
        console.print(f"nothing expected in the next {days} days")
        return
    t = Table(title=f"Expected in the next {days} days")
    t.add_column("date")
    t.add_column("label")
    t.add_column("amount", justify="right")
    for e in expected:
        t.add_row(e["date"], e["label"], format_cents(e["expected_amount_cents"] or 0))
    console.print(t)


@cli.command()
@click.argument("question")
@click.pass_obj
def ask(db_file, question):
    """Natural-language Q&A over the full history (read-only SQL tool loop)."""
    from ledgerline.query import ask as ask_fn

    answer = ask_fn(question, db_file=db_file)
    console.print(answer)


@cli.command()
@click.option("--month", required=True, help="YYYY-MM")
@click.option("--out", "out_file", type=click.Path(path_type=Path), default=None,
              help="Output CSV path (default: stdout).")
@click.pass_obj
def export(db_file, month, out_file):
    """CSV dump of a month for analysis elsewhere."""
    from ledgerline.query import export_month

    conn = db.connect(db_file)
    rows = export_month(conn, month)
    out = open(out_file, "w", newline="") if out_file else sys.stdout
    try:
        w = csv_mod.writer(out)
        w.writerow(["posted_date", "account", "amount_cents", "currency",
                    "merchant_raw", "merchant_clean", "category"])
        for r in rows:
            w.writerow(list(r))
    finally:
        if out_file:
            out.close()
            console.print(f"wrote {len(rows)} rows to {out_file}")


@cli.command()
@click.option("--since", default=None, help="YYYY-MM-DD (default: SimpleFIN's default window).")
@click.pass_obj
def sync(db_file, since):
    """Pull transactions via SimpleFIN Bridge through the same ingest pipeline."""
    from ledgerline.categorize import categorize_rules_only
    from ledgerline.connectors.simplefin import sync as sync_fn

    conn = db.connect(db_file)

    def resolver(sfid: str, name: str) -> str:
        return click.prompt(
            f'SimpleFIN account "{name}" is not mapped yet. Local account label',
            default=name,
        )

    results = sync_fn(conn, resolver, since=since)
    for label, r in results.items():
        console.print(
            f"{label}: [green]{r.new} new[/green] / {r.duplicates} duplicate"
        )
    _, unknown = categorize_rules_only(conn)
    if unknown:
        console.print(
            f"{len(unknown)} merchants uncategorized — run [bold]ledgerline categorize[/bold]"
        )


@cli.group()
def accounts():
    """Local account labels."""


@accounts.command("add")
@click.argument("name")
@click.option("--institution", default="unknown")
@click.option("--type", "account_type",
              type=click.Choice(["checking", "savings", "credit", "investment"]),
              default=None)
@click.option("--currency", default="USD")
@click.pass_obj
def accounts_add(db_file, name, institution, account_type, currency):
    from ledgerline.ingest import get_or_create_account

    conn = db.connect(db_file)
    get_or_create_account(conn, name, institution, account_type, currency)
    console.print(f"account [green]{name}[/green] ready")


@accounts.command("list")
@click.pass_obj
def accounts_list(db_file):
    conn = db.connect(db_file)
    t = Table(title="Accounts")
    for col in ("id", "name", "institution", "type", "currency", "txns"):
        t.add_column(col)
    rows = conn.execute(
        "SELECT a.*, COUNT(t.id) AS n FROM accounts a"
        " LEFT JOIN transactions t ON t.account_id = a.id GROUP BY a.id"
    )
    for r in rows:
        t.add_row(str(r["id"]), r["name"], r["institution"],
                  r["type"] or "", r["currency"], str(r["n"]))
    console.print(t)


if __name__ == "__main__":
    cli()

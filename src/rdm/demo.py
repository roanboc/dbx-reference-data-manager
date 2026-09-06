"""Demo content for local development: domains, functions and realistic reference lists.

Hierarchy: domain (business classifier) > function (schema) > form (table). The demo domains
are placeholders for the organisation's own list, which global admins maintain in the app.
"""

from __future__ import annotations

import io
import random
from datetime import date, timedelta

import pandas as pd

from rdm.auth.provider import PERSONAS
from rdm.backend.base import DatabaseBackend
from rdm.backend.duckdb_backend import CATALOG_LEVEL
from rdm.models import (
    ID_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    ColumnDef,
    DataType,
    DomainDef,
    FileDef,
    FormDef,
    FunctionDef,
    Role,
    RowDelete,
    RowInsert,
    RowUpdate,
    User,
)

SEED_USER = User(username="seed@example.org", display_name="Seed script", groups=("rdm_admins",))

#: The demo history is written as the *personas the reviewer can switch to*, not as invented
#: names: switching to Fiona in the header then shows her own edits in the History tab, which is
#: what makes "who changed what, and when" land.
ALICE = PERSONAS["admin"].user
FIONA = PERSONAS["function_admin"].user
EDDIE = PERSONAS["editor"].user

DEMO_DOMAINS: list[DomainDef] = [
    DomainDef(
        "customer",
        "Customer",
        "Customers, contracts, surveys and support services.",
        "customer.data@example.org",
    ),
    DomainDef(
        "finance", "Finance", "Financial planning, cost management and reporting.", "finance.data@example.org"
    ),
    DomainDef("people", "People", "Workforce, HR and payroll reference data.", "hr.systems@example.org"),
    DomainDef(
        "research",
        "Research",
        "Research and development (no functions yet).",
        "research.data@example.org",
    ),
]

DEMO_GRANTS: dict[str, dict[str, Role]] = {
    CATALOG_LEVEL: {"rdm_admins": Role.ADMIN},
    "customer__survey_service_improvement": {
        "customer_stewards": Role.EDITOR,
        "customer_readers": Role.VIEWER,
    },
    "finance__cost_management": {
        "finance_admins": Role.ADMIN,
        "finance_stewards": Role.EDITOR,
        "finance_readers": Role.VIEWER,
    },
    "hr__reference": {"hr_stewards": Role.EDITOR, "hr_readers": Role.VIEWER},
}


def demo_gl_transactions_csv(rows: int = 2000) -> bytes:
    """A deterministic CSV of general-ledger postings: a list too large to maintain in a grid."""
    rng = random.Random(42)
    accounts = ["4000", "4100", "5000", "5200", "7000"]
    centres = ["CC1001", "CC1002", "CC2001", "CC3001", "CC9001"]
    start = date(2024, 1, 1)
    frame = pd.DataFrame(
        {
            "posting_id": [f"P{i:06d}" for i in range(1, rows + 1)],
            "posted_on": [(start + timedelta(days=rng.randint(0, 365))).isoformat() for _ in range(rows)],
            "gl_account": [rng.choice(accounts) for _ in range(rows)],
            "cost_centre_code": [rng.choice(centres) for _ in range(rows)],
            "amount_gbp": [round(rng.uniform(-5000, 25000), 2) for _ in range(rows)],
            "narrative": [f"Posting {i}" for i in range(1, rows + 1)],
        }
    )
    return frame.to_csv(index=False).encode()


def demo_fx_rates_parquet() -> bytes:
    """Daily FX rates as Parquet (the format pipelines land)."""
    days = pd.date_range("2024-01-01", periods=366, freq="D")
    rng = random.Random(7)
    frame = pd.DataFrame(
        {
            "rate_date": days.date,
            "currency": ["USD"] * len(days),
            "rate_to_gbp": [round(0.78 + rng.uniform(-0.03, 0.03), 4) for _ in days],
        }
    )
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    return buffer.getvalue()


def seed(backend: DatabaseBackend) -> None:
    """Create demo domains, functions, forms, files, rows and grants. Safe to run on an empty database only."""
    admin = SEED_USER
    for domain in DEMO_DOMAINS:
        backend.create_domain(domain, admin)
    backend.create_function(
        FunctionDef(
            "customer__survey_service_improvement",
            display_name="Customer Survey & Service Improvement",
            description="Reference lists used by the customer survey and service improvement programme.",
            owner="survey.team@example.org",
            doc_link="https://wiki.example.org/customer-survey/reference-data",
            domain="customer",
        ),
        admin,
    )
    backend.create_function(
        FunctionDef(
            "finance__cost_management",
            display_name="Finance - Cost Management",
            description="Cost centres, GL mappings and budget reference data owned by Finance.",
            owner="finance.data@example.org",
            doc_link="https://wiki.example.org/finance/cost-management",
            domain="finance",
        ),
        admin,
    )
    backend.create_function(
        FunctionDef(
            "hr__reference",
            display_name="HR Reference",
            description="People and contract reference lists maintained by HR Systems.",
            owner="hr.systems@example.org",
            domain="people",
        ),
        admin,
    )

    for function, grants in DEMO_GRANTS.items():
        for principal, role in grants.items():
            backend.grant_function_role(function, principal, role, admin)

    backend.put_file(
        FileDef(
            "finance__cost_management",
            "gl_transactions.csv",
            display_name="GL Transactions (sample)",
            description="General-ledger postings extract; too many rows for a grid, kept as a file for reference.",
            owner="finance.data@example.org",
        ),
        demo_gl_transactions_csv(),
        admin,
    )
    backend.put_file(
        FileDef(
            "finance__cost_management",
            "fx_rates.parquet",
            display_name="FX Rates",
            description="Daily USD to GBP rates landed by the treasury pipeline.",
            owner="finance.data@example.org",
        ),
        demo_fx_rates_parquet(),
        admin,
    )

    backend.create_form(
        FormDef(
            "customer__survey_service_improvement",
            "survey_questions",
            display_name="Survey Questions",
            description="Master list of questions used across customer surveys, with weighting and lifecycle status.",
            owner="survey.team@example.org",
            columns=[
                ColumnDef(
                    "question_code", DataType.STRING, "Unique code, e.g. CSAT-Q01", nullable=False, is_key=True
                ),
                ColumnDef("question_text", DataType.STRING, "Question as shown to customers", nullable=False),
                ColumnDef(
                    "category",
                    DataType.STRING,
                    "Survey theme",
                    options=["Product", "Delivery", "Support", "Billing", "Overall"],
                ),
                ColumnDef(
                    "weight",
                    DataType.DECIMAL,
                    "Weight applied in the satisfaction index",
                    precision=10,
                    scale=2,
                ),
                ColumnDef("is_active", DataType.BOOLEAN, "Included in the current survey cycle"),
                ColumnDef("introduced_on", DataType.DATE, "First survey cycle using the question"),
            ],
        ),
        admin,
        pd.DataFrame(
            {
                "question_code": ["CSAT-Q01", "CSAT-Q02", "CSAT-Q08", "CSAT-Q15", "NPS-Q03", "NPS-Q07"],
                "question_text": [
                    "The product does what I expected it to do.",
                    "The product is good value for what I pay.",
                    "My order arrived when I was told it would.",
                    "I could find the information I needed without asking.",
                    "My last invoice was clear and correct.",
                    "I know how to get help when something goes wrong.",
                ],
                "category": ["Product", "Product", "Delivery", "Support", "Billing", "Support"],
                "weight": [1.0, 1.0, 1.25, 0.75, 0.5, 1.5],
                "is_active": [True, True, True, False, True, True],
                "introduced_on": [
                    date(2019, 9, 1),
                    date(2019, 9, 1),
                    date(2020, 9, 1),
                    date(2021, 9, 1),
                    date(2023, 1, 15),
                    date(2024, 1, 15),
                ],
            }
        ),
    )
    backend.create_form(
        FormDef(
            "customer__survey_service_improvement",
            "service_areas",
            display_name="Service Areas",
            description="Service areas that own survey actions and improvement plans.",
            owner="survey.team@example.org",
            columns=[
                ColumnDef("area_code", DataType.STRING, "Short code", nullable=False, is_key=True),
                ColumnDef("area_name", DataType.STRING, "Service area name", nullable=False),
                ColumnDef("lead_email", DataType.STRING, "Accountable lead"),
                ColumnDef("target_score", DataType.INTEGER, "Target satisfaction score (%)"),
                ColumnDef("is_active", DataType.BOOLEAN, "Currently in scope"),
            ],
        ),
        admin,
        pd.DataFrame(
            {
                "area_code": ["SUP", "LOG", "BIL", "ONB", "FLD"],
                "area_name": [
                    "Customer Support",
                    "Logistics",
                    "Billing",
                    "Onboarding",
                    "Field Services",
                ],
                "lead_email": [
                    "sup.lead@example.org",
                    "log.lead@example.org",
                    "bil.lead@example.org",
                    None,
                    "fld.lead@example.org",
                ],
                "target_score": [85, 80, 82, 78, 75],
                "is_active": [True, True, True, True, False],
            }
        ),
    )
    backend.create_form(
        FormDef(
            "finance__cost_management",
            "cost_centres",
            display_name="Cost Centres",
            description="Cost centre hierarchy with budget holders. Close a cost centre by setting valid_to.",
            owner="finance.data@example.org",
            columns=[
                ColumnDef(
                    "cost_centre_code", DataType.STRING, "Finance system code", nullable=False, is_key=True
                ),
                ColumnDef("cost_centre_name", DataType.STRING, "Descriptive name", nullable=False),
                ColumnDef(
                    "business_unit",
                    DataType.STRING,
                    "Owning business unit",
                    options=["Commercial", "Operations", "Technology", "Corporate Services"],
                ),
                ColumnDef("budget_holder", DataType.STRING, "Budget holder email"),
                ColumnDef(
                    "annual_budget_gbp", DataType.DECIMAL, "Approved annual budget", precision=18, scale=2
                ),
                ColumnDef("valid_from", DataType.DATE, "Start of validity", nullable=False),
                ColumnDef("valid_to", DataType.DATE, "End of validity (empty = open)"),
            ],
        ),
        admin,
        pd.DataFrame(
            {
                "cost_centre_code": ["CC1001", "CC1002", "CC2001", "CC3001", "CC9001"],
                "cost_centre_name": [
                    "Direct Sales",
                    "Marketing",
                    "Manufacturing",
                    "Field Services",
                    "Head Office",
                ],
                "business_unit": [
                    "Commercial",
                    "Commercial",
                    "Operations",
                    "Operations",
                    "Corporate Services",
                ],
                "budget_holder": [
                    "s.head@example.org",
                    "m.head@example.org",
                    "o.head@example.org",
                    "f.head@example.org",
                    "ho@example.org",
                ],
                "annual_budget_gbp": [1250000.00, 980000.50, 3400000.00, 2750000.00, 610000.00],
                "valid_from": [date(2022, 8, 1)] * 5,
                "valid_to": [None, None, None, date(2025, 7, 31), None],
            }
        ),
    )
    backend.create_form(
        FormDef(
            "finance__cost_management",
            "gl_account_mappings",
            display_name="GL Account Mappings",
            description="Maps general ledger accounts to reporting lines.",
            owner="finance.data@example.org",
            columns=[
                ColumnDef("gl_account", DataType.STRING, "GL account number", nullable=False, is_key=True),
                ColumnDef("reporting_line", DataType.STRING, "Management reporting line", nullable=False),
                ColumnDef(
                    "account_type",
                    DataType.STRING,
                    "Account class",
                    options=["Income", "Pay", "Non-pay", "Capital"],
                ),
                ColumnDef("notes", DataType.STRING, "Free text"),
            ],
        ),
        admin,
        pd.DataFrame(
            {
                "gl_account": ["4000", "4100", "5000", "5200", "7000"],
                "reporting_line": [
                    "Product income",
                    "Service income",
                    "Operations pay",
                    "Corporate services pay",
                    "Equipment",
                ],
                "account_type": ["Income", "Income", "Pay", "Pay", "Capital"],
                "notes": [None, "Includes support contracts", None, None, "Over £10k"],
            }
        ),
    )
    backend.create_form(
        FormDef(
            "hr__reference",
            "employment_types",
            display_name="Employment Types",
            description="Employment type codes used by the HR system and payroll.",
            owner="hr.systems@example.org",
            columns=[
                ColumnDef("code", DataType.STRING, "HR system code", nullable=False, is_key=True),
                ColumnDef("name", DataType.STRING, "Employment type", nullable=False),
                ColumnDef("description", DataType.STRING, "Guidance for HR advisers"),
                ColumnDef("default_fte", DataType.DOUBLE, "Default FTE"),
                ColumnDef("pensionable", DataType.BOOLEAN, "Eligible for the pension scheme"),
            ],
        ),
        admin,
        pd.DataFrame(
            {
                "code": ["FT", "PT", "FTC", "CAS", "AGY"],
                "name": ["Full time", "Part time", "Fixed term", "Casual", "Agency"],
                "description": [
                    "Permanent, 1.0 FTE",
                    "Permanent, below 1.0 FTE",
                    "Contract with an end date",
                    "Hourly paid, no guaranteed hours",
                    "Supplied by an agency; not an employee",
                ],
                "default_fte": [1.0, 0.5, 1.0, 0.0, 1.0],
                "pensionable": [True, True, True, False, False],
            }
        ),
    )

    _seed_stewardship(backend)


def _row_id(backend: DatabaseBackend, form: FormDef, key_column: str, key: str) -> tuple[str, int]:
    """``(_id, _version)`` of the row with that business key, as a steward's browser would hold it."""
    rows = backend.read_rows(form)
    match = rows[rows[key_column] == key]
    if match.empty:
        raise ValueError(f"Demo data and demo history disagree: no {key_column} '{key}' in {form.full_name}.")
    return str(match.iloc[0][ID_COLUMN]), int(match.iloc[0][VERSION_COLUMN])


def _seed_stewardship(backend: DatabaseBackend) -> None:
    """Replay a plausible few weeks of stewardship on top of the initial load.

    Without this the demo database is 26 inserts by one user in one second: the History tab,
    the per-row history in the item form and the Type 2 history table - three of the things the
    app exists for - all have nothing to show. Every change here goes through the same
    ``apply_changes`` path a steward's Save uses, so the audit trail is genuine rather than
    written by hand.
    """
    cost_centres = backend.get_form("finance__cost_management", "cost_centres")
    # Switch on the Type 2 history table (FR-47) *before* the edits, so the demo has a form
    # whose validity windows were actually opened and closed by the changes below.
    cost_centres = backend.set_scd2(cost_centres, True, SEED_USER)

    rid, version = _row_id(backend, cost_centres, "cost_centre_code", "CC1002")
    backend.apply_changes(
        cost_centres,
        ChangeSet(updates=[RowUpdate(rid, {"budget_holder": "a.mensah@example.org"}, version)]),
        FIONA,
    )
    rid, version = _row_id(backend, cost_centres, "cost_centre_code", "CC2001")
    backend.apply_changes(
        cost_centres,
        ChangeSet(updates=[RowUpdate(rid, {"annual_budget_gbp": 3_650_000.00}, version)]),
        ALICE,
    )
    backend.apply_changes(
        cost_centres,
        ChangeSet(
            inserts=[
                RowInsert(
                    {
                        "cost_centre_code": "CC4001",
                        "cost_centre_name": "Customer Success",
                        "business_unit": "Commercial",
                        "budget_holder": "c.success@example.org",
                        "annual_budget_gbp": 420_000.00,
                        "valid_from": date(2025, 4, 1),
                    }
                )
            ]
        ),
        FIONA,
    )
    # CC3001 was closed with an end date in the initial load; reopening it gives the item form
    # a row with two versions to show, and the history table a closed window beside an open one.
    rid, version = _row_id(backend, cost_centres, "cost_centre_code", "CC3001")
    backend.apply_changes(
        cost_centres, ChangeSet(updates=[RowUpdate(rid, {"valid_to": None}, version)]), FIONA
    )
    rid, version = _row_id(backend, cost_centres, "cost_centre_code", "CC3001")
    backend.apply_changes(
        cost_centres,
        ChangeSet(updates=[RowUpdate(rid, {"budget_holder": "r.kaur@example.org"}, version)]),
        ALICE,
    )
    # A deletion, so the History tab has an entry that can be restored from (FR-24).
    rid, version = _row_id(backend, cost_centres, "cost_centre_code", "CC9001")
    backend.apply_changes(cost_centres, ChangeSet(deletes=[RowDelete(rid, version)]), ALICE)

    # Eddie edits the Customer function, so his persona lands on a list he has changed himself.
    questions = backend.get_form("customer__survey_service_improvement", "survey_questions")
    rid, version = _row_id(backend, questions, "question_code", "CSAT-Q08")
    backend.apply_changes(
        questions, ChangeSet(updates=[RowUpdate(rid, {"is_active": False}, version)]), EDDIE
    )
    rid, version = _row_id(backend, questions, "question_code", "CSAT-Q01")
    backend.apply_changes(questions, ChangeSet(updates=[RowUpdate(rid, {"weight": 1.5}, version)]), EDDIE)

    employment_types = backend.get_form("hr__reference", "employment_types")
    rid, version = _row_id(backend, employment_types, "code", "CAS")
    backend.apply_changes(
        employment_types,
        ChangeSet(updates=[RowUpdate(rid, {"description": "Hourly paid, no guaranteed hours (2025 policy)"}, version)]),
        ALICE,
    )

    for form in (cost_centres, questions, employment_types):
        _assert_every_update_changed_something(backend, form)


def _assert_every_update_changed_something(backend: DatabaseBackend, form: FormDef) -> None:
    """Guard the demo story against silently becoming a wall of no-ops.

    An update that writes the value a row already had is recorded, but shows no changed field
    and opens a Type 2 window identical to the one it closed - so the History tab looks broken
    in exactly the demo it exists for. Setting a value the seed data already contains is easy
    to do by accident, so it fails the seed instead.
    """
    history = backend.get_history(form)
    empty = history[(history["change_type"] == "update") & (history["changed_fields"] == "")]
    if not empty.empty:
        raise ValueError(
            f"Demo history for {form.full_name} has {len(empty)} update(s) that changed nothing: "
            f"pick a value the seeded rows do not already have."
        )

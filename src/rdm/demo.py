"""Demo content for local development: domains, functions, realistic reference lists and files.

Hierarchy: domain (business classifier) > function (schema) > form (table). The demo domains
are placeholders for the organisation's own list, which global admins maintain in the app.
"""

from __future__ import annotations

import io
import random
from datetime import date, timedelta

import pandas as pd

from rdm.backend.base import DatabaseBackend
from rdm.backend.duckdb_backend import CATALOG_LEVEL
from rdm.models import ColumnDef, DataType, DomainDef, FileDef, FormDef, FunctionDef, Role, User

SEED_USER = User(username="seed@example.org", display_name="Seed script", groups=("rdm_admins",))

DEMO_DOMAINS: list[DomainDef] = [
    DomainDef(
        "student",
        "Student",
        "Students, enrolments, surveys and student services.",
        "student.data@example.org",
    ),
    DomainDef(
        "finance", "Finance", "Financial planning, cost management and reporting.", "finance.data@example.org"
    ),
    DomainDef("people", "People", "Workforce, HR and payroll reference data.", "hr.systems@example.org"),
    DomainDef(
        "research",
        "Research",
        "Research management and grants (no functions yet).",
        "research.data@example.org",
    ),
]

DEMO_GRANTS: dict[str, dict[str, Role]] = {
    CATALOG_LEVEL: {"rdm_admins": Role.ADMIN},
    "student__survey_service_improvement": {"student_stewards": Role.EDITOR, "student_readers": Role.VIEWER},
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
            "student__survey_service_improvement",
            display_name="Student Survey & Service Improvement",
            description="Reference lists used by the student survey and service improvement programme.",
            owner="survey.team@example.org",
            doc_link="https://wiki.example.org/student-survey/reference-data",
            domain="student",
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
            "student__survey_service_improvement",
            "survey_questions",
            display_name="Survey Questions",
            description="Master list of questions used across student surveys, with weighting and lifecycle status.",
            owner="survey.team@example.org",
            columns=[
                ColumnDef(
                    "question_code", DataType.STRING, "Unique code, e.g. NSS-Q01", nullable=False, is_key=True
                ),
                ColumnDef("question_text", DataType.STRING, "Question as shown to students", nullable=False),
                ColumnDef(
                    "category",
                    DataType.STRING,
                    "Survey theme",
                    options=["Teaching", "Assessment", "Support", "Facilities", "Overall"],
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
                "question_code": ["NSS-Q01", "NSS-Q02", "NSS-Q08", "NSS-Q15", "INT-Q03", "INT-Q07"],
                "question_text": [
                    "Staff are good at explaining things.",
                    "Staff have made the subject interesting.",
                    "The criteria used in marking have been clear in advance.",
                    "I have been able to access course-specific resources when I needed to.",
                    "The library spaces meet my study needs.",
                    "I know where to get support for my wellbeing.",
                ],
                "category": ["Teaching", "Teaching", "Assessment", "Facilities", "Facilities", "Support"],
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
            "student__survey_service_improvement",
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
                "area_code": ["LIB", "ITS", "WEL", "CAR", "EST"],
                "area_name": ["Library", "IT Services", "Wellbeing", "Careers", "Estates"],
                "lead_email": [
                    "lib.lead@example.org",
                    "it.lead@example.org",
                    "wel.lead@example.org",
                    None,
                    "est.lead@example.org",
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
                    "faculty",
                    DataType.STRING,
                    "Owning faculty or directorate",
                    options=["Arts", "Science", "Engineering", "Professional Services"],
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
                    "English Literature",
                    "History",
                    "Physics",
                    "Civil Engineering",
                    "Registry",
                ],
                "faculty": ["Arts", "Arts", "Science", "Engineering", "Professional Services"],
                "budget_holder": [
                    "a.head@example.org",
                    "h.head@example.org",
                    "p.head@example.org",
                    "c.head@example.org",
                    "reg@example.org",
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
                    "Tuition income",
                    "Research income",
                    "Academic pay",
                    "Professional services pay",
                    "Equipment",
                ],
                "account_type": ["Income", "Income", "Pay", "Pay", "Capital"],
                "notes": [None, "Includes grants", None, None, "Over £10k"],
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

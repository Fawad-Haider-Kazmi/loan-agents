"""Demo data: eight scenarios that between them exercise every branch of the
workflow, plus a random-case generator for the feedback-learning demo."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional

from .models import CreditReport, Document, DocumentType, LoanApplication
from .services.credit_bureau import MockCreditBureau
from .repository import Repository


@dataclass
class Scenario:
    application: LoanApplication
    credit: Optional[CreditReport]      # None -> bureau has no file (thin file)
    expectation: str
    blacklisted: bool = False
    bureau_failures: int = 0            # transient failures injected before success


def make_documents(*, name: str, national_id: str, employer: str, salary: float, account: str,
                   avg_credit: Optional[float] = None, include_bank: bool = True,
                   garble_salary: bool = False, id_name: Optional[str] = None) -> list[Document]:
    docs = [
        Document(DocumentType.NATIONAL_ID, "cnic.jpg",
                 f"Name: {id_name or name}\nID Number: {national_id}\n"
                 f"Date of Birth: 1990-05-14\nExpiry: 2031-03-01"),
        Document(DocumentType.SALARY_SLIP, "salary_slip.pdf",
                 "### scan error ###\n~~ illegible ~~" if garble_salary else
                 f"Employer: {employer}\nEmployee: {name}\nMonthly Salary: {salary:,.0f}\nMonth: 2026-09"),
    ]
    if include_bank:
        docs.append(Document(DocumentType.BANK_STATEMENT, "statement.pdf",
                             f"Account Holder: {name}\nAccount Number: {account}\n"
                             f"Average Monthly Balance: {salary * 2:,.0f}\n"
                             f"Average Monthly Credit: {avg_credit if avg_credit is not None else salary:,.0f}"))
    return docs


def _app(app_id, name, nid, amount, tenure, stated_income, employer, account, **doc_kw) -> LoanApplication:
    salary = doc_kw.pop("salary", stated_income)
    return LoanApplication(app_id, name, nid, amount, tenure, stated_income, employer, account,
                           make_documents(name=name, national_id=nid, employer=employer,
                                          salary=salary, account=account, **doc_kw))


def sample_scenarios() -> list[Scenario]:
    return [
        Scenario(_app("APP-1001", "Ayesha Khan", "42101-1000001-1", 300_000, 24, 250_000, "Indus Textiles", "PK01-1001"),
                 CreditReport("42101-1000001-1", 760, 1, 10_000, 0, 0.20),
                 "Low risk, small amount -> straight-through approval, no human needed"),
        Scenario(_app("APP-1002", "Bilal Ahmed", "42101-1000002-1", 1_500_000, 48, 150_000, "Karachi Logistics", "PK01-1002"),
                 CreditReport("42101-1000002-1", 680, 2, 20_000, 1, 0.45),
                 "Policy-eligible but large amount -> human approval required"),
        Scenario(_app("APP-1003", "Kamran Shah", "42101-1000003-1", 1_200_000, 36, 90_000, "Metro Traders", "PK01-1003"),
                 CreditReport("42101-1000003-1", 540, 3, 30_000, 3, 0.80),
                 "Debt-to-income far above limit -> automatic rejection"),
        Scenario(_app("APP-1004", "Sana Malik", "42101-1000004-1", 400_000, 24, 140_000, "Harbour Pharma", "PK01-1004",
                      include_bank=False),
                 CreditReport("42101-1000004-1", 720, 0, 0, 0, 0.1),
                 "Bank statement missing -> NEEDS_INFO, applicant notified"),
        Scenario(_app("APP-1005", "Imran Qureshi", "42101-1000005-1", 350_000, 24, 160_000, "Orient Steel", "PK01-1005"),
                 CreditReport("42101-1000005-1", 700, 1, 12_000, 0, 0.3),
                 "ID is on the blacklist -> automatic rejection (applicant not told why)", blacklisted=True),
        Scenario(_app("APP-1006", "Farah Iqbal", "42101-1000006-1", 800_000, 36, 300_000, "Sky Media", "PK01-1006",
                      salary=120_000),
                 CreditReport("42101-1000006-1", 700, 1, 15_000, 0, 0.30),
                 "Stated income 2.5x the salary slip -> fraud + validation flags -> escalated to a human"),
        Scenario(_app("APP-1007", "Usman Tariq", "42101-1000007-1", 200_000, 12, 180_000, "Royal Foods", "PK01-1007"),
                 CreditReport("42101-1000007-1", 730, 0, 5_000, 0, 0.25),
                 "Credit bureau times out twice, retry succeeds -> straight-through approval",
                 bureau_failures=2),
        Scenario(_app("APP-1008", "Nadia Sheikh", "42101-1000008-1", 250_000, 18, 130_000, "City Clinic", "PK01-1008"),
                 None, "No bureau history (thin file) -> escalated to a human"),
    ]


def seed_services(scenarios: list[Scenario], bureau: MockCreditBureau, repo: Repository) -> None:
    for s in scenarios:
        nid = s.application.national_id
        if s.credit:
            bureau.register(nid, s.credit)
        if s.bureau_failures:
            bureau._fail_first[_digits(nid)] = s.bureau_failures
        if s.blacklisted:
            repo.add_to_blacklist(nid, "confirmed prior fraud")


def _digits(v: str) -> str:
    return "".join(c for c in v if c.isdigit())


# ------------------------------------------------------- random cases for feedback demo
FIRST = ["Ali", "Sara", "Omar", "Mina", "Zain", "Hira", "Danish", "Iqra", "Faisal", "Maha"]
LAST = ["Raza", "Siddiqui", "Hussain", "Baig", "Farooq", "Noor", "Mirza", "Javed", "Ansari", "Abbasi"]


def random_case(rng: random.Random, i: int) -> tuple[LoanApplication, CreditReport]:
    salary = round(rng.uniform(60_000, 300_000), -3)
    amount = max(50_000.0, round(salary * 12 * rng.uniform(0.1, 1.2), -4))
    tenure = rng.choice([12, 24, 36, 48])
    nid = f"42101-{2000000 + i:07d}-1"
    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    account = f"PK02-{i:05d}"
    score = int(min(850, max(300, rng.gauss(650, 90))))
    credit = CreditReport(nid, score, rng.randint(0, 4), round(salary * rng.uniform(0, 0.3), -2),
                          min(6, int(rng.expovariate(1.2))), min(1.0, max(0.0, rng.gauss(0.4, 0.22))))
    app = LoanApplication(f"SIM-{i:04d}", name, nid, amount, tenure, salary, "Sim Corp", account,
                          make_documents(name=name, national_id=nid, employer="Sim Corp",
                                         salary=salary, account=account))
    return app, credit

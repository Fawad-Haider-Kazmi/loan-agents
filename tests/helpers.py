import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loan_system.config import SystemConfig                       # noqa: E402
from loan_system.notifier import InMemoryNotifier                 # noqa: E402
from loan_system.samples import sample_scenarios, seed_services   # noqa: E402
from loan_system.services.banking import MockCoreBanking          # noqa: E402
from loan_system.services.credit_bureau import MockCreditBureau   # noqa: E402
from loan_system.system import build_system                       # noqa: E402

_cache = {}


def make_system(bank_fail_first=0, db_dir=None, seed=True, **cfg_overrides):
    d = db_dir or tempfile.mkdtemp()
    cfg = SystemConfig(db_path=f"{d}/t.db", model_path=os.path.join(tempfile.gettempdir(), "test_model.json"),
                       retry_base_delay=0, **cfg_overrides)
    bureau, bank, notifier = MockCreditBureau(), MockCoreBanking(fail_first=bank_fail_first), InMemoryNotifier()
    system = build_system(cfg, bureau=bureau, bank=bank, notifier=notifier, sleep=lambda s: None)
    scenarios = {s.application.application_id: s for s in sample_scenarios()}
    if seed:
        seed_services(list(scenarios.values()), bureau, system.repo)
    return system, scenarios, d


def fresh_app(scenarios, app_id):
    """Scenarios are shared module state; return a deep copy so tests stay isolated."""
    import copy
    return copy.deepcopy(scenarios[app_id].application)

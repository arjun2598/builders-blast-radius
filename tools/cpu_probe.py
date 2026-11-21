"""pytest plugin: measures CPU seconds (this process; ProcessPoolExecutor is
replaced by the in-process executor the selector uses) per test phase and
per test-module import.

PYTHONPATH=tools:../../src CPU_OUT=cpu.json python -m pytest -p cpu_probe tests
Then tools/cpu_table.py turns it into data/cpu.json.
"""

import json
import os
import time

import pytest

from blast import duckcache
from blast.runner import GC_THRESHOLD, SyncExecutor

import concurrent.futures  # noqa: E402

concurrent.futures.ProcessPoolExecutor = SyncExecutor

_cpu: dict = {}
_mods: dict = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    t = time.process_time()
    yield
    if isinstance(collector, pytest.Module):
        _mods[collector.nodeid] = time.process_time() - t


def pytest_collection_finish(session):
    # same collector settings as the runner's children
    import gc

    gc.collect()
    gc.freeze()
    gc.set_threshold(*GC_THRESHOLD)


def _wrap(phase):
    @pytest.hookimpl(hookwrapper=True)
    def hook(item, *a):
        t = time.process_time()
        yield
        _cpu.setdefault(item.nodeid, {})[phase] = time.process_time() - t

    return hook


pytest_runtest_setup = _wrap("s")
pytest_runtest_call = _wrap("c")
pytest_runtest_teardown = _wrap("t")


def pytest_unconfigure(config):
    with open(os.environ["CPU_OUT"], "w") as fh:
        json.dump({"tests": _cpu, "modules": _mods, "startup": time.process_time()}, fh)

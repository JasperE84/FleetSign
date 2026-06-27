import importlib

def test_package_imports():
    assert importlib.import_module("fleetsign") is not None

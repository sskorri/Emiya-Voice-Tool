"""Native-window runs (BUILD_SPEC §8.8 and the reliability bench): pytest -m native, or python tests\\native\\smoke.py.

Every window opens in test-window mode (--smoke): on a free screen, without taking the focus, driven from
inside the app by smoke_plan.py and measured from outside by winprobe.py. Nothing here runs in the default
suite: the modules import only the standard library and ctypes, and every test is marked native.
"""

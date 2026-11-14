"""Every Python run of the test suite is offline (§8.1): set before any test module imports huggingface_hub."""
import os

os.environ["HF_HUB_OFFLINE"] = "1"

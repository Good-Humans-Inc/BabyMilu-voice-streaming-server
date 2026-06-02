import os
import sys
import importlib
from config.logger import setup_logging

logger = setup_logging()


def create_instance(class_name, *args, **kwargs):
    provider_path = os.path.join(
        "core", "providers", "memory", class_name, f"{class_name}.py"
    )
    if not os.path.exists(provider_path):
        logger.bind(tag=__name__).warning(
            f"记忆提供者未找到: {class_name}，将回退到 'nomem'（无记忆）提供者。"
        )
        class_name = "nomem"

    lib_name = f"core.providers.memory.{class_name}.{class_name}"
    if lib_name not in sys.modules:
        sys.modules[lib_name] = importlib.import_module(f"{lib_name}")
    return sys.modules[lib_name].MemoryProvider(*args, **kwargs)

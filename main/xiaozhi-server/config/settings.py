import os
from config.config_loader import read_config, get_project_dir, load_config


default_config_file = "config.yaml"
config_file_valid = False


def check_config_file():
    global config_file_valid
    if config_file_valid:
        return
    """
    简化的配置检查，仅提示用户配置文件的使用情况
    """
    custom_config_file = get_project_dir() + "data/." + default_config_file
    if not os.path.exists(custom_config_file):
        raise FileNotFoundError(
            "找不到data/.config.yaml文件，请按教程确认该配置文件是否存在"
        )

    # 检查是否从API读取配置
    config = load_config()
    if config.get("read_config_from_api", False):
        print("从API读取配置")
        old_config_origin = read_config(custom_config_file)
        if old_config_origin.get("selected_module") is not None:
            error_msg = "您的配置文件好像既包含智控台的配置又包含本地配置：\n"
            error_msg += "\n建议您：\n"
            error_msg += "1、将根目录的config_from_api.yaml文件复制到data下，重命名为.config.yaml\n"
            error_msg += "2、按教程配置好接口地址和密钥\n"
            raise ValueError(error_msg)
    config_file_valid = True


def get_gcp_credentials_path() -> str:
    """Return the path to GCP credentials if set via env/config.

    Precedence:
      1) GOOGLE_APPLICATION_CREDENTIALS env var
      2) data/.gcp/sa.json (if present and mounted)
    """
    # 1) Environment variable (preferred)
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and os.path.exists(path):
        return path

    # 2) Default convention under data folder (optional)
    default_path = os.path.join(get_project_dir(), "data/.gcp/sa.json")
    if os.path.exists(default_path):
        return default_path

    return ""

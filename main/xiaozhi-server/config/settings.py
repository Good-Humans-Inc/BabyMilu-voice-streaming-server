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
      1) GOOGLE_APPLICATION_CREDENTIALS env var (if it's a file)
      2) If env var points to a directory, look for sa.json inside it
      3) /opt/secrets/gcp/ directory (Docker secret mount, look for JSON file)
      4) data/.gcp/sa.json (if present and mounted)
      5) data/.gcp/ directory (if present, look for any JSON file inside)
    """
    # 1) Environment variable (preferred)
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        if os.path.isfile(path):
            return path
        elif os.path.isdir(path):
            # Try to find sa.json inside the directory
            sa_file = os.path.join(path, "sa.json")
            if os.path.isfile(sa_file):
                return sa_file
            # Look for any JSON file in the directory
            try:
                json_files = [f for f in os.listdir(path) if f.endswith('.json')]
                if json_files:
                    found_file = os.path.join(path, json_files[0])
                    if os.path.isfile(found_file):
                        return found_file
            except Exception:
                pass

    # 2) Check Docker secret mount directory first (before data/.gcp)
    docker_secret_dir = "/opt/secrets/gcp"
    if os.path.isdir(docker_secret_dir):
        try:
            json_files = [f for f in os.listdir(docker_secret_dir) if f.endswith('.json')]
            if json_files:
                found_file = os.path.join(docker_secret_dir, json_files[0])
                if os.path.isfile(found_file):
                    return found_file
        except Exception:
            pass

    # 3) Default convention under data folder (optional)
    default_path = os.path.join(get_project_dir(), "data/.gcp/sa.json")
    if os.path.isfile(default_path):
        return default_path
    
    # 4) If data/.gcp is a directory, look for JSON files inside
    default_dir = os.path.join(get_project_dir(), "data/.gcp")
    if os.path.isdir(default_dir):
        try:
            json_files = [f for f in os.listdir(default_dir) if f.endswith('.json')]
            if json_files:
                found_file = os.path.join(default_dir, json_files[0])
                if os.path.isfile(found_file):
                    return found_file
        except Exception:
            pass

    return ""

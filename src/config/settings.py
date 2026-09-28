import yaml
import os
from pathlib import Path
from dataclasses import dataclass, fields
from functools import lru_cache


def load_yaml_config(config_path: Path) -> dict:
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def parse_dataclass_from_dict(cls, data: dict):
    field_values = {}
    for field in fields(cls):
        field_values[field.name] = data.get(field.name, field.default)
    return cls(**field_values)


@dataclass
class PaddleOCRConfig:
    text_detection_model_dir: str = 'DEFAULT_DET_PATH'
    text_recognition_model_dir: str = 'DEFAULT_REC_PATH'
    # 模型名必须与上面的 dir 指向同一个模型, 否则 PaddleOCR 会按 name 去找/下载对应模型
    text_detection_model_name: str = 'PP-OCRv5_server_det'
    text_recognition_model_name: str = 'PP-OCRv5_server_rec'
    use_doc_orientation_classify: bool = True
    use_doc_unwarping: bool = True
    use_textline_orientation: bool = True
    lang: str = 'en'
    device: str = 'cpu'
    # CPU 下 PaddleX 默认走 oneDNN(mkldnn) 后端；paddlepaddle 3.3.x 的 PIR→oneDNN
    # 指令转换存在回归（ConvertPirAttribute2RuntimeAttribute not support），
    # 会直接 500。CPU 部署请置 false 走纯 paddle 内核；GPU 部署该开关无影响。
    enable_mkldnn: bool = True


@dataclass
class ServiceConfig:
    host: str = '127.0.0.1'
    port: int = 9000
    # keep-alive 空闲超时（秒）：uvicorn 默认 5s。调用方的 OCR 调用很密集，间隔一超过它就得
    # 重新建连；调大到 75s 才能复用同一条连接（见 README §1.5.13）。
    timeout_keep_alive: int = 75
    # 收到 SIGTERM 后等当前请求收尾的宽限时间（秒）。
    timeout_graceful_shutdown: int = 10


class Settings:
    _instance = None

    def __init__(self):
        config_path = Path(os.environ.get('CONFIG_FILE', Path(__file__).parent / 'config.yaml'))
        self._config = load_yaml_config(config_path)
        self.paddleocr = parse_dataclass_from_dict(PaddleOCRConfig, self._config.get('paddleocr', {}))
        self.server = parse_dataclass_from_dict(ServiceConfig, self._config.get('server', {}))

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


@lru_cache()
def get_settings() -> Settings:
    return Settings.get_instance()


if __name__ == '__main__':
    settings = get_settings()
    print("=== PaddleOCR Config ===")
    print(f"text_detection_model_dir: {settings.paddleocr.text_detection_model_dir}")
    print(f"text_recognition_model_dir: {settings.paddleocr.text_recognition_model_dir}")
    print(f"use_doc_orientation_classify: {settings.paddleocr.use_doc_orientation_classify}")
    print(f"use_doc_unwarping: {settings.paddleocr.use_doc_unwarping}")
    print(f"use_textline_orientation: {settings.paddleocr.use_textline_orientation}")
    print(f"lang: {settings.paddleocr.lang}")
    print(f"device: {settings.paddleocr.device}")
    print(f"enable_mkldnn: {settings.paddleocr.enable_mkldnn}")
    print("\n=== Service Config ===")
    print(f"host: {settings.server.host}")
    print(f"port: {settings.server.port}")
# python /app/src/config/settings.py
import yaml
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
    use_doc_orientation_classify: bool = True
    use_doc_unwarping: bool = True
    use_textline_orientation: bool = True
    lang: str = 'en'
    device: str = 'cpu'


@dataclass
class ServiceConfig:
    host: str = '127.0.0.1'
    port: int = 9000


class Settings:
    _instance = None

    def __init__(self):
        config_path = Path(__file__).parent / 'config.yaml'
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
    print("\n=== Service Config ===")
    print(f"host: {settings.server.host}")
    print(f"port: {settings.server.port}")
# python /app/src/config/settings.py
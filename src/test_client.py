import base64
import requests


def image_to_base64(image_path: str) -> str:
    with open(image_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


def test_ocr_server(host: str, port: int, image_path: str):
    url = f"http://{host}:{port}/ocr"
    image_base64 = image_to_base64(image_path)

    payload = {
        "image": image_base64,
        "image_type": "jpg"
    }

    response = requests.post(url, json=payload)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    import sys
    sys.path.insert(0, '/app/src')
    from config.settings import get_settings

    settings = get_settings()
    image_path = "/app/src/test_data/test.jpg"

    result = test_ocr_server(
        host=settings.server.host,
        port=settings.server.port,
        image_path=image_path
    )
    print(result)
# python /app/src/test_client.py
import base64
from io import BytesIO
from PIL import Image
import numpy as np
from schame.server_data import OCRPageResult, OCRWord


def base64_to_pil(b64_str: str) -> Image.Image:
    img_bytes = base64.b64decode(b64_str)
    return Image.open(BytesIO(img_bytes)).convert("RGB")


def pil_to_bgr_array(img: Image.Image) -> np.ndarray:
    return np.array(img.convert("RGB"))[:, :, ::-1].copy()


def parse_ocr_result(result) -> OCRPageResult:
    words = []
    for res in result:
        rec_texts = res.get('rec_texts', [])
        rec_scores = res.get('rec_scores', [])
        dt_polys = res.get('dt_polys', [])

        for text, score, box in zip(rec_texts, rec_scores, dt_polys):
            words.append(OCRWord(
                text=text,
                confidence=float(score),
                box=tuple(tuple(float(p) for p in point) for point in box)
            ))

    return OCRPageResult(words=words)
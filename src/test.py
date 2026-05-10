import base64
from io import BytesIO
from PIL import Image
import numpy as np
from paddleocr import PaddleOCR
from config.settings import get_settings
from schame.server_data import OCRResponse, OCRPageResult, OCRWord


def image_path_to_pil(image_path: str) -> Image.Image:
    """从文件路径读取图片为 PIL Image (RGB)"""
    return Image.open(image_path).convert("RGB")


def base64_to_pil(b64_str: str) -> Image.Image:
    """将 Base64 字符串（不含 data:image 前缀）解码为 PIL Image"""
    img_bytes = base64.b64decode(b64_str)
    return Image.open(BytesIO(img_bytes)).convert("RGB")


def pil_to_bgr_array(img: Image.Image) -> np.ndarray:
    """PIL Image (RGB) 转为 OpenCV BGR 格式的 numpy 数组（可选，predict 直接接受 PIL）"""
    opencv_img = np.array(img.convert("RGB"))[:, :, ::-1].copy()  # RGB -> BGR
    return opencv_img


def parse_ocr_result(result) -> OCRResponse:
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

    page_result = OCRPageResult(
        input_path='test_data/test.jpg',
        words=words
    )

    return OCRResponse(
        code=0,
        message='success',
        data=page_result
    )


if __name__ == '__main__':
    settings = get_settings()

    ocr = PaddleOCR(
        text_detection_model_dir=settings.paddleocr.text_detection_model_dir,
        text_recognition_model_dir=settings.paddleocr.text_recognition_model_dir,
        use_doc_orientation_classify=settings.paddleocr.use_doc_orientation_classify,
        use_doc_unwarping=settings.paddleocr.use_doc_unwarping,
        use_textline_orientation=settings.paddleocr.use_textline_orientation,
        lang=settings.paddleocr.lang,
        device=settings.paddleocr.device
    )

    image_path = 'test_data/test.jpg'
    b64_str = base64.b64encode(open(image_path, 'rb').read()).decode()
    pil_img = base64_to_pil(b64_str)

    # 保存解码图，用于人工校验
    save_path = 'test_data/decoded_from_base64.jpg'
    pil_img.save(save_path, 'JPEG')

    # 稳定方式：转成 BGR numpy 数组再预测
    bgr_img = pil_to_bgr_array(pil_img)
    raw_result = ocr.predict(bgr_img)

    print("Raw OCR result:", raw_result)
    response = parse_ocr_result(raw_result)
    print(response.model_dump_json())
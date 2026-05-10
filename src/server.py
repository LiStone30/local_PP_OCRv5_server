import base64
from io import BytesIO
from contextlib import asynccontextmanager
from PIL import Image
import numpy as np
from fastapi import FastAPI, HTTPException
from paddleocr import PaddleOCR
from config.settings import get_settings
from schame.server_data import OCRRequest, OCRResponse, OCRPageResult, OCRWord
from utils.image_utils import base64_to_pil, pil_to_bgr_array, parse_ocr_result


ocr_instance = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ocr_instance
    settings = get_settings()
    ocr_instance = PaddleOCR(
        text_detection_model_dir=settings.paddleocr.text_detection_model_dir,
        text_recognition_model_dir=settings.paddleocr.text_recognition_model_dir,
        use_doc_orientation_classify=settings.paddleocr.use_doc_orientation_classify,
        use_doc_unwarping=settings.paddleocr.use_doc_unwarping,
        use_textline_orientation=settings.paddleocr.use_textline_orientation,
        lang=settings.paddleocr.lang,
        device=settings.paddleocr.device
    )
    yield
    ocr_instance = None


app = FastAPI(lifespan=lifespan)


@app.post("/ocr", response_model=OCRResponse)
async def ocr_image(request: OCRRequest):
    try:
        pil_img = base64_to_pil(request.image)
        bgr_img = pil_to_bgr_array(pil_img)
        raw_result = ocr_instance.predict(bgr_img)
        page_result = parse_ocr_result(raw_result)
        return OCRResponse(code=0, message="success", data=page_result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(app, host=settings.server.host, port=settings.server.port)
# python /app/src/server.py
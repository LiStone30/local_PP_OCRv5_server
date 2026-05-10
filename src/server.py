import signal
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
import sys
import uvicorn


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
        device=settings.paddleocr.device,
        text_det_thresh=0.1,            # 降低阈值，使文字区域更连接
        text_det_box_thresh=0.1,        # 降低保留阈值，保留更多过渡框
        text_det_unclip_ratio=2.0,      # 核心参数：大幅增大扩张系数，让框向外粘连
    )
    yield
    # ---- 清理阶段 ----
    ocr_instance = None
    # 强制垃圾回收，释放可能持有的 GPU 资源
    import gc; gc.collect()
    # 如果 PaddlePaddle 提供了清理 API，可在此调用
    # import paddle; paddle.device.cuda.empty_cache()

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
    settings = get_settings()

    # 注册信号处理函数，确保收到 SIGTERM 时立即退出
    def handle_sigterm(signum, frame):
        print("Received SIGTERM, shutting down gracefully...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    uvicorn.run(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_level="info",
        # 关键：不使用 reload，且建议将超时时间缩短
        timeout_graceful_shutdown=10
    )
# python /app/src/server.py
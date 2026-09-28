import signal
import base64
import logging
import os
import threading
import time
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

# 设置日志级别
logging.getLogger('ppocr').setLevel(logging.WARNING)

# 本服务自己的日志：容器里没有装日志系统，自己挂一个 handler（否则只能靠 logging 的
# lastResort，连时间戳都没有）。默认 INFO；想看每次请求的明细设 OCR_LOG_LEVEL=DEBUG。
log = logging.getLogger('ppocr.server')
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s | %(message)s'))
    log.addHandler(_handler)
log.setLevel(os.environ.get('OCR_LOG_LEVEL', 'INFO').upper())


ocr_instance = None

#: 推理串行锁 —— 把"单 worker 排队"这个既有事实（README §1.4）显式化。
#: 为什么必须有：端点是同步 def，FastAPI 会丢到线程池里并行跑，而 PaddleOCR 的 predict
#: 既不线程安全、内存也不允许并发（2 vCPU / 1.7G 无 swap 的 ECS 上，并发推理成倍吃内存，
#: 历史上就是这样把整机 OOM 掉的）。拿锁以后：事件循环不再被推理占住（慢请求不会卡住
#: 新连接的读取），但真正算的时候仍然只有一个。
_infer_lock = threading.Lock()

#: 单次请求超过这个耗时（秒）就记一条 WARNING —— 与调用方 uiauto/ocr_client.py 同一口径，
#: 事后对时间线时能一眼分清"服务端慢"还是"网络慢"。
_SLOW_REQUEST_S = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ocr_instance
    settings = get_settings()
    ocr_instance = PaddleOCR(
        text_detection_model_dir=settings.paddleocr.text_detection_model_dir,
        text_detection_model_name=settings.paddleocr.text_detection_model_name,
        text_recognition_model_dir=settings.paddleocr.text_recognition_model_dir,
        text_recognition_model_name=settings.paddleocr.text_recognition_model_name,
        use_doc_orientation_classify=settings.paddleocr.use_doc_orientation_classify,
        use_doc_unwarping=settings.paddleocr.use_doc_unwarping,
        use_textline_orientation=settings.paddleocr.use_textline_orientation,
        device=settings.paddleocr.device,
        enable_mkldnn=settings.paddleocr.enable_mkldnn,
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
def ocr_image(request: OCRRequest):
    """同步端点（**不是** async def）：阻塞式推理交给 Starlette 线程池，不占事件循环。

    解码（base64 → PIL → BGR）与 predict 一起放进锁里：并发请求的中间图像不会同时在
    内存里堆着，峰值内存与串行时一致。
    """
    started = time.monotonic()
    try:
        with _infer_lock:
            pil_img = base64_to_pil(request.image)
            bgr_img = pil_to_bgr_array(pil_img)
            raw_result = ocr_instance.predict(bgr_img)
            page_result = parse_ocr_result(raw_result)
    except Exception as e:
        log.exception('OCR 失败: %s', e)      # 原先异常只进了响应体, 服务端日志里查不到
        raise HTTPException(status_code=500, detail=str(e))
    elapsed = time.monotonic() - started
    if elapsed > _SLOW_REQUEST_S:
        log.warning('OCR 慢: 请求 base64 %d B, 耗时 %.2fs', len(request.image), elapsed)
    else:
        log.debug('OCR: 请求 base64 %d B, 耗时 %.3fs', len(request.image), elapsed)
    return OCRResponse(code=0, message="success", data=page_result)


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
        # 关键：不使用 reload
        # keep-alive 空闲超时：uvicorn 默认 5s —— 调用方两次 OCR 的间隔一超过它，连接池就得
        # 重新建连（100+ 次/分钟的量级 = 100+ 条短连接，容易被家宽 NAT / ISP / 云侧边缘掐断）。
        # 调到 75s 后调用方复用同一条连接，依据见 game-automation docs/ocr_api.md §九。
        timeout_keep_alive=settings.server.timeout_keep_alive,
        timeout_graceful_shutdown=settings.server.timeout_graceful_shutdown
    )
# python /app/src/server.py
from pydantic import BaseModel, Field
from typing import List, Tuple, Optional

# -------------------- 基础类型 --------------------
Point = Tuple[float, float]                     # 二维坐标点 (x, y)
TextBox = Tuple[Point, Point, Point, Point]     # 四点文本框（左上、右上、右下、左下）

# -------------------- 单个识别词条 --------------------
class OCRWord(BaseModel):
    text: str = Field(..., description="识别出的文字内容")
    confidence: float = Field(..., ge=0.0, le=1.0, description="文本行的识别置信度")
    box: TextBox = Field(..., description="文本框的四个顶点坐标，顺序：左上、右上、右下、左下")

# -------------------- 单张图片的完整结果 --------------------
class OCRPageResult(BaseModel):
    input_path: Optional[str] = Field(None, description="输入图片的原始文件名（上传时通常提供）")
    words: List[OCRWord] = Field(default_factory=list, description="本页所有识别出的文本行")

# -------------------- 接口统一响应包装（推荐） --------------------
class OCRResponse(BaseModel):
    code: int = Field(0, description="状态码，0 表示成功")
    message: str = Field("success", description="提示信息")
    data: OCRPageResult = Field(..., description="OCR 识别结果")



class OCRRequest(BaseModel):
    """
    OCR 服务的请求体 - Base64 模式
    """
    image: str = Field(
        ...,
        description="图片的 Base64 编码字符串（不含 data:image/...;base64, 前缀）"
    )
    image_type: Optional[str] = Field(
        default="jpg",
        description="图片原始格式，用于解码，支持 jpg/png/bmp 等常见格式"
    )
    # 可以扩展更多业务参数，如：
    # detect_orientation: bool = Field(default=False, description="是否检测文本方向")
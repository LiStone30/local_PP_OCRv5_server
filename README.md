podman pull ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddle:3.1.0-gpu-cuda12.6-cudnn9.5

podman run -d --device nvidia.com/gpu=all --gpus all -p 8118:8118 \
   -v $(pwd)/models:/app/models \
   -v $(pwd)/test.py:/app/test.py \
   -v $(pwd)/test.jpg:/app/test.jpg \
   ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddle:3.1.0-gpu-cuda12.6-cudnn9.5 \
    sleep infinity


podman exec -it 92ec52e04a173deb3b35bf89aec9225cea5054fc774fd64d702da38fb299d6fa bash

pip install paddleocr -i https://pypi.tuna.tsinghua.edu.cn/simple

python3 -c "import paddle; print(paddle.is_compiled_with_cuda()); print(paddle.device.get_device())"


# 打包一个容器为镜像
podman commit --change='CMD ["python", "/app/src/server.py"]' 04ebe45b449e ppocr_v5_server:latest

# 再次配置
sed -i.bak 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list && \
sed -i 's|http://security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list

apt-get update && apt-get install -y libglib2.0-0 libgl1 libsm6 libxext6 libxrender-dev

apt-get install -y 

# 写一个服务
podman run -d --device nvidia.com/gpu=all --gpus all -p 8118:8118 \
   -v $(pwd)/models:/app/models \
   -v $(pwd)/src:/app/src \
   ppocr_v5_server:latest \
    sleep infinity

podman exec -it 04ebe45b449e76ba956925c24251d441938208f45969132f0ecb6afa5eaadb2d bash

apt-get update && apt-get install -y libglib2.0-0 libgl1 libsm6 libxext6 libxrender-dev libgl1-mesa-glx

pip install fastapi uvicorn -i https://pypi.tuna.tsinghua.edu.cn/simple

# 运行服务
podman run -d --device nvidia.com/gpu=all --gpus all -p 8118:8118 \
   -v $(pwd)/models:/app/models \
   -v $(pwd)/src:/app/src \
   ppocr_v5_server:latest 

podman exec -it 25bae1cf08efc8e886a662dd78121ecae61b596f43edb03d0d6a99d1b0269957 bash

# 一键启动服务

podman-compose up -d

podman-compose logs -f

podman-compose down

git commit -m "这个服务对 文本分块的问题 效果特别差。一句话 分为3个文本块"

## 手动启动 / 停止服务（当前约定：无任何开机自启 / 自动拉起）

> 服务器上已不再配置开机自启（原 ocr-startup.service 已取消），
> 容器也未设置 restart 策略（进程退出不会自动重启）。
> 需要服务时，在服务器上本仓库目录手动执行：

```bash
# 启动（幂等：已在运行则直接退出；会自动检查模型盘挂载、刷新 CDI 并等待就绪）
bash start_service.sh

# 停止
podman-compose down

# 跟踪日志
podman-compose logs -f
```
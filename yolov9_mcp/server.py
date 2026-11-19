import os
import sys
from pathlib import Path
import torch
import cv2
import numpy as np
from fastmcp import FastMCP

# ================= 終極外掛：強行寫入 Node.js 系統路徑 =================
NODE_PATH = r"C:\Program Files\nodejs"
if NODE_PATH not in os.environ["PATH"]:
    os.environ["PATH"] = NODE_PATH + os.pathsep + os.environ["PATH"]
# =====================================================================

# 1. 動態將王博士的 yolov9 資料夾加入 Python 路徑
YOLOV9_DIR = Path(__file__).parent / "yolov911"
if str(YOLOV9_DIR) not in sys.path:
    sys.path.insert(0, str(YOLOV9_DIR))

# 匯入王博士專案內的原生模組
from models.common import DetectMultiBackend
from utils.general import check_img_size, non_max_suppression, scale_boxes
from utils.augmentations import letterbox

# 2. 初始化 FastMCP
mcp = FastMCP("WongKinYiu-YOLOv9-Server")

# 宣告全域變數，用來暫存模型
model = None
stride, names, pt, imgsz = None, None, None, None
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
MODEL_PATH = str(Path(__file__).parent / "best9.pt")

@mcp.tool()
def detect_objects(image_path: str, conf_threshold: float = 0.25, iou_threshold: float = 0.45) -> str:
    """
    使用王博士原生的 YOLOv9 架構進行物件偵測。
    """
    global model, stride, names, pt, imgsz
    
    if not os.path.exists(image_path):
        return f"錯誤：找不到圖片檔案 {image_path}"
    
    try:
        # 💡 延遲載入機制：當第一次執行 Tool 時才載入模型，避免啟動時連線超時
        if model is None:
            if not os.path.exists(MODEL_PATH):
                return f"錯誤：找不到權重檔：{MODEL_PATH}"
            
            print(f"【背景提示】正在首次載入王博士 YOLOv9 模型 ({device})...")
            model = DetectMultiBackend(MODEL_PATH, device=device, dnn=False, data=None, fp16=False)
            stride, names, pt = model.stride, model.names, model.pt
            imgsz = check_img_size((640, 640), s=stride)
            print("【背景提示】模型載入完成！")

        # 1. 讀取影像
        img0 = cv2.imread(image_path)
        if img0 is None:
            return "錯誤：無法讀取該影像，請確認格式是否正確。"
        
        # 2. 影像預處理 (Letterbox)
        img = letterbox(img0, imgsz, stride=stride, auto=True)[0]
        img = img.transpose((2, 0, 1))[::-1]  # HWC to CHW, BGR to RGB
        img = np.ascontiguousarray(img)
        
        # 3. 轉為 Tensor
        im = torch.from_numpy(img).to(device)
        im = im.float()
        im /= 255.0  # 正規化 0-1
        if len(im.shape) == 3:
            im = im[None]  # 增加 batch 維度
            
        # 4. 模型推論
        pred = model(im, augment=False, visualize=False)
        
        # 修正：如果王博士的模型回傳的是 list，取第一個真正預測的 Tensor
        if isinstance(pred, (list, tuple)):
            pred = pred[0]
        
        # 5. 非極大值抑制 (NMS)
        pred = non_max_suppression(pred, conf_threshold, iou_threshold, classes=None, agnostic=False, max_det=1000)
        
        # 6. 解析結果
        output_results = []
        for i, det in enumerate(pred):
            if len(det):
                det[:, :4] = scale_boxes(im.shape[2:], det[:, :4], img0.shape).round()
                
                for *xyxy, conf, cls in reversed(det):
                    cls_id = int(cls)
                    cls_name = names[cls_id] if cls_id in names else f"class_{cls_id}"
                    confidence = float(conf)
                    box = [int(x) for x in xyxy]
                    
                    output_results.append(
                        f"物件: {cls_name}, 置信度: {confidence:.2f}, 座標: {box}"
                    )
                    
        if not output_results:
            return "偵測完成：未偵測到任何物件。"
            
        return "【YOLOv9 原生模型偵測結果】\n" + "\n".join(output_results)
        
    except Exception as e:
        return f"原生推論過程中發生錯誤: {str(e)}"

if __name__ == "__main__":
    mcp.run()
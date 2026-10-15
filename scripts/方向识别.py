"""离线四方向识别，不依赖飞桨运行环境。"""

from dataclasses import dataclass
import hashlib
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

MODEL_HASH = "2f62c9bfb830a0b417241269fde7ef2d0ad5446c0ed2b8af33b1f6543545e8e2"


@dataclass(frozen=True)
class 判断:
    rotation: int | None
    confidence: float
    reason: str


class 识别器:
    def __init__(self, model_path=None):
        path = Path(model_path) if model_path else Path(__file__).resolve().parent / "models" / "rapid_orientation.onnx"
        if not path.is_file():
            raise FileNotFoundError("缺少项目内方向模型，请检查 models 文件夹。")
        if hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_HASH:
            raise ValueError("方向模型完整性校验失败，请重新取得原始模型。")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        self.name = self.session.get_inputs()[0].name
        labels = self.session.get_modelmeta().custom_metadata_map.get("character", "").splitlines()
        if labels != ["0", "90", "180", "270"]:
            raise ValueError("模型四方向标签不符合预期。")

    def _scores(self, image):
        w, h = image.size
        ratio = 256 / min(w, h)
        image = image.resize((round(w * ratio), round(h * ratio)), Image.Resampling.LANCZOS)
        w, h = image.size
        # 多区域一致性减少印章、局部竖字对整页方向的干扰。
        positions = [((w - 224) // 2, (h - 224) // 2),
                     ((w - 224) // 2, (h - 224) // 4),
                     ((w - 224) // 2, (h - 224) * 3 // 4)] if h >= w else [
                         ((w - 224) // 2, (h - 224) // 2),
                         ((w - 224) // 4, (h - 224) // 2),
                         ((w - 224) * 3 // 4, (h - 224) // 2)]
        crops = []
        for x, y in positions:
            a = np.asarray(image.crop((x, y, x + 224, y + 224)), dtype=np.float32)[:, :, ::-1] / 255
            a = (a - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)
            crops.append(a.transpose(2, 0, 1))
        scores = self.session.run(None, {self.name: np.ascontiguousarray(crops, dtype=np.float32)})[0]
        if scores.shape != (3, 4) or not np.isfinite(scores).all():
            raise ValueError("方向模型返回无效结果。")
        return scores

    def predict(self, image):
        image = image.convert("RGB")
        if min(image.size) < 32:
            return 判断(None, 0., "页面尺寸过小，保留原方向")
        gray = np.asarray(image.convert("L"))
        ink = int(np.count_nonzero(gray < 180))
        if ink < max(250, gray.size * .0015):
            return 判断(None, 0., "空白或文字过少，保留原方向")
        scores = self._scores(image)
        mean = scores.mean(axis=0)
        index = int(mean.argmax())
        confidence = float(mean[index])
        votes = scores.argmax(axis=1)
        if np.count_nonzero(votes == index) < 3 or confidence < .70:
            return self._compare(image, scores)
        correction = (-index * 90) % 360
        if correction:
            checked = self._scores(image.rotate(-correction, expand=True))
            if (checked.argmax(axis=1) != 0).any() or checked[:, 0].mean() < .70:
                return self._compare(image, scores)
        return 判断(correction, confidence, "多区域方向一致")

    def _compare(self, image, original):
        candidates = [original] + [self._scores(image.rotate(-a, expand=True)) for a in (90, 180, 270)]
        values = np.array([s[:, 0].mean() for s in candidates])
        index = int(values.argmax())
        best = candidates[index]
        confidence = float(values[index])
        runner_up = float(np.sort(values)[-2])
        if confidence < .70 or confidence - runner_up < .20 or (best.argmax(axis=1) != 0).any():
            return 判断(None, confidence, "四方向比较仍无可靠一致证据，保留原方向")
        return 判断(index * 90, confidence, "四方向比较及多区域复查通过")

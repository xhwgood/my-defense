# -*- coding: utf-8 -*-
"""
card_policy.py —— 卡牌选择策略：局内上下文评分 + 决策日志。

旧的 best_card() 是一张全局固定的优先顺序表：索引越小越优先，与这一局已经选了
哪些卡、卡牌等级都不相关，因此三张候选分属不同流派时会来回横跳，已投入的流派
反而接不上。本模块在原有优先级基础上叠加局内上下文：

    score = base × 全局优先级分
          + family_synergy × 已选同流派张数
          + family_momentum（同流派已达到 threshold 张时一次性加成）
          + same_card_bonus（已拥有同名卡，属于升级）

权重默认写在 DEFAULT_WEIGHTS，可用同目录的 card_policy.json 覆盖（便于调参）。

每次选牌追加一行 JSON 到 logs/card_decisions.jsonl，并在开始界面/通关/挑战失败
时写入 run_start、run_end，记录本局结果。这些数据用于后续离线校准权重，无需人工标注。
"""

import json
import time
import uuid
from pathlib import Path

from card_constants import (
    ANTIGRAVITY_CARD_NAMES,
    CARD_NAME_LIST,
    HIVE_CARD_NAMES,
    JAMMER_CARD_NAMES,
    OTHER_CARD_NAMES,
    RAILGUN_CARD_NAMES,
)

_CONFIG_PATH = Path(__file__).with_name("card_policy.json")
_LOG_PATH = Path(__file__).with_name("logs") / "card_decisions.jsonl"

# 卡牌优先级表的流派分组；顺序必须与 card_constants.CARD_NAME_LIST 的拼接顺序一致。
_FAMILY_GROUPS = (
    ("蜂巢", HIVE_CARD_NAMES),
    ("反重力", ANTIGRAVITY_CARD_NAMES),
    ("电磁炮", RAILGUN_CARD_NAMES),
    ("干扰", JAMMER_CARD_NAMES),
    ("其他", OTHER_CARD_NAMES),
)

FAMILY_BY_INDEX = tuple(
    family for family, names in _FAMILY_GROUPS for _ in names
)
if len(FAMILY_BY_INDEX) != len(CARD_NAME_LIST):
    raise RuntimeError("card_constants 中 CARD_NAME_LIST 与各流派数组长度不一致")

# OCR 常把卡牌名尾部的罗马数字等级误识别成形近字符（如“强化连射Ⅲ”→“强化连射川”），
# 这些字符只会出现在等级后缀里，解析等级后即可剔除。
_LEVEL_SUFFIX_LOOKALIKES = set("川州洲丨")

# 等级标记：长 token 在前，避免 “III” 被识别成 “I”。
_LEVEL_TOKENS = (
    ("Ⅵ", 6), ("Ⅴ", 5), ("Ⅳ", 4), ("Ⅲ", 3), ("Ⅱ", 2), ("Ⅰ", 1),
    ("VI", 6), ("IV", 4), ("III", 3), ("II", 2), ("V", 5), ("I", 1),
    ("六", 6), ("五", 5), ("四", 4), ("三", 3), ("二", 2), ("一", 1),
    ("川", 3), ("州", 3), ("洲", 3), ("丨", 3),
    ("6", 6), ("5", 5), ("4", 4), ("3", 3), ("2", 2), ("1", 1),
)

DEFAULT_WEIGHTS = {
    "base": 1.0,                     # 全局优先级权重
    "family_synergy": 0.12,          # 每张已选同流派卡牌的加成
    "family_momentum_threshold": 3,  # 同流派达到该张数后触发一次性加成
    "family_momentum": 0.25,         # 一次性加成值，鼓励流派成型
    "same_card_bonus": 0.15,         # 已拥有同名卡（升级）加成
}


def _keep_chinese(s):
    """只保留中文字符，剔除数字、空格、罗马数字等 OCR 附带的无关注缀。"""
    return "".join(ch for ch in s if "一" <= ch <= "鿿" and ch not in _LEVEL_SUFFIX_LOOKALIKES)


def _build_name_index():
    """卡牌名（归一化后）→ CARD_NAME_LIST 索引；同名卡牌取索引最小的（优先级最高）。"""
    index = {}
    for i, name in enumerate(CARD_NAME_LIST):
        index.setdefault(_keep_chinese(name), i)
    return index


_NAME_TO_INDEX = _build_name_index()


def parse_card(text):
    """从 OCR 文本解析出卡牌：(CARD_NAME_LIST 索引, 等级)；识别不到返回 (None, 1)。

    等级缺失时按 1 处理——只是少了一点加成，不影响选牌。
    """
    level = 1
    rest = text
    for token, value in _LEVEL_TOKENS:
        pos = rest.rfind(token)
        if pos != -1:
            level = value
            rest = rest[:pos] + rest[pos + len(token):]
            break
    idx = _NAME_TO_INDEX.get(_keep_chinese(rest))
    return idx, level


def load_weights(path=_CONFIG_PATH):
    """读取 card_policy.json 覆盖默认权重；文件不存在或格式错误时用默认值。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[!] 读取 {path.name} 失败（{type(e).__name__}: {e}），使用默认卡牌权重")
        return {}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


class CardPolicy:
    """维护单局构筑状态，按上下文评分选牌，并把每次决策写入 JSONL 日志。"""

    def __init__(self, weights=None, log_path=None):
        self.weights = dict(DEFAULT_WEIGHTS)
        self.weights.update(load_weights())
        if weights:
            self.weights.update(weights)
        self.log_path = Path(log_path) if log_path else _LOG_PATH
        self._log_failed = False
        self.start_run()

    # ---- 局内状态 ----
    def start_run(self):
        """开始新的一局（在「开始界面」时调用）；上一局未记录结果则按 unknown 归档。"""
        if getattr(self, "step", 0) and not self.run_closed:
            self.end_run("unknown", "脚本未检测到本局通关/失败就开始了下一局")
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.run_started_at = _now()
        self.step = 0
        self.family_counts = {}
        self.card_counts = {}
        self.picked = []
        self.run_closed = False
        # run_start 延迟到本局第一次选牌时再写，避免 probe/空跑留下一堆没有决策的空局记录。
        self._run_start_logged = False

    def end_run(self, outcome, detail=""):
        """记录本局结果：outcome 为 clear（通关）/ lose（挑战失败）/ unknown。"""
        if self.run_closed or not self.step:
            return
        self.run_closed = True
        self._append({"type": "run_end", "ts": _now(), "run_id": self.run_id,
                      "outcome": outcome, "steps": self.step,
                      "build": dict(self.family_counts), "picked": list(self.picked),
                      "detail": detail})

    # ---- 评分 ----
    def _score(self, idx):
        w = self.weights
        family = FAMILY_BY_INDEX[idx]
        base = (len(CARD_NAME_LIST) - idx) / len(CARD_NAME_LIST)
        score = w["base"] * base
        n_family = self.family_counts.get(family, 0)
        score += w["family_synergy"] * n_family
        if n_family >= w["family_momentum_threshold"]:
            score += w["family_momentum"]
        if self.card_counts.get(CARD_NAME_LIST[idx], 0) > 0:
            score += w["same_card_bonus"]
        return score, family

    def _candidates(self, texts, clicks):
        candidates = []
        for slot, text in enumerate(texts):
            idx, level = parse_card(text)
            if idx is None:
                continue
            score, family = self._score(idx)
            candidates.append({
                "slot": slot,
                "text": text,
                "idx": idx,
                "name": CARD_NAME_LIST[idx],
                "level": level,
                "family": family,
                "score": round(score, 4),
                "click": clicks[slot],
            })
        candidates.sort(key=lambda c: (-c["score"], c["idx"]))
        return candidates

    def choose(self, texts, clicks, record=True):
        """从三张候选卡中选分数最高的一张。

        record=True 时更新局内构筑并写日志（真实点击时用）；probe 等只读场景传 False。
        返回选中项（含 candidates 明细）；没有可识别的卡牌时返回 None。
        """
        candidates = self._candidates(texts, clicks)
        if not candidates:
            return None
        best = dict(candidates[0])
        best["candidates"] = candidates

        if record:
            if not self._run_start_logged:
                self._run_start_logged = True
                self._append({"type": "run_start", "ts": self.run_started_at,
                              "run_id": self.run_id, "weights": self.weights})
            build_before = dict(self.family_counts)
            self.step += 1
            self.family_counts[best["family"]] = self.family_counts.get(best["family"], 0) + 1
            self.card_counts[best["name"]] = self.card_counts.get(best["name"], 0) + 1
            self.picked.append({"name": best["name"], "level": best["level"], "step": self.step})
            self._append({
                "type": "decision",
                "ts": _now(),
                "run_id": self.run_id,
                "step": self.step,
                "ocr": list(texts),
                "offered": [{k: c[k] for k in ("slot", "name", "level", "family", "idx", "score")}
                            for c in candidates],
                "chosen": {k: best[k] for k in ("name", "level", "family", "idx", "score")},
                "build_before": build_before,
            })
        return best

    # ---- 日志 ----
    def _append(self, record):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            if not self._log_failed:
                print(f"[!] 写入选牌日志失败（{type(e).__name__}: {e}），后续不再提示")
                self._log_failed = True

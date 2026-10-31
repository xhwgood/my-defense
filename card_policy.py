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

若已拿到某流派的核心卡（CORE_CARD_NAMES），该流派的名次会在优先级里上升一位
（每张核心卡各升一位），base 随之抬升，用来表达「拿到核心卡后流派质变」。

权重默认写在 DEFAULT_WEIGHTS，可用同目录的 card_policy.json 覆盖（便于调参）。

每局结束（通关/挑战失败）时，会把本局胜负与各次选牌的特征差追加到 logs/card_policy_samples.jsonl；
每满 FIT_EVERY_RUNS 局，就用最近的对局数据做一次岭回归批量拟合，并落盘到
logs/card_policy_learned.json，脚本重启后继续累加。base 与动量阈值保持人工设定，不参与拟合。

每次选牌追加一行 JSON 到 logs/card_decisions.jsonl，并在开始界面/通关/挑战失败
时写入 run_start、run_end，记录本局结果。这些数据用于后续离线校准权重，无需人工标注。
"""

import json
import time
import uuid
from pathlib import Path

import numpy as np

from card_constants import (
    ANTIGRAVITY_CARD_NAMES,
    CARD_NAME_LIST,
    FIREWHEEL,
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
    ("火轮", FIREWHEEL),
    ("电磁炮", RAILGUN_CARD_NAMES),
    ("干扰", JAMMER_CARD_NAMES),
    ("其他", OTHER_CARD_NAMES),
)

FAMILY_BY_INDEX = tuple(
    family for family, names in _FAMILY_GROUPS for _ in names
)
if len(FAMILY_BY_INDEX) != len(CARD_NAME_LIST):
    raise RuntimeError("card_constants 中 CARD_NAME_LIST 与各流派数组长度不一致")

# 流派默认名次（0 起）与各流派张数，用于把「派内第几张」换算成全局优先级。
_FAMILY_ORDER = tuple(family for family, _ in _FAMILY_GROUPS)
_FAMILY_DEFAULT_RANK = {family: rank for rank, family in enumerate(_FAMILY_ORDER)}
_FAMILY_CARD_COUNT = {family: len(names) for family, names in _FAMILY_GROUPS}
_WITHIN_FAMILY_INDEX = tuple(
    i for _, names in _FAMILY_GROUPS for i in range(len(names))
)

# 质变核心卡：每拿到其中一张，该流派在优先级里上升一位；已排第一的流派不再上升。
# 没有核心卡的流派不用写。
CORE_CARD_NAMES = {
    "电磁炮": ("陷阱电网",),
}

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

# ---- 可学习权重 ----
# 只有下面三个上下文权重参与拟合；base 与动量阈值保持人工设定（前者是分数锚点，后者是离散阈值）。
LEARNED_KEYS = ("family_synergy", "family_momentum", "same_card_bonus")
WEIGHT_BOUNDS = {
    "family_synergy": (0.0, 0.6),
    "family_momentum": (0.0, 1.2),
    "same_card_bonus": (0.0, 0.6),
}

# ---- 批量拟合 ----
# 每完成 FIT_EVERY_RUNS 局（有通关/失败结果）就用最近的对局数据重新拟合一次三个权重。
# 样本少时可以调小（如 3）方便快速观察；稳定运行后建议改大（如 30~50），减少运气带来的抖动。
FIT_EVERY_RUNS = 3
FIT_SAMPLE_WINDOW_RUNS = 100   # 只用最近这么多局拟合，避免旧策略的数据长期拖累
FIT_RIDGE_LAMBDA = 1.0         # 岭回归正则强度，样本少时抑制极端系数
FIT_PRIOR_STRENGTH = 30        # 拟合值向人工先验收缩的强度（决策样本数），越大越保守
WIN_RATE_EMA = 0.1             # 胜率滑动平均系数，仅用于日志展示

_LEARNED_PATH = Path(__file__).with_name("logs") / "card_policy_learned.json"
_SAMPLES_PATH = Path(__file__).with_name("logs") / "card_policy_samples.jsonl"


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

    等级仅写入日志备查，不参与评分。
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
    """维护单局构筑状态，按上下文评分选牌，记录决策日志，并定期用累计胜负批量拟合权重。"""

    def __init__(self, weights=None, log_path=None, learned_path=None, samples_path=None):
        self.weights = dict(DEFAULT_WEIGHTS)
        self.weights.update(load_weights())
        if weights:
            self.weights.update(weights)
        self.log_path = Path(log_path) if log_path else _LOG_PATH
        self.learned_path = Path(learned_path) if learned_path else _LEARNED_PATH
        self.samples_path = Path(samples_path) if samples_path else _SAMPLES_PATH
        self._log_failed = False
        self.samples = []
        self.runs_learned = 0
        self.runs_since_fit = 0
        self.win_rate = 0.5
        self._load_learned()
        self._load_samples()
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
        self._decision_diffs = []
        self.run_closed = False
        # run_start 延迟到本局第一次选牌时再写，避免 probe/空跑留下一堆没有决策的空局记录。
        self._run_start_logged = False

    def end_run(self, outcome, detail=""):
        """记录本局结果（clear 通关 / lose 挑战失败 / unknown），并据此在线更新权重。"""
        if self.run_closed or not self.step:
            return
        self.run_closed = True
        self._append({"type": "run_end", "ts": _now(), "run_id": self.run_id,
                      "outcome": outcome, "steps": self.step,
                      "build": dict(self.family_counts), "picked": list(self.picked),
                      "detail": detail})
        self._learn(outcome)

    # ---- 评分 ----
    def _features(self, idx):
        """单张卡相对当前构筑的特征：同流派张数、是否已到动量阈值、是否同名升级。"""
        family = FAMILY_BY_INDEX[idx]
        n_family = self.family_counts.get(family, 0)
        return {
            "family_synergy": float(n_family),
            "family_momentum": 1.0 if n_family >= self.weights["family_momentum_threshold"] else 0.0,
            "same_card_bonus": 1.0 if self.card_counts.get(CARD_NAME_LIST[idx], 0) > 0 else 0.0,
        }

    def _priority_order(self):
        """当前流派名次：每拥有一张核心卡，该流派从当前位置上升一位。"""
        order = list(_FAMILY_ORDER)
        for family in _FAMILY_ORDER:
            steps = sum(1 for name in CORE_CARD_NAMES.get(family, ())
                        if self.card_counts.get(name, 0) > 0)
            pos = order.index(family)
            target = max(0, pos - steps)
            if target != pos:
                order.pop(pos)
                order.insert(target, family)
        return order

    def _priority_base(self, idx):
        """卡牌全局优先级分（0~1）：按当前流派名次 + 派内顺序换算，随核心卡到手而变化。"""
        family = FAMILY_BY_INDEX[idx]
        offset = 0
        for current in self._priority_order():
            if current == family:
                position = offset + _WITHIN_FAMILY_INDEX[idx]
                return (len(CARD_NAME_LIST) - position) / len(CARD_NAME_LIST)
            offset += _FAMILY_CARD_COUNT[current]
        return 0.0

    def _score(self, idx):
        base = self._priority_base(idx)
        feats = self._features(idx)
        score = self.weights["base"] * base
        score += sum(self.weights[key] * feats[key] for key in LEARNED_KEYS)
        return score, FAMILY_BY_INDEX[idx]

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
            # 记录「选中卡 - 全部候选均值」的特征差，本局结束后用胜负做一次策略梯度更新。
            feature_maps = [self._features(c["idx"]) for c in candidates]
            mean_feat = {key: sum(f[key] for f in feature_maps) / len(feature_maps)
                         for key in LEARNED_KEYS}
            chosen_feat = self._features(best["idx"])
            self._decision_diffs.append(
                {key: chosen_feat[key] - mean_feat[key] for key in LEARNED_KEYS}
            )
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

    # ---- 批量拟合 ----
    def _learn(self, outcome):
        """把本局结果与各次决策的特征差存为一条样本；每满 FIT_EVERY_RUNS 局触发一次批量拟合。

        unknown（未看到通关/失败）不产生样本。
        """
        if outcome not in ("clear", "lose") or not self._decision_diffs:
            return
        win = outcome == "clear"
        self.win_rate += WIN_RATE_EMA * ((1.0 if win else 0.0) - self.win_rate)
        sample = {
            "type": "sample",
            "ts": _now(),
            "run_id": self.run_id,
            "y": int(win),
            "diffs": [{key: round(d[key], 6) for key in LEARNED_KEYS}
                      for d in self._decision_diffs],
        }
        self._decision_diffs = []
        self.samples.append(sample)
        self.samples = self.samples[-FIT_SAMPLE_WINDOW_RUNS:]
        self._append(sample, self.samples_path)
        self.runs_learned += 1
        self.runs_since_fit += 1
        if self.runs_since_fit >= FIT_EVERY_RUNS:
            self._fit()

    def _fit(self):
        """用累计样本做岭回归：胜负 ~ 1 + Σ 系数 × (选中卡特征 - 候选均值特征)。

        样本量少时拟合系数很噪，因此按样本量向当前权重（人工先验）收缩：
        alpha = 决策数 / (决策数 + FIT_PRIOR_STRENGTH)，最后再夹到权重上下界内。
        """
        self.runs_since_fit = 0
        if len(self.samples) < FIT_EVERY_RUNS:
            return
        rows, labels = [], []
        for sample in self.samples:
            for diff in sample["diffs"]:
                rows.append([diff[key] for key in LEARNED_KEYS])
                labels.append(sample["y"])
        if not rows:
            return

        X = np.column_stack([np.ones(len(rows)), np.asarray(rows, dtype=float)])
        y = np.asarray(labels, dtype=float)
        reg = np.eye(X.shape[1]) * FIT_RIDGE_LAMBDA
        reg[0, 0] = 0.0  # 截距不加惩罚
        coef = np.linalg.solve(X.T @ X + reg, X.T @ y)

        alpha = len(rows) / (len(rows) + FIT_PRIOR_STRENGTH)
        before = {key: round(self.weights[key], 4) for key in LEARNED_KEYS}
        fitted = {}
        for i, key in enumerate(LEARNED_KEYS):
            low, high = WEIGHT_BOUNDS[key]
            fitted[key] = round(float(coef[i + 1]), 4)
            blended = (1.0 - alpha) * self.weights[key] + alpha * fitted[key]
            self.weights[key] = min(high, max(low, blended))
        after = {key: round(self.weights[key], 4) for key in LEARNED_KEYS}

        print(f"[拟合] 用最近 {len(self.samples)} 局（{len(rows)} 条决策）拟合，"
              f"数据权重 {alpha:.0%}｜"
              f"协同 {before['family_synergy']:.3f}→{after['family_synergy']:.3f}，"
              f"动量 {before['family_momentum']:.3f}→{after['family_momentum']:.3f}，"
              f"同名 {before['same_card_bonus']:.3f}→{after['same_card_bonus']:.3f}｜"
              f"累计 {self.runs_learned} 局，胜率 {self.win_rate:.0%}")
        self._append({"type": "fit", "ts": _now(), "run_id": self.run_id,
                      "runs": len(self.samples), "decisions": len(rows),
                      "alpha": round(alpha, 4), "coef": fitted,
                      "weights_before": before, "weights_after": after,
                      "runs_learned": self.runs_learned,
                      "win_rate": round(self.win_rate, 4)})
        self._save_learned()

    def _save_learned(self):
        """把拟合出的权重落盘，脚本重启后接着用。"""
        try:
            self.learned_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "weights": {key: round(self.weights[key], 6) for key in LEARNED_KEYS},
                "runs_learned": self.runs_learned,
                "runs_since_fit": self.runs_since_fit,
                "win_rate": round(self.win_rate, 6),
            }
            self.learned_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            print(f"[!] 保存拟合权重失败：{type(e).__name__}: {e}")

    def _load_learned(self):
        """载入上次落盘的拟合权重；只覆盖可学习的三个键，其余用人工配置。"""
        if not self.learned_path.is_file():
            return
        try:
            data = json.loads(self.learned_path.read_text(encoding="utf-8"))
            for key, value in (data.get("weights") or {}).items():
                if key in LEARNED_KEYS:
                    low, high = WEIGHT_BOUNDS[key]
                    self.weights[key] = min(high, max(low, float(value)))
            self.runs_learned = int(data.get("runs_learned", 0))
            self.runs_since_fit = int(data.get("runs_since_fit", 0))
            self.win_rate = float(data.get("win_rate", 0.5))
            print(f"[拟合] 已载入拟合权重（累计 {self.runs_learned} 局，"
                  f"胜率 {self.win_rate:.0%}）：" +
                  "，".join(f"{key} {self.weights[key]:.3f}" for key in LEARNED_KEYS))
        except Exception as e:
            print(f"[!] 读取 {self.learned_path.name} 失败（{type(e).__name__}: {e}），使用默认权重")

    def _load_samples(self):
        """载入历史对局样本，使批量拟合在脚本重启后仍能用到之前的数据。"""
        if not self.samples_path.is_file():
            return
        try:
            lines = self.samples_path.read_text(encoding="utf-8").splitlines()
            for line in lines[-FIT_SAMPLE_WINDOW_RUNS:]:
                try:
                    sample = json.loads(line)
                except Exception:
                    continue
                if "diffs" in sample:
                    self.samples.append(sample)
            self.samples = self.samples[-FIT_SAMPLE_WINDOW_RUNS:]
            if self.samples:
                print(f"[拟合] 已载入 {len(self.samples)} 局历史样本")
        except Exception as e:
            print(f"[!] 读取 {self.samples_path.name} 失败（{type(e).__name__}: {e}）")

    # ---- 日志 ----
    def _append(self, record, path=None):
        path = Path(path) if path else self.log_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            if not self._log_failed:
                print(f"[!] 写入选牌日志失败（{type(e).__name__}: {e}），后续不再提示")
                self._log_failed = True

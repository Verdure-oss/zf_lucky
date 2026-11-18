"""
回复语料与内容生成。

三档来源，按优先级降级：

  1. **结合帖子内容**（首选）
     从标题/正文里提取关键词（轴体、键帽、桌垫、套件…），
     拼出与内容沾边的短句，例如：
       标题「【GB】栗子茶轻音轴…」→「栗子茶轻音轴看着不错，冲一个」
       标题「GWANG 毛毡桌垫」     →「桌垫质感可以，蹲一份」

  2. **通用抽奖语**（提取不到关键词时）
     冲冲冲 / 来了来了 / 祝我好运 …

  3. **站点快捷语**（兜底）
     冲冲冲

为什么不用站点的「一键冲冲冲」按钮：
  前端 fastSubmit() 直接把写死的 fastReplyContent（默认 "冲冲冲"）提交，
  无法自定义内容。要发与内容相关的评论，只能走编辑器输入。

语料可在 .env 用 REPLY_CORPUS 覆盖通用语；
关键词库与模板在下方常量里，改完无需改配置。
"""

from __future__ import annotations

import logging
import random
import re
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ---------- 通用语（与内容无关时的兜底）----------

DEFAULT_CORPUS = [
    # 常见的抽奖参与语（参考站内真实回复风格）
    "冲冲冲",
    "来了来了",
    "参与一下",
    "祝我好运",
    "抽我抽我",
    "蹲一个",
    "支持支持",
    "希望能中",
    "这个可以有",
    "试试运气",
    "想要一份",
    "安排上",
    "支持一波",
    "那必须是我了",
    "凑个热闹",
    "来晚了没",
    "中不中无所谓，参与一下",
    "求个好运",
    "这个真想要",
    "蹲蹲蹲",
    "来一份",
    "碰碰运气",
    "希望能轮到我",
    "留个名",
    "凑个数",
    "想要想要",
    "看起来很香",
    "心动",
]

# ---------- 品类关键词：命中后用于生成相关评论 ----------

# 每个品类：(显示名, 关键词列表, 评论模板)
# 模板里 {item} 会被替换成品类名或更具体的型号
CATEGORIES: List[Dict] = [
    {
        "name": "轴体",
        "words": ["轴", "段落", "线性", "闷轴", "磁轴", "静音", "触发", "克数"],
        "templates": [
            "{item}手感怎么样，蹲一个",
            "这个轴看着挺对味，冲",
            "{item}想试试，抽一个",
            "轴体有兴趣，参与一下",
            "{item}这价位可以",
            "段落手感听着不错，蹲",
                    "想试试这个轴，蹲",
            "手感听着不错，来一份",
            "这个轴可以，参与",
            "轴体爱好者路过，抽一个",
            "段落感挺期待",
        ],
    },
    {
        "name": "键帽",
        "words": ["键帽", "PBT", "PC", "二色", "字符", "配色", "个性键", "套装"],
        "templates": [
            "{item}配色好看，蹲一份",
            "这套键帽有点东西，参与",
            "{item}想上手看看，抽一个",
            "键帽质感可以，支持一下",
            "{item}挺对味，冲",
            "这套想收，蹲一个",
                    "这套配色挺戳我，蹲",
            "键帽党报到，抽一个",
            "字符设计不错，参与",
            "想收一套，支持",
            "这个主题有意思",
        ],
    },
    {
        "name": "桌垫",
        "words": ["桌垫", "鼠标垫", "毛毡", "垫子", "pad"],
        "templates": [
            "{item}桌面刚需，蹲一个",
            "这个垫子看质感不错，参与",
            "{item}尺寸挺合适，抽一份",
            "桌垫想换一个，支持",
            "{item}铺桌面挺搭",
                    "桌面正好缺个垫，蹲",
            "这个图案可以，参与",
            "桌垫党来了，抽一个",
            "想要大尺寸的",
        ],
    },
    {
        "name": "套件",
        "words": ["套件", "kit", "60", "65", "75", "80", "配列", "外壳", "阳极", "电泳"],
        "templates": [
            "{item}配列挺合胃口，蹲一个",
            "这个套件工艺可以，参与",
            "{item}想组一把，抽一个",
            "配列挺对味，支持一下",
            "{item}外壳看着不错",
                    "想组一把，蹲",
            "这个配列可以，参与",
            "套件党报到，抽一个",
            "工艺看着不错",
        ],
    },
    {
        "name": "成品",
        "words": ["成品", "整机", "键盘", "磁轴键盘", "三模"],
        "templates": [
            "{item}看着不错，蹲一份",
            "这把键盘想上手试试，参与",
            "{item}配置可以，抽一个",
            "成品省心，蹲一个",
                    "开箱就用的省心，蹲",
            "这把可以，参与",
            "想入手一台",
        ],
    },
    {
        "name": "数据线",
        "words": ["线材", "数据线", "cable", "编织线", "航插线"],
        "templates": [
            "{item}配色挺搭，蹲一个",
            "这根线想换，参与",
            "线材质感可以，支持",
                    "想换根线，蹲",
            "配色不错，参与",
            "线材党报到",
        ],
    },
]

# 通用收尾词：拼在相关评论前面，增加变化
OPENERS = ["", "", "", "看着不错，", "有点心动，", "挺有意思，", "蹲了很久，"]

# 站点的回复长度限制（保守值）
MIN_LEN = 1
MAX_LEN = 100

# 从标题里提取“型号”用的噪声词：这些不该出现在 item 里
_NOISE = re.compile(
    r"(【|】|\[|\]|（|）|\(|\)|「|」|《|》|GB|IC|IC2|现货|预告|截团|提醒|"
    r"开团|补货|发售|团购|上新|新品|正式|支持|改装|随机|配色|自选|"
    r"第[一二三四五六七八九十\d]+[批季弹]|今晚|明晚|号|点|月|日|\||🎁|·)"
)
_SPACE = re.compile(r"\s+")

# 品牌/作者名特征：匹配这些的多半是店铺/人名，不是产品型号
_NOT_ITEM_SUFFIX = (
    "studio", "Studio", "STUDIO", "工作室", "官方", "旗舰店", "店铺",
)
# 含这些词的片段视为店铺/品牌名（整段跳过）
_BRAND_WORDS = (
    "外设", "键极客", "键帽馆", "小店", "代理", "代理店", "品牌",
)

# 常见“方”标记：这些词前后的内容多为人名/店铺名而非产品
_PARTY_HINTS = ("×", "x", "X", "联名", "携手", "代理", "出品", "studio", "Studio")

# 品类名词本身（当提取不到型号时直接用品类名，比用作者名自然）
CATEGORY_NOUNS = {
    "轴体": "这个轴",
    "键帽": "这套键帽",
    "桌垫": "这张桌垫",
    "套件": "这个套件",
    "成品": "这把键盘",
    "数据线": "这根线",
}


def clean_title(title: str) -> str:
    """清洗标题，去掉标签与噪声，便于提取关键词。"""
    if not title:
        return ""
    t = _NOISE.sub(" ", title)
    t = _SPACE.sub(" ", t).strip(" 　-—·|/")
    return t


def _looks_like_item(word: str) -> bool:
    """粗判一个片段是否像产品型号而非人名/品牌名。"""
    if not word or len(word) < 2 or len(word) > 14:
        return False
    if word.isdigit():
        return False
    if any(word.endswith(s) for s in _NOT_ITEM_SUFFIX):
        return False
    # 纯英文单词（无数字、无中文）多为品牌/人名，如 Ruben、Lin、AEMEATH
    if re.fullmatch(r"[A-Za-z'’ ]+", word):
        return False
    # 以 1~2 个英文大写字母开头紧跟中文，多半是品牌名被切断（如 "G新款…"）
    if re.match(r"^[A-Z]{1,2}[一-龥]", word):
        return False
    return True


def extract_item(title: str, text: str = "") -> Optional[Dict]:
    """
    从标题/正文里判断品类并提取“具体型号”。

    提取不到合适的型号时，回退到品类代称（如「这个轴」「这套键帽」），
    而不是硬塞作者名或品牌名——那会让评论显得莫名其妙。

    :return: {'category': 品类名, 'item': 具体词或品类代称} 或 None
    """
    blob = f"{title or ''} {text or ''}"
    if not blob.strip():
        return None

    # 匹配优先级：命中词越长越具体（"磁轴键盘" 应判为成品而非轴体）
    best = None
    best_len = 0
    for cat in CATEGORIES:
        for w in cat["words"]:
            if w.lower() in blob.lower() and len(w) > best_len:
                best = cat
                best_len = len(w)

    if not best:
        return None

    cleaned = clean_title(title) or ""
    # 按分隔符切成候选片段，优先取较短的
    # 只在明显分隔符处切分，避免把 "GWANG新款..." 从中间切开
    parts = re.split(r"[\s/、,，:：!！?？|—]+", cleaned)
    item = ""
    for part in parts:
        part = part.strip(" .+-—")
        if _looks_like_item(part):
            item = part
            break

    # 再尝试：从片段中截取包含品类关键词的更短子串
    if not item:
        for part in parts:
            for w in best["words"]:
                idx = part.lower().find(w.lower())
                if idx >= 0:
                    # 优先用整段；过长才以关键词为中心截取，
                    # 且不截断中文词首（避免「人工智能」变成「工智能」）
                    if len(part) <= 14:
                        frag = part
                    else:
                        s = max(0, idx - 6)
                        # 若左边界落在中文词中间，向前找到分隔或开头
                        if s > 0 and ord(part[s]) > 127 and ord(part[s - 1]) > 127:
                            s = 0
                        frag = part[s: idx + len(w) + 4].strip(" .+-—")
                    # 去掉可能夹带的英文人名前缀
                    frag = re.sub(r"^[A-Za-z'’ ]{2,}\s+", "", frag).strip()
                    if _looks_like_item(frag):
                        item = frag
                        break
            if item:
                break

    if not item:
        item = CATEGORY_NOUNS.get(best["name"], best["name"])
    else:
        # 最后一道保险：item 若不含任何品类关键词且明显偏短，
        # 多半是品牌/人名（羽树、捕获者），改用品类代称更自然
        cat_words2 = [w.lower() for w in best["words"]]
        if len(item) <= 5 and not any(w in item.lower() for w in cat_words2):
            item = CATEGORY_NOUNS.get(best["name"], best["name"])
        # 过长的片段读起来像标题而非产品名，也换成品类代称
        if len(item) > 10:
            item = CATEGORY_NOUNS.get(best["name"], best["name"])

    return {"category": best["name"], "item": item}


class Corpus:
    """回复语料池，带近期去重与内容相关性生成。"""

    def __init__(self, items: Optional[List[str]] = None):
        cleaned = [str(i).strip() for i in (items or DEFAULT_CORPUS) if str(i).strip()]
        if not cleaned:
            cleaned = list(DEFAULT_CORPUS)
            log.warning("语料池为空，回退默认值")
        self.items = cleaned
        self._recent: List[str] = []

    # ---------- 通用选取 ----------

    def pick(self) -> str:
        """从通用语料里选一条（近期不重复）。"""
        fresh = [i for i in self.items if i not in self._recent]
        if not fresh:
            self._recent.clear()
            fresh = list(self.items)

        choice = random.choice(fresh)
        self._recent.append(choice)
        if len(self._recent) >= len(self.items):
            self._recent = (
                self._recent[-(len(self.items) - 1):] if len(self.items) > 1 else []
            )
        return self._normalize(choice)

    # ---------- 结合内容生成 ----------

    def pick_for(self, title: str = "", text: str = "") -> str:
        """
        优先生成与帖子内容相关的评论；提不到内容则回退通用语。

        这是主要入口。
        """
        info = extract_item(title, text)
        if not info:
            return self.pick()

        cat = next(
            (c for c in CATEGORIES if c["name"] == info["category"]), None
        )
        if not cat:
            return self.pick()

        template = random.choice(cat["templates"])
        opener = random.choice(OPENERS)
        content = f"{opener}{template.format(item=info['item'])}"

        # 生成结果与近期重复时换一个模板
        for _ in range(4):
            if content not in self._recent:
                break
            template = random.choice(cat["templates"])
            opener = random.choice(OPENERS)
            content = f"{opener}{template.format(item=info['item'])}"

        self._recent.append(content)
        if len(self._recent) > 20:
            self._recent = self._recent[-20:]
        return self._normalize(content)

    @staticmethod
    def _normalize(text: str) -> str:
        """裁到合法长度，去掉多余空白。"""
        text = " ".join(str(text).split())
        if len(text) < MIN_LEN:
            return DEFAULT_CORPUS[0]
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN]
        return text

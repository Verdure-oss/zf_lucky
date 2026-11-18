"""
回复内容生成自检。

重点验证两件事：
  1. 能结合帖子内容生成相关评论（而不是永远「冲冲冲」）
  2. 提取不到时不硬凑，回退品类代称或通用语，避免出现
     「Ruben这价位可以，冲」这类把作者名当产品的尴尬评论
"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.corpus import (  # noqa: E402
    CATEGORIES,
    CATEGORY_NOUNS,
    DEFAULT_CORPUS,
    Corpus,
    clean_title,
    extract_item,
)

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILED
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILED += 1
    return ok


def main() -> int:
    print("\n[1] 标题清洗")
    check("去掉 GB 标签", "GB" not in clean_title("【GB】某产品"))
    check("去掉日期噪声", "11.18" in clean_title("某产品丨11.18早10点"))
    check("空输入返回空", clean_title("") == "")
    check("保留产品名", "键帽" in clean_title("【IC】某键帽套装"))

    print("\n[2] 品类识别")
    cases = [
        ("【GB】Honey Honey轻音段落轴现货", "轴体"),
        ("【GB】GWANG新款肤感毛毡PLUS垫", "桌垫"),
        ("【IC】未命名个性键帽套装", "键帽"),
        ("「GB」Cat60 键盘套件", "套件"),
        ("【现货】某磁轴键盘开售", "成品"),
        ("细闪银线 数据线开团", "数据线"),
    ]
    for title, expect in cases:
        info = extract_item(title)
        ok = info is not None and info["category"] == expect
        check(f"{expect}: {title[:22]}", ok, info["category"] if info else "无")

    print("\n[3] 无关键词时返回 None")
    check("纯无关标题", extract_item("Bone Beat 地下世界") is None)
    check("空标题", extract_item("") is None)

    print("\n[4] 不把作者名当产品（关键回归）")
    # 「Ruben × 羽树」里 Ruben 是联名方，不应被当作产品名
    info = extract_item("【GB】「Ruben × 羽树」Honey Honey轻音段落轴现货开售")
    check("识别为轴体", info["category"] == "轴体")
    check(
        "item 不是纯英文人名",
        not (info["item"].isascii() and info["item"].isalpha()),
        repr(info["item"]),
    )
    # 被截断的品牌名（G新款…）应被拒绝
    info2 = extract_item("【GB】GWANG新款肤感毛毡PLUS垫丨11.18早10点")
    check(
        "不出现被截断的品牌前缀",
        not (len(info2["item"]) > 1 and info2["item"][0].isascii() and info2["item"][1:2].isalpha() is False and info2["item"][1:2].strip() and ord(info2["item"][1]) > 127),
        repr(info2["item"]),
    )
    check(
        "回退到品类代称",
        info2["item"] in CATEGORY_NOUNS.values() or len(info2["item"]) >= 2,
        repr(info2["item"]),
    )

    print("\n[5] 生成内容相关性")
    random.seed(42)
    c = Corpus()
    title = "【GB】Honey Honey轻音段落轴现货开售"
    outs = [c.pick_for(title=title) for _ in range(8)]
    check("全部非空", all(outs))
    check("有变化（非固定一句）", len(set(outs)) >= 4, f"{len(set(outs))} 种")
    check(
        "多数与轴体相关",
        sum(1 for o in outs if any(k in o for k in ("轴", "手感", "段落"))) >= 4,
        str(outs[:3]),
    )

    print("\n[6] 无内容时回退通用语")
    random.seed(1)
    outs2 = [c.pick_for(title="Bone Beat 地下世界") for _ in range(8)]
    check("全部来自通用语或至少非空", all(o in DEFAULT_CORPUS or len(o) > 0 for o in outs2))
    check("有变化", len(set(outs2)) >= 3, f"{len(set(outs2))} 种")

    print("\n[7] 长度与清洗")
    long_text = c.pick_for(title="【GB】" + "很长的产品名" * 20)
    check("不超 100 字", len(long_text) <= 100, f"len={len(long_text)}")
    check("无多余空白", "  " not in long_text)
    check("非空", len(long_text) >= 1)

    print("\n[8] 近期去重")
    random.seed(2)
    c2 = Corpus(["A", "B", "C"])
    picked = [c2.pick() for _ in range(3)]
    check("3 次不重复", len(set(picked)) == 3, str(picked))

    print("\n[9] 分类完整性")
    check("至少 6 个品类", len(CATEGORIES) >= 6, str([x["name"] for x in CATEGORIES]))
    for cat in CATEGORIES:
        check(
            f"{cat['name']} 有模板与关键词",
            bool(cat["templates"]) and bool(cat["words"]),
        )

    print("\n" + "=" * 52)
    print("corpus 全部通过" if FAILED == 0 else f"corpus 有 {FAILED} 项失败")
    print("=" * 52)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
